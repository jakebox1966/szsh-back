"""
비상장사 재무 적재 (Phase A): 사업보고서 제출 비상장사 → fnlttSinglAcntAll API.

기존 재무 경로(상장사 일괄 txt 파일)는 비상장을 못 받으므로, 회사별 재무 API로 보강한다.
compute.py 가 쓰는 표준 XBRL account_id 를 그대로 적재 → match + compute 가 자동으로 서빙에 반영.

흐름:
  1) corpCode.xml 로 corp_name → corp_code 해석 (로컬 캐시 우선)
  2) company.json → dart_raw upsert (bizr_no 등 — match.py 가 NPS 와 매칭하는 데 필요)
  3) fnlttSinglAcntAll(연도별) → dart_finance_raw upsert
  이후: python -m app.match (새 dart 를 NPS 와 매칭) → python -m app.compute (서빙 반영)
  ※ 사업보고서 미제출(감사보고서만) 회사는 API 가 013 → 스킵(= Phase B 감사보고서 파서 대상)

사용:
  python -m app.ingest_unlisted                         # 내장 SEED
  python -m app.ingest_unlisted --names "컬리,야놀자"   # 지정
"""
import argparse
import io
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests

from app.config import settings
from app.database import SessionLocal
from app.models import DartRaw, DartFinanceRaw

BASE = "https://opendart.fss.or.kr/api"
CORPCODE_CACHE = Path("data/dart/CORPCODE.xml")

# compute.py 가 매출/영업이익/순이익 추출에 쓰는 표준 account_id (이것만 적재하면 충분)
FIN_ACCOUNTS = {
    "ifrs-full_Revenue",
    "dart_OperatingIncomeLoss",
    "ifrs-full_ProfitLoss",
    "ifrs-full_RevenueFromInterest",
    "ifrs-full_FeeAndCommissionIncome",
    "ifrs-full_InsuranceRevenue",
    "dart_OperatingIncomeInsurance",
    "ifrs-full_ProfitLossFromOperatingActivities",
}

# Phase A 시드 — 사업보고서 제출 비상장사 위주(미제출이면 013 으로 자동 스킵되니 안전)
SEED = [
    "컬리", "비바리퍼블리카", "야놀자", "무신사", "우아한형제들",
    "에스케이온", "현대엔지니어링", "씨제이올리브영", "쿠팡", "오아시스",
    "당근마켓", "버킷플레이스", "메가존클라우드",
]


def _load_corpcode() -> list[tuple[str, str, str]]:
    """(corp_code, corp_name, stock_code) 목록. 캐시 없으면 다운로드 후 캐시."""
    if CORPCODE_CACHE.exists():
        xml = CORPCODE_CACHE.read_bytes()
    else:
        print("corpCode.xml 다운로드 중...")
        r = requests.get(f"{BASE}/corpCode.xml", params={"crtfc_key": settings.dart_api_key}, timeout=60)
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        xml = zf.read(zf.namelist()[0])
        CORPCODE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        CORPCODE_CACHE.write_bytes(xml)
    root = ET.fromstring(xml)
    return [
        ((e.findtext("corp_code") or "").strip(),
         (e.findtext("corp_name") or "").strip(),
         (e.findtext("stock_code") or "").strip())
        for e in root.iter("list")
    ]


def _resolve(names: list[str], corpcodes: list[tuple[str, str, str]]):
    by_name: dict[str, list] = {}
    for cc, nm, sc in corpcodes:
        by_name.setdefault(nm, []).append((cc, nm, sc))
    picked = []
    for n in names:
        ms = by_name.get(n)
        if not ms:
            print(f"  ⚠ corp_code 못찾음: {n!r}")
            continue
        unlisted = [m for m in ms if not m[2]]  # 비상장(stock_code 빈) 우선
        picked.append((unlisted or ms)[0])
    return picked


def _get(url, params):
    return requests.get(url, params={**params, "crtfc_key": settings.dart_api_key}, timeout=20).json()


