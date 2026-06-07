"""
NPS ↔ DART fuzzy matching 스크립트.

사용법:
    python -m app.match                # 전체 매칭 실행
    python -m app.match --export       # pending 건 CSV 추출 (수동 검수용)
    python -m app.match --threshold 85 # 자동 매칭 기준 점수 변경 (기본: 90)
"""
import argparse
import csv
import re
import sys
from pathlib import Path

from fuzzywuzzy import fuzz
from sqlalchemy import func

from app.database import SessionLocal, Base, engine
from app.models import NpsRaw, DartRaw, Mapping


def _normalize(name: str) -> str:
    """회사명 정규화: 법인 키워드 제거, 소문자, 공백 정리."""
    if not name:
        return ""
    s = name.strip().lower()
    # 법인 키워드 제거
    for p in ["주식회사", "(주)", "㈜", "(유)", "유한회사", "유한책임회사",
              "inc.", "inc", "corp.", "corp", "ltd.", "ltd", "co.", "co",
              "llc", "l.l.c."]:
        s = s.replace(p, "")
    # 특수문자 제거, 공백 정리
    s = re.sub(r"[^\w\s가-힣a-z0-9]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# NPS 근로자구분 접미사 — 같은 회사를 근로자 종류별로 따로 등록한 사업장을 한 회사로 흡수.
# 예: "현대건설(주) 임직원" / "현대건설(주)-한국인근로자" / "현대건설(주)일용근로자" → 같은 회사.
# Why: 깔끔한 베이스명("현대건설(주)") 사업장이 없으면 본체 식별이 변형들을 각각 다른 회사로 쪼갬.
_WORKER_SUFFIX_RE = re.compile(
    r"\s*[-/(]*\s*("
    r"임직원|상용근로자|상용직|상용|"
    r"비정규직|비정규|정규직|"          # 씨제이올리브영주식회사-비정규 → 본사로 흡수
    r"한국인근로자|외국인근로자|외국인|"
    r"일용근로자|일용직|일용|"
    r"단시간근로자|단시간|아르바이트|파견|시간제"
    r")\s*\)?\s*$"
)


def _strip_worker_suffix(name: str) -> str:
    """근로자구분 접미사 + /일용/·/상용/ 이하 제거 (반복 적용)."""
    s = name or ""
    s = re.sub(r"/(일용|상용)/.*", "", s)
    prev = None
    while prev != s:
        prev = s
        s = _WORKER_SUFFIX_RE.sub("", s).strip()
    return s


# 법인키워드 뒤에 붙는 지점/공장/근로자 등 잔여 텍스트 제거용.
# 키워드가 맨 앞(예: "(주)성우하이텍")이면 lookbehind(?<=.) 때문에 보존 — 회사명 통째 삭제 방지.
# 예: "롯데웰푸드(주)양산공장" → "롯데웰푸드(주)", "넥센타이어(주)서울사무소" → "넥센타이어(주)"
_LEGAL_CUT_RE = re.compile(r"(?<=.)(\(주\)|㈜|\(유\)|주식회사|유한회사)\s*[-/]?\s*\S.*$")


def _group_normalize(name: str) -> str:
    """그룹화 전용 정규화: 전각→반각, 법인키워드 뒤 지점/공장/근로자구분 잔여 제거 후 _normalize.
    같은 회사의 지점·공장·근로자구분·전각 표기 차이를 같은 그룹 키로 모은다.
    예) "롯데웰푸드(주)양산공장" / "롯데웰푸드(주)푸드사업장" / "롯데웰푸드(주)" → 모두 "롯데웰푸드".
    """
    s = (name or "").replace("（", "(").replace("）", ")").replace("／", "/")
    s = re.sub(r"/(일용|상용)/.*", "", s)
    prev = None
    while prev != s:
        prev = s
        s = _LEGAL_CUT_RE.sub(lambda m: m.group(1), s).strip()   # 법인키워드 뒤 잔여 제거
        s = _strip_worker_suffix(s)                              # 키워드 없는 근로자구분 접미사
    return _normalize(s)


# 영문 알파벳 → 한국식 글자별 발음. 한국 기업 약자 (LG/SK/CJ 등) 가 알파벳을
# 한 글자씩 한글로 읽는 패턴이라 사전만 있으면 대부분 자동 변환됨.
_ALPHABET_TO_KOREAN = {
    "a": "에이", "b": "비", "c": "씨", "d": "디", "e": "이",
    "f": "에프", "g": "지", "h": "에이치", "i": "아이", "j": "제이",
    "k": "케이", "l": "엘", "m": "엠", "n": "엔", "o": "오",
    "p": "피", "q": "큐", "r": "알", "s": "에스", "t": "티",
    "u": "유", "v": "브이", "w": "더블유", "x": "엑스", "y": "와이",
    "z": "지",
}

# 글자별 발음 규칙을 따르지 않는 브랜드 고유 발음. 단어로 발음하는 케이스만 예외 등록.
# 예: NAVER 는 글자별 ("엔에이브이이알") 이 아니라 단어 ("네이버") 로 읽음.
HANYOUNG_EXCEPTIONS = {
    "naver": "네이버",
}


def _hanyoung_canonical(name: str) -> str:
    """영문 알파벳을 한국식 발음 (한글) 로 일괄 변환.

    NPS·DART 가 같은 회사를 영문/한글로 다르게 표기하는 케이스 흡수:
    예) DART "LG전자" ↔ NPS "엘지전자" — 둘 다 "엘지전자" 로 변환 후 비교.

    동작:
    1. 예외 단어 (브랜드 고유 발음) 먼저 치환 — 알고리즘이 알 수 없는 케이스만 등록
    2. 남은 영문 알파벳은 글자별로 한글 음차 (a→에이, b→비, ...)
    """
    if not name:
        return name
    s = name.lower()
    for en in sorted(HANYOUNG_EXCEPTIONS, key=len, reverse=True):
        s = s.replace(en, HANYOUNG_EXCEPTIONS[en])
    return "".join(_ALPHABET_TO_KOREAN.get(ch, ch) for ch in s)


def nps_company_key(biz_no: str | None, name: str | None) -> tuple[str, str]:
    """NPS 사업장 → 회사 단위 그룹화 키 (1차 정규식 기반).
    같은 회사의 본점/지점/공장/일용/상용 사업장이 동일 키로 묶이도록 설계.
    Why: NPS는 사업장 단위라 한 회사가 N개 row로 분리. 매칭·집계 시 회사 단위 정합성 위해.
    공유: match.py(매칭 단계)·compute.py(서빙 테이블 생성)가 동일 키 생성하도록 import 사용.
    추가 그룹화는 detect_company_groups()의 본체 식별 알고리즘에서 처리.
    """
    if not biz_no:
        return ("", "")
    base = name or ""
    # /일용/, /상용/ 이하 전체 제거 (예: "...(주)/일용/케이디에너젠 SHC-1 PROJECT")
    base = re.sub(r"/(일용|상용)/.*", "", base)
    # 끝에 붙은 "지명+사업장|공장|지점|..." 1단어 제거 (회사명 뒤 공백+한 단어)
    base = re.sub(
        r"\s+\S*?(사업장|공장|지점|본점|본사|사무소|영업소|캠퍼스)\s*$", "", base
    )
    # 끝의 괄호 추가설명 제거 (예: "(지점2)", "(MSC)")
    base = re.sub(r"\s*\([^)]+\)\s*$", "", base)
    # 그룹 정규화 (근로자구분 접미사·전각괄호 흡수 + 법인 키워드/특수문자 정리)
    base = _group_normalize(base)
    return (biz_no, base)


# 본체 식별용 법인 키워드
LEGAL_ENTITY_KEYWORDS = ["(주)", "주식회사", "㈜", "(유)", "유한회사", "유한책임회사"]
# 본체 후보 정규화 이름 최소 길이 (너무 짧으면 다른 회사 잘못 흡수 위험)
MASTER_MIN_NORM_LEN = 4


def _find_masters_in_group(members: list) -> list:
    """같은 biz_no prefix 그룹 안에서 본체 후보 식별.
    조건: 법인 키워드 보유 + 정규화 이름 길이 >= MASTER_MIN_NORM_LEN.
    Returns: 본체 후보 NPS row 리스트 (정규화 이름 짧은 순).
    """
    candidates_by_norm: dict[str, object] = {}
    for m in members:
        if not any(kw in (m.name or "") for kw in LEGAL_ENTITY_KEYWORDS):
            continue
        norm = _group_normalize(m.name)
        if len(norm) < MASTER_MIN_NORM_LEN:
            continue
        # 같은 정규화 이름의 후보가 여러 개면 가장 깨끗한 (원본 이름 짧은) 것
        if norm not in candidates_by_norm or len(m.name) < len(candidates_by_norm[norm].name):
            candidates_by_norm[norm] = m
    # 정규화 이름 짧은 순서로 정렬 (군더더기 없는 본체 우선)
    return sorted(candidates_by_norm.values(), key=lambda m: len(_group_normalize(m.name)))


def _common_prefix_len(s1: str, s2: str) -> int:
    """두 문자열의 공통 prefix 길이."""
    i = 0
    min_len = min(len(s1), len(s2))
    while i < min_len and s1[i] == s2[i]:
        i += 1
    return i


def _assign_to_master(member, masters: list):
    """멤버를 본체에 흡수 결정.
    매칭 규칙 (2단계):
      1) 정확 startswith: 멤버 정규화 이름이 본체 정규화 이름으로 시작 (가장 보수적)
      2) Fuzzy prefix: 본체가 5자 이상이고 공통 prefix가 본체 길이의 60% 이상
         예: "쿠팡풀필먼트서비스" ↔ "쿠팡풀필먼트 부천센터" — 공통 "쿠팡풀필먼트" 6/9=67%
         안전장치: 짧은 본체("삼성전자" 등)는 fuzzy 매칭 안 함 (false positive 위험)
    여러 본체가 매칭되면 가장 긴 매칭 길이가 우선.
    """
    if not masters:
        return None
    m_norm = _group_normalize(member.name)
    best_master = None
    best_score = 0
    for master in masters:
        master_norm = _group_normalize(master.name)
        master_len = len(master_norm)
        # 1) 정확 startswith
        if m_norm.startswith(master_norm):
            if master_len > best_score:
                best_master = master
                best_score = master_len
            continue
        # 2) Fuzzy prefix (본체 5자 이상 + 공통 prefix 60% 이상)
        if master_len >= 5:
            common = _common_prefix_len(m_norm, master_norm)
            if common >= 5 and common >= master_len * 0.6:
                if common > best_score:
                    best_master = master
                    best_score = common
    return best_master


def detect_company_groups(all_nps_rows: list) -> dict[int, tuple[str, str]]:
    """NPS 사업장 전체 → 회사 단위 그룹 키 매핑.

    알고리즘 (옵션 D — 본체 자동 식별):
      1. biz_no 6자리 prefix로 1차 그룹화
      2. 각 prefix 그룹 안에서 본체 후보 식별 (법인 키워드 + 최소 길이)
      3. 본체 후보 없음 → nps_company_key 정규식 결과 그대로 사용
      4. 본체 후보 있음 → 형제가 본체 이름으로 시작하면 본체로 흡수
      5. 흡수 안 된 형제는 자체 키 부여 (다른 회사로 인정)

    Returns: {nps_id: (biz_no_prefix, group_name_normalized)}
    """
    from collections import defaultdict
    by_prefix: dict[str, list] = defaultdict(list)
    for n in all_nps_rows:
        if n.biz_no:
            by_prefix[n.biz_no].append(n)

    nps_id_to_key: dict[int, tuple[str, str]] = {}

    for prefix, members in by_prefix.items():
        masters = _find_masters_in_group(members)
        if not masters:
            # 본체 후보 없음 → 정규식 키 그대로
            for m in members:
                nps_id_to_key[m.id] = nps_company_key(m.biz_no, m.name)
            continue

        # 본체 자신은 본인을 본체로 등록
        master_norms = {_group_normalize(mst.name): mst for mst in masters}
        for mst in masters:
            nps_id_to_key[mst.id] = (prefix, _group_normalize(mst.name))

        # 비본체 멤버 → 본체로 흡수 또는 자체 키
        for m in members:
            if m.id in nps_id_to_key:
                continue   # 이미 본체로 등록됨
            assigned = _assign_to_master(m, masters)
            if assigned is not None:
                nps_id_to_key[m.id] = (prefix, _group_normalize(assigned.name))
            else:
                # 본체에 흡수 안 됨 = 같은 prefix 다른 회사 → 정규식 키
                nps_id_to_key[m.id] = nps_company_key(m.biz_no, m.name)

    return nps_id_to_key


def _split_address(addr: str) -> tuple[str, str]:
    """주소를 (시도, 시군구) 첫 두 토큰으로 분리. 공백 기준 split."""
    if not addr:
        return ("", "")
    tokens = addr.strip().split()
    sido = tokens[0] if len(tokens) >= 1 else ""
    sigungu = tokens[1] if len(tokens) >= 2 else ""
    return (sido, sigungu)


def _addr_score(dart_addr: str, nps_addr: str) -> int:
    """주소 비교 가산점. 시도+시군구 모두 일치 +5, 시도만 +2, 둘 다 다름 0."""
    if not dart_addr or not nps_addr:
        return 0
    d_sido, d_sigungu = _split_address(dart_addr)
    n_sido, n_sigungu = _split_address(nps_addr)
    if d_sido and d_sido == n_sido:
        if d_sigungu and d_sigungu == n_sigungu:
            return 5
        return 2
    return 0


def _industry_score(dart_code: str | None, nps_code: str | None) -> int:
    """업종코드 비교 가산점. 완전 일치 +5, 대분류(앞 2자리) 일치 +2."""
    if not dart_code or not nps_code:
        return 0
    d = dart_code.strip()
    n = nps_code.strip()
    if d == n:
        return 5
    if len(d) >= 2 and len(n) >= 2 and d[:2] == n[:2]:
        return 2
    return 0


def _est_score(dart_est_dt, nps_applied_at) -> int:
    """설립일/가입일 근접 가산점. 1년 이내 +3, 5년 이내 +1.
    NPS 가입일은 설립 이후일 수밖에 없으므로 nps_applied_at >= dart_est_dt 기대.
    """
    if dart_est_dt is None or nps_applied_at is None:
        return 0
    diff_days = (nps_applied_at - dart_est_dt).days
    # 가입일이 설립일보다 빠르면 일반적 케이스 아님 — 가산 0
    if diff_days < -30:
        return 0
    abs_days = abs(diff_days)
    if abs_days <= 365:
        return 3
    if abs_days <= 365 * 5:
        return 1
    return 0


def match(auto_threshold: int = 90, pending_threshold: int = 70):
    """DART 기준으로 NPS 후보군 필터 → fuzzy matching → mapping 저장."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    try:
        # DART 데이터 로드 (bizr_no 있는 것만)
        dart_rows = db.query(DartRaw).filter(DartRaw.bizr_no.isnot(None)).all()
        if not dart_rows:
            print("dart_raw에 데이터가 없습니다. 먼저 ingest_dart build-map을 실행하세요.")
            return

        # 이미 매칭된 dart_id 제외
        existing_dart_ids = set(
            r[0] for r in db.query(Mapping.dart_id).all()
        )
        new_darts = [d for d in dart_rows if d.id not in existing_dart_ids]
        print(f"DART 전체: {len(dart_rows)}건, 신규 매칭 대상: {len(new_darts)}건")

        if not new_darts:
            print("매칭할 신규 건이 없습니다.")
            return

        # NPS biz_no → [NpsRaw] 인덱스 구축
        print("NPS 인덱스 구축 중...")
        all_nps = db.query(NpsRaw).all()
        nps_by_prefix: dict[str, list[NpsRaw]] = {}
        for nps in all_nps:
            if nps.biz_no:
                nps_by_prefix.setdefault(nps.biz_no, []).append(nps)
        print(f"NPS 사업장: {len(all_nps)}건, prefix 그룹: {len(nps_by_prefix)}개")

        # 본체 자동 식별 (옵션 D)
        print("NPS 본체 자동 식별 (D 알고리즘) 중...")
        nps_id_to_group_key = detect_company_groups(all_nps)
        unique_company_groups = len(set(nps_id_to_group_key.values()))
        print(f"  통합 후 회사 그룹: {unique_company_groups}개 (압축률 {(1 - unique_company_groups / len(all_nps)) * 100:.1f}%)")

        matched_count = 0
        pending_count = 0
        no_candidate = 0
        low_score = 0

        for idx, dart in enumerate(new_darts):
            # 후보군 필터: DART bizr_no 앞 6자리 == NPS biz_no
            prefix = dart.bizr_no[:6]
            candidates = nps_by_prefix.get(prefix, [])

            if not candidates:
                no_candidate += 1
                continue

            # 통합 키 단위로 그룹화 — D 알고리즘 결과 활용
            # 본점/지점/공장/일용 + 본체 자동 식별로 흡수된 사업장이 같은 회사로 합쳐짐
            groups: dict[tuple[str, str], list] = {}
            for nps in candidates:
                key = nps_id_to_group_key.get(nps.id)
                if key is None:
                    continue
                groups.setdefault(key, []).append(nps)

            # fuzzy matching + 멀티시그널 — 그룹 단위로 점수 계산
            dart_norm = _normalize(dart.corp_name)
            dart_canonical = _hanyoung_canonical(dart_norm)
            dart_addr = (dart.address or "").strip()
            dart_induty = dart.induty_code
            dart_est = dart.est_dt
            best_representative = None
            best_score = 0

            for group_key, members in groups.items():
                # 대표 row: 정규화 이름이 가장 짧은 것 (가장 깨끗한 본체일 가능성 높음)
                representative = min(members, key=lambda n: len(_normalize(n.name)))

                nps_norm = _normalize(representative.name)
                nps_canonical = _hanyoung_canonical(nps_norm)
                # 한영 음차 정규화 후 매칭 (예: "lg전자" ↔ "엘지전자" 가 같은 형태로 비교)
                base = fuzz.WRatio(dart_canonical, nps_canonical)
                bonus = 0
                # 주소 비교 (시도+시군구 분리)
                nps_addr = (representative.address_road or representative.address_lot or "").strip()
                bonus += _addr_score(dart_addr, nps_addr)
                # 업종코드 비교
                bonus += _industry_score(dart_induty, representative.industry_code)
                # 설립일 vs NPS 가입일 비교
                bonus += _est_score(dart_est, representative.applied_at)

                score = min(base + bonus, 100)

                if score > best_score:
                    best_score = score
                    best_representative = representative

            if best_score < pending_threshold:
                low_score += 1
                continue

            status = "matched" if best_score >= auto_threshold else "pending"

            db.add(Mapping(
                nps_id=best_representative.id,
                dart_id=dart.id,
                score=best_score,
                status=status,
            ))

            if status == "matched":
                matched_count += 1
            else:
                pending_count += 1

            if (idx + 1) % 500 == 0:
                db.flush()
                print(f"  진행: {idx + 1}/{len(new_darts)}")

        db.commit()
        print(f"\n완료!")
        print(f"  자동 매칭 (≥{auto_threshold}): {matched_count}건")
        print(f"  검수 필요 ({pending_threshold}~{auto_threshold - 1}): {pending_count}건")
        print(f"  후보 없음 (prefix 불일치): {no_candidate}건")
        print(f"  점수 미달 (<{pending_threshold}): {low_score}건")

    except Exception as e:
        db.rollback()
        print(f"오류 발생: {e}")
        raise
    finally:
        db.close()

    # 알고리즘 매칭 완료 후 manual_mappings.csv + 검수결과 자동 적용
    print()
    apply_manual_mappings()
    apply_reviewed_csv()


def export_pending(output_path: str = "data/pending_review.csv"):
    """status='pending' 건을 CSV로 추출."""
    db = SessionLocal()

    try:
        rows = (
            db.query(Mapping, DartRaw, NpsRaw)
            .join(DartRaw, Mapping.dart_id == DartRaw.id)
            .join(NpsRaw, Mapping.nps_id == NpsRaw.id)
            .filter(Mapping.status == "pending")
            .order_by(Mapping.score.desc())
            .all()
        )

        if not rows:
            print("검수 대상이 없습니다.")
            return

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([
                "mapping_id", "score", "status",
                "dart_corp_name", "dart_stock_code", "dart_bizr_no",
                "nps_name", "nps_biz_no", "nps_address",
            ])
            for mapping, dart, nps in rows:
                writer.writerow([
                    mapping.id, mapping.score, mapping.status,
                    dart.corp_name, dart.stock_code, dart.bizr_no,
                    nps.name, nps.biz_no, nps.address_road or nps.address_lot or "",
                ])

        print(f"CSV 추출 완료: {path} ({len(rows)}건)")

    finally:
        db.close()


def import_review(input_path: str = "data/pending_review.csv"):
    """검수 완료된 CSV를 읽어서 mapping 테이블 status 업데이트."""
    path = Path(input_path)
    if not path.exists():
        print(f"파일을 찾을 수 없습니다: {path}")
        sys.exit(1)

    db = SessionLocal()

    try:
        updated = 0
        skipped = 0

        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                mapping_id = int(row["mapping_id"])
                new_status = row["status"].strip().lower()

                if new_status not in ("matched", "rejected"):
                    skipped += 1
                    continue

                mapping = db.query(Mapping).filter(Mapping.id == mapping_id).first()
                if not mapping:
                    skipped += 1
                    continue

                mapping.status = new_status
                updated += 1

        db.commit()
        print(f"완료! 업데이트: {updated}건, 스킵: {skipped}건")

    except Exception as e:
        db.rollback()
        print(f"오류 발생: {e}")
        raise
    finally:
        db.close()


def apply_manual_mappings(input_path: str = "data/manual_mappings.csv"):
    """LLM/사람이 직접 작성한 매핑 CSV 를 읽어서 mapping 테이블에 적용.

    매칭 알고리즘이 못 잡는 케이스 (영단어 발음, 사명 변경, 한영 혼용 등) 보강용.
    CSV 형식: dart_id, nps_id, dart_corp_name, nps_name, note
    - 이미 mapping row 있으면 UPDATE (status='matched', score=100)
    - 없으면 INSERT
    - dart_id 가 다른 nps_id 와 이미 매칭돼 있어도 덮어쓰기 (manual 우선)
    """
    path = Path(input_path)
    if not path.exists():
        print(f"manual_mappings 파일 없음: {path} (스킵)")
        return

    db = SessionLocal()
    try:
        inserted = 0
        updated = 0
        skipped = 0

        with open(path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    dart_id = int(row["dart_id"])
                    nps_id = int(row["nps_id"])
                except (KeyError, ValueError):
                    skipped += 1
                    continue

                # DART/NPS 실재 확인 (FK 위반 방지)
                dart_exists = db.query(DartRaw.id).filter(DartRaw.id == dart_id).first()
                nps_exists = db.query(NpsRaw.id).filter(NpsRaw.id == nps_id).first()
                if not dart_exists or not nps_exists:
                    skipped += 1
                    continue

                existing = db.query(Mapping).filter(Mapping.dart_id == dart_id).first()
                if existing:
                    existing.nps_id = nps_id
                    existing.score = 100
                    existing.status = "matched"
                    updated += 1
                else:
                    db.add(Mapping(
                        nps_id=nps_id, dart_id=dart_id,
                        score=100, status="matched",
                    ))
                    inserted += 1

        db.commit()
        print(f"manual_mappings 적용 완료: INSERT {inserted}건, UPDATE {updated}건, 스킵 {skipped}건")
    finally:
        db.close()


def apply_reviewed_csv(input_path: str = "data/pending_review_reviewed.csv"):
    """검수 완료(pending_review_reviewed.csv)를 (dart bizr_no + nps name) 페어로 현재 mapping에 재적용.
    mapping_id 가 재생성돼도 안전하게 매칭 — rejected→매칭 제외, matched→확정.
    Why: DELETE FROM mapping 후 재match 하면 mapping_id 가 바뀌어 mapping_id 기반 재import 불가.
    """
    path = Path(input_path)
    if not path.exists():
        print(f"reviewed csv 없음: {path} (스킵)")
        return
    db = SessionLocal()
    try:
        by_pair: dict[tuple[str, str], list] = {}
        for m, d, n in (
            db.query(Mapping, DartRaw, NpsRaw)
            .join(DartRaw, Mapping.dart_id == DartRaw.id)
            .join(NpsRaw, Mapping.nps_id == NpsRaw.id)
            .all()
        ):
            by_pair.setdefault(((d.bizr_no or "").strip(), (n.name or "").strip()), []).append(m)

        rej = mat = notfound = 0
        with open(path, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                st = (r.get("status") or "").strip().lower()
                if st not in ("matched", "rejected"):
                    continue
                key = ((r.get("dart_bizr_no") or "").strip(), (r.get("nps_name") or "").strip())
                ms = by_pair.get(key, [])
                if not ms:
                    notfound += 1
                    continue
                for m in ms:
                    m.status = "rejected" if st == "rejected" else "matched"
                if st == "rejected":
                    rej += 1
                else:
                    mat += 1
        db.commit()
        print(f"reviewed.csv 재적용: rejected {rej}건, matched {mat}건, 현재mapping에 없음 {notfound}건")
    finally:
        db.close()


def dry_run_grouping(limit: int = 30):
    """본체 자동 식별 (D 알고리즘) dry-run.
    DB 변경 없이 그룹화 결과만 출력. 흡수 케이스 상위 N개 표시.
    """
    db = SessionLocal()
    try:
        print("NPS 사업장 로드 중...")
        all_nps = db.query(NpsRaw).all()
        print(f"  총 {len(all_nps)}건")

        nps_id_to_group_key = detect_company_groups(all_nps)

        # 그룹별 멤버 수집
        groups: dict[tuple[str, str], list] = {}
        for nps in all_nps:
            key = nps_id_to_group_key.get(nps.id)
            if key:
                groups.setdefault(key, []).append(nps)

        # 흡수가 일어난 그룹만 (멤버 2개 이상)
        multi_member = [(k, v) for k, v in groups.items() if len(v) >= 2]
        multi_member.sort(key=lambda x: len(x[1]), reverse=True)

        print(f"\n총 그룹: {len(groups)}건 (NPS 사업장 {len(all_nps)} → 회사 {len(groups)}, 압축 {(1 - len(groups) / len(all_nps)) * 100:.1f}%)")
        print(f"흡수 그룹 (멤버 2+): {len(multi_member)}건")
        print(f"\n=== 흡수 상위 {limit}개 그룹 (멤버 수 많은 순) ===")
        for i, (key, members) in enumerate(multi_member[:limit], 1):
            prefix, master_name = key
            print(f"\n[{i}] biz_no={prefix}, 본체='{master_name}', 멤버 {len(members)}개")
            for m in members[:10]:
                marker = "★" if _group_normalize(m.name) == master_name else "  "
                print(f"   {marker} {m.name}")
            if len(members) > 10:
                print(f"   ... 외 {len(members) - 10}개")
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="NPS ↔ DART fuzzy matching")
    parser.add_argument("--export", action="store_true", help="pending 건 CSV 추출")
    parser.add_argument("--import-csv", type=str, metavar="FILE", help="검수 완료 CSV 반영")
    parser.add_argument("--threshold", type=int, default=90, help="자동 매칭 기준 점수 (기본: 90)")
    parser.add_argument("--pending-threshold", type=int, default=70, help="pending 최소 점수 (기본: 70)")
    parser.add_argument("--output", type=str, default="data/pending_review.csv", help="CSV 출력 경로")
    parser.add_argument("--dry-run-grouping", action="store_true", help="D 알고리즘 그룹화 dry-run (DB 변경 없음)")
    parser.add_argument("--dry-run-limit", type=int, default=30, help="dry-run 출력 상위 N개")
    parser.add_argument("--apply-manual", action="store_true", help="manual_mappings.csv 만 적용 (알고리즘 매칭 스킵)")
    parser.add_argument("--manual-csv", type=str, default="data/manual_mappings.csv", help="manual mappings CSV 경로")
    args = parser.parse_args()

    if args.dry_run_grouping:
        dry_run_grouping(limit=args.dry_run_limit)
    elif args.import_csv:
        import_review(args.import_csv)
    elif args.export:
        export_pending(args.output)
    elif args.apply_manual:
        apply_manual_mappings(args.manual_csv)
    else:
        match(auto_threshold=args.threshold, pending_threshold=args.pending_threshold)


if __name__ == "__main__":
    main()