def _won(s):
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _parse_dt(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y%m%d").date()
    except ValueError:
        return None


# ── Phase B: 감사보고서 문서 파싱 (사업보고서 미제출 = API 013 인 회사) ──
import re  # noqa: E402

_REV = re.compile(r"매출액|영업수익|수익\(매출")
_OP = re.compile(r"영업이익|영업손실")
_NP = re.compile(r"당기순이익|당기순손실")


def _cells(tr):
    # DART 문서는 셀을 <TD>/<TH> 또는 <TE>(account 코드 포함형)로 씀 — 셋 다 처리
    return [
        re.sub(r"<[^>]+>", " ", c).replace("&nbsp;", " ").strip()
        for c in re.findall(r"<T[DHE][^>]*>(.*?)</T[DHE]>", tr, re.S | re.I)
    ]


def _num(s):
    neg = (s.strip().startswith("(") and ")" in s) or "△" in s
    d = re.sub(r"[^\d]", "", s)
    return None if not d else (-int(d) if neg else int(d))


def _first_amt(rc):
    for c in rc[1:]:
        if re.search(r"\d,\d{3}", c):  # 천단위 콤마 = 금액 (주석번호 제외)
            return _num(c)
    return None


def _label(cell):
    return re.sub(r"^[ⅠⅡⅢⅣⅤⅥ\d\.\s]+", "", cell)


def _signed_amt(rc, lab):
    """금액 추출 + 손실 라벨 부호 보정.
    '영업손실'·'당기순손실'처럼 손실 전용 행은 괄호/△ 없이 양수 magnitude 로만 적히는
    감사보고서가 있어(예: 당근마켓 FY2022) 음수로 보정한다.
    '영업이익(손실)' 처럼 이익/손실 병기 라벨은 숫자 표기(괄호/△)에 맡긴다.
    """
    v = _first_amt(rc)
    if v is None:
        return None
    if "손실" in lab and "이익" not in lab:
        return -abs(v)
    return v


def _parse_audit_doc(text: str):
    """감사보고서 문서(HTML형 표)에서 손익계산서 매출/영업이익/당기순이익 추출.
    단일표(무신사)·페이지분할표(배민) 모두 대응: 매출 행을 가진 표부터 이어지는 표까지 본다.
    당기(좌측 첫 콤마숫자)를 채택. 단위(원/천원/백만원) 자동 환산.
    """
    unit = 1
    mm = re.search(r"단위\s*[:：]\s*([가-힣]*)\s*원", text)
    if mm:
        unit = {"": 1, "천": 1000, "백만": 10 ** 6, "십억": 10 ** 9}.get(mm.group(1), 1)
    tbls = [
        [_cells(tr) for tr in re.findall(r"<TR[^>]*>(.*?)</TR>", tb, re.S | re.I)]
        for tb in re.findall(r"<TABLE[^>]*>(.*?)</TABLE>", text, re.S | re.I)
    ]
    for i, rows in enumerate(tbls):
        rev = op = np = None
        for rc in rows:
            if not rc:
                continue
            lab = _label(rc[0])
            if rev is None and _REV.search(lab) and "외" not in lab:
                rev = _first_amt(rc)
            if op is None and _OP.search(lab):
                op = _signed_amt(rc, lab)
            if np is None and _NP.search(lab):
                np = _signed_amt(rc, lab)
        if rev is None:
            continue  # 손익계산서 표 아님
        j = i + 1
        while (op is None or np is None) and j < min(i + 4, len(tbls)):
            for rc in tbls[j]:
                if not rc:
                    continue
                lab = _label(rc[0])
                if op is None and _OP.search(lab):
                    op = _signed_amt(rc, lab)
                if np is None and _NP.search(lab):
                    np = _signed_amt(rc, lab)
            j += 1
        if op is not None or np is not None:
            return {
                "ifrs-full_Revenue": rev * unit if rev else None,
                "dart_OperatingIncomeLoss": op * unit if op is not None else None,
                "ifrs-full_ProfitLoss": np * unit if np is not None else None,
            }
    return None


def _audit_reports(cc: str, years: list[int]):
    """{bsns_year: rcept_no}. 연도별로 조회(페이지네이션 회피 — 대기업은 공시가 많아
    5년치 한 번에 조회하면 감사보고서가 page 밖으로 밀림). FY y 감사보고서는 y+1년 상반기 제출.
    """
    by_year: dict[int, str] = {}
    for y in years:
        j = _get(f"{BASE}/list.json", {"corp_code": cc, "bgn_de": f"{y + 1}0101",
                                       "end_de": f"{y + 1}0930", "page_count": "100"})
        items = j.get("list", []) if j.get("status") == "000" else []
        # 사업보고서 우선 — 감사보고서만 내다가 성장하며 사업보고서 제출사로 전환한 회사
        # (예: 당근마켓 FY2024~)는 fnlttSinglAcntAll API 가 013 이라 사업보고서 문서로만 잡힌다.
        for prefer in ("사업보고서", "연결감사보고서", "감사보고서"):
            hit = next(
                (it["rcept_no"] for it in items
                 if it.get("report_nm", "").startswith(prefer) and f"({y}." in it.get("report_nm", "")),
                None,
            )
            if hit:
                by_year[y] = hit
                break
        time.sleep(0.05)
    return by_year


def _fetch_audit_finance(cc: str, years: list[int]):
    """{bsns_year: {account_id: amount}} — 감사보고서/사업보고서 문서 파싱 결과.
    사업보고서 ZIP 은 연결·별도·본문 여러 문서로 쪼개짐 → 각 문서 파싱해 가장 완전한
    손익계산서 채택. 우선순위: 완전성(매출+영업+순익 다 있음) > 본문아님 > 연결.
    """
    out: dict[int, dict] = {}
    for y, rcept in _audit_reports(cc, years).items():
        try:
            r = requests.get(f"{BASE}/document.xml",
                             params={"crtfc_key": settings.dart_api_key, "rcept_no": rcept}, timeout=60)
            zf = zipfile.ZipFile(io.BytesIO(r.content))
        except (zipfile.BadZipFile, requests.RequestException):
            continue
        best = None  # (rank, parsed)
        for name in zf.namelist():
            try:
                doc = zf.read(name).decode("utf-8", "replace")
            except Exception:
                continue
            parsed = _parse_audit_doc(doc)
            if not parsed or not parsed.get("ifrs-full_Revenue"):
                continue
            completeness = sum(
                parsed.get(k) is not None
                for k in ("ifrs-full_Revenue", "dart_OperatingIncomeLoss", "ifrs-full_ProfitLoss")
            )
            is_main = name == f"{rcept}.xml"          # 사업보고서 본문(요약재무 섞임) → 후순위
            # 연/별도 둘 다 있으면 별도 우선 — 기존 감사보고서 데이터·상장 bulk 가 별도 기준이라
            # 일관성 유지(연결만 쓰면 해외 자회사 손익이 섞여 연도 간 기준 불일치). 연결 문서는
            # '연결' 토큰이 훨씬 많음(당근 FY2025: 별도 1 vs 연결 37) → 적은 쪽을 별도로 본다.
            consol_count = doc.count("연결")
            rank = (completeness, not is_main, -consol_count)
            if best is None or rank > best[0]:
                best = (rank, parsed)
        if best:
            out[y] = best[1]
        time.sleep(0.1)
    return out


def _load_targets_csv(path: str):
    """unlisted_targets.csv → [(corp_code, corp_name, '')]."""
    import csv as _csv
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for r in _csv.DictReader(f):
            cc = (r.get("corp_code") or "").strip()
            if cc:
                out.append((cc, (r.get("corp_name") or "").strip(), ""))
    return out


def run(years: list[int], names: list[str] | None = None,
        targets_csv: str | None = None, throttle: float = 0.3):
    if targets_csv:
        targets = _load_targets_csv(targets_csv)
    else:
        targets = _resolve(names or SEED, _load_corpcode())
    print(f"적재 대상: {len(targets)}곳")
    db = SessionLocal()
    dart_ok = fin_rows = skipped = 0
    try:
        for ti, (cc, nm, _sc) in enumerate(targets, 1):
            # resume: 이미 재무 적재된 corp_code 는 스킵 (API 절약)
            existing = db.query(DartRaw).filter(DartRaw.corp_code == cc).first()
            if existing and db.query(DartFinanceRaw.id).filter(DartFinanceRaw.dart_id == existing.id).first():
                skipped += 1
                continue
            if ti % 10 == 0:
                db.commit()  # 주기적 커밋 → 중간에 끊겨도 resume 가능
                print(f"  …진행 {ti}/{len(targets)} (적재 {dart_ok}, 스킵 {skipped})")
            time.sleep(throttle)  # 레이트리밋 완화
            info = _get(f"{BASE}/company.json", {"corp_code": cc})
            if info.get("status") != "000":
                print(f"  ⚠ company.json 실패: {nm} ({info.get('message')})")
                continue

            vals = dict(
                corp_code=cc, corp_name=info.get("corp_name") or nm,
                stock_name=info.get("stock_name"),
                stock_code=(info.get("stock_code") or "").strip() or None,
                bizr_no=info.get("bizr_no"), jurir_no=info.get("jurir_no"),
                ceo_nm=info.get("ceo_nm"), corp_cls=info.get("corp_cls"),
                address=info.get("adres"), eng_name=info.get("corp_name_eng"),
                induty_code=info.get("induty_code"),
                est_dt=_parse_dt(info.get("est_dt")), acc_mt=info.get("acc_mt"),
                fetched_at=datetime.utcnow(),
            )
            dart = db.query(DartRaw).filter(DartRaw.corp_code == cc).first()
            if dart:
                for k, v in vals.items():
                    setattr(dart, k, v)
            else:
                dart = DartRaw(**vals)
                db.add(dart)
            db.flush()
            dart_ok += 1

            got_year = False
            for year in years:
                picked_fs = None
                for fs in ("CFS", "OFS"):
                    j = _get(f"{BASE}/fnlttSinglAcntAll.json",
                             {"corp_code": cc, "bsns_year": str(year), "reprt_code": "11011", "fs_div": fs})
                    if j.get("status") == "000" and j.get("list"):
                        picked_fs = (fs, j["list"])
                        break
                    time.sleep(0.05)
                if not picked_fs:
                    continue
                fs, items = picked_fs
                got_year = True
                # account_id 별 "첫 유효값"(= 손익계산서 본문 총계; 귀속/포괄 중복은 뒤라 무시)
                seen: dict[str, tuple] = {}
                for it in items:
                    aid = it.get("account_id")
                    if aid in FIN_ACCOUNTS and aid not in seen:
                        amt = _won(it.get("thstrm_amount"))
                        if amt is not None:
                            seen[aid] = (it.get("account_nm"), amt, _won(it.get("frmtrm_amount")))
                # 매출(영업수익)을 비표준 코드(ifrs-full_GrossProfit 등)로 태깅한 회사
                # (크립토·금융 등, 예: 두나무) 구제 — 표준 매출 코드 없을 때 손익계산서 라인명으로 보강.
                if "ifrs-full_Revenue" not in seen:
                    for it in items:
                        if it.get("sj_div") not in ("CIS", "IS"):
                            continue
                        nm = (it.get("account_nm") or "").replace(" ", "")
                        if nm in ("영업수익", "매출액", "매출"):
                            amt = _won(it.get("thstrm_amount"))
                            if amt is not None:
                                seen["ifrs-full_Revenue"] = (it.get("account_nm"), amt, _won(it.get("frmtrm_amount")))
                                break
                stmt_type = "포괄손익계산서 - 연결" if fs == "CFS" else "포괄손익계산서 - 별도"
                for aid, (anm, cur, prev) in seen.items():
                    ex = (
                        db.query(DartFinanceRaw)
                        .filter_by(dart_id=dart.id, bsns_year=str(year),
                                   account_id=aid, statement_type=stmt_type)
                        .first()
                    )
                    if ex:
                        ex.current_amount, ex.prev_amount, ex.account_nm = cur, prev, anm
                    else:
                        db.add(DartFinanceRaw(
                            dart_id=dart.id, stock_code=None, bsns_year=str(year),
                            report_type="사업보고서", statement_type=stmt_type,
                            account_id=aid, account_nm=anm,
                            current_amount=cur, prev_amount=prev,
                            currency="KRW", ingested_at=datetime.utcnow(),
                        ))
                    fin_rows += 1

            # Phase B: 사업보고서 미제출(API 013) → 감사보고서 문서 파싱 fallback
            src = "API"
            if not got_year:
                audit = _fetch_audit_finance(cc, years)
                for y, accounts in audit.items():
                    for aid, amt in accounts.items():
                        if amt is None:
                            continue
                        st = "포괄손익계산서 - 감사보고서"
                        ex = (
                            db.query(DartFinanceRaw)
                            .filter_by(dart_id=dart.id, bsns_year=str(y),
                                       account_id=aid, statement_type=st)
                            .first()
                        )
                        if ex:
                            ex.current_amount = amt
                        else:
                            db.add(DartFinanceRaw(
                                dart_id=dart.id, stock_code=None, bsns_year=str(y),
                                report_type="감사보고서", statement_type=st,
                                account_id=aid, account_nm=aid, current_amount=amt,
                                currency="KRW", ingested_at=datetime.utcnow(),
                            ))
                        fin_rows += 1
                if audit:
                    got_year, src = True, "감사보고서"

            print(f"  {'✅' if got_year else '❌'} {nm:16} dart_id={dart.id} "
                  f"{'재무 적재(' + src + ')' if got_year else '재무 수집 실패'}")
        db.commit()
        print(f"\n완료: dart_raw {dart_ok}건, finance {fin_rows}행, 스킵(이미적재) {skipped}건")
        print("다음 단계: python -m app.match  →  python -m app.compute")
    except Exception as e:
        db.rollback()
        print(f"오류: {e}")
        raise
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description="비상장사 재무 적재 (Phase A+B)")
    ap.add_argument("--names", type=str, default="", help="쉼표구분 회사명 (없으면 SEED)")
    ap.add_argument("--targets-csv", type=str, default="", help="corp_code 명단 CSV (예: data/dart/unlisted_targets.csv)")
    ap.add_argument("--years", type=str, default="2021,2022,2023,2024")
    ap.add_argument("--throttle", type=float, default=0.3)
    a = ap.parse_args()
    years = [int(y) for y in a.years.split(",")]
    if a.targets_csv:
        run(years, targets_csv=a.targets_csv, throttle=a.throttle)
    else:
        names = [x.strip() for x in a.names.split(",") if x.strip()] or SEED
        run(years, names=names, throttle=a.throttle)


if __name__ == "__main__":
    main()
