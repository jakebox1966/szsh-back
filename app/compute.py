"""
원본 데이터(nps_monthly)로부터 서비스 테이블을 재계산하는 배치 스크립트.

사용법:
    python -m app.compute
    python -m app.compute --top 50        # employee_growth TOP N
    python -m app.compute --year-month 2026-01  # 특정 월 기준으로 계산
"""
import argparse
import re
from datetime import date

from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import engine, SessionLocal, Base
from app.match import nps_company_key, detect_company_groups, _normalize as _match_normalize, _hanyoung_canonical
from app.models import (
    NpsRaw, NpsMonthly, Industry, DartRaw, DartEmpSttus, DartFinanceRaw, Mapping,
    Company, CompanySummary, CompanyHR, CompanyHRTrend,
    EmployeeGrowth,
    CompanyFinance, CompanyFinanceHistory,
)

# 국민연금 보험료율 (사업자+근로자 합산 9%)
PENSION_RATE = 0.09

# 추정 연봉 이상치 상한 (이상 시 None 처리)
EST_SALARY_OUTLIER = 500_000_000  # 5억

# DART 손익계산서 핵심 계정 ID
# 일반업종
FINANCE_ACCOUNT_REVENUE = "ifrs-full_Revenue"
FINANCE_ACCOUNT_OPERATING = "dart_OperatingIncomeLoss"
FINANCE_ACCOUNT_NETPROFIT = "ifrs-full_ProfitLoss"

# 은행/증권/금융기타 영업수익 = 이자수익 + 수수료수익 합산
FINANCE_ACCOUNT_INTEREST = "ifrs-full_RevenueFromInterest"
FINANCE_ACCOUNT_FEE = "ifrs-full_FeeAndCommissionIncome"
# 보험사 매출 — 보험수익 (DB손해보험·삼성생명 등 대부분) 또는 보험서비스수익 (일부 회사)
FINANCE_ACCOUNT_INSURANCE_REV = "ifrs-full_InsuranceRevenue"
FINANCE_ACCOUNT_INSURANCE_SVC = "dart_OperatingIncomeInsurance"
# 금융업 영업이익 — 일반업종과 다른 account_id 사용
FINANCE_ACCOUNT_OPERATING_FIN = "ifrs-full_ProfitLossFromOperatingActivities"

# compute 단계에서 받아올 모든 account_id (한 번에 쿼리)
FINANCE_ALL_ACCOUNTS = [
    FINANCE_ACCOUNT_REVENUE,
    FINANCE_ACCOUNT_OPERATING,
    FINANCE_ACCOUNT_NETPROFIT,
    FINANCE_ACCOUNT_INTEREST,
    FINANCE_ACCOUNT_FEE,
    FINANCE_ACCOUNT_INSURANCE_REV,
    FINANCE_ACCOUNT_INSURANCE_SVC,
    FINANCE_ACCOUNT_OPERATING_FIN,
]

# statement_type 우선순위 (먼저 매치되는 것 채택). 단일 포괄손익이 가장 보편적.
FINANCE_STATEMENT_PRIORITY = [
    "단일 포괄손익계산서",
    "손익계산서",
    "포괄손익계산서",  # 금융업 "포괄손익계산서, 은행/증권/보험/금융기타 - 별도" 포함
]

# 지주회사 업종코드 (KSIC): 64992 = 비금융 지주회사, 659906 = 구코드 호환
INDUSTRY_HOLDING_CODES = {"64992", "659906"}

BATCH_SIZE = 5000


def _stmt_priority(stmt_type: str | None) -> int:
    """statement_type 우선순위. 낮을수록 우선 (먼저 매치되는 prefix 채택).
    같은 (회사·연도·계정과목) 에 여러 보고서 형태가 있을 때 어느 걸 쓸지 결정.
    """
    if not stmt_type:
        return 999
    for i, prefix in enumerate(FINANCE_STATEMENT_PRIORITY):
        if stmt_type.startswith(prefix):
            return i
    return 999


def _build_aliases(rep_name: str, member_names: list[str], eng_name: str | None = None,
                   stock_name: str | None = None) -> list[str]:
    """회사 검색용 별칭 리스트 생성.
    - 그룹 멤버 사업장 이름들 (NPS 다른 표기)
    - DART 영문명·종목명
    - 한국식 발음 자동 변환 (예: 'LG전자' → '엘지전자')
      → 영문 약자로 검색해도 한글 표기 회사가 잡히도록 alias 에 한글 음차 추가
    """
    alias_set: set[str] = set()
    # 기본: 멤버 이름들
    for name in member_names:
        if name:
            alias_set.add(name.strip())
    if eng_name:
        alias_set.add(eng_name.strip())
    if stock_name:
        alias_set.add(stock_name.strip())

    # 영문 알파벳 → 한글 음차 변환 후 추가. 변환 결과가 원본과 다를 때만 alias 로.
    # (KO→EN 역방향은 검색 단에서 query 를 같은 함수로 canonical 화하면 자동 매칭됨)
    expanded = set(alias_set)
    for alias in alias_set:
        canonical = _hanyoung_canonical(alias)
        if canonical and canonical != alias.lower():
            expanded.add(canonical)

    # 본체 이름 자체는 name 컬럼에 별도로 있으므로 alias에서 제거 (중복 회피)
    expanded.discard(rep_name.strip())
    # 빈 문자열 제거
    return [a for a in expanded if a]


def _is_holding_company(name: str | None, industry_code: str | None, corp_cls: str | None = None) -> bool:
    """홀딩스/지주회사 식별.
    - 이름에 '홀딩스' / '지주' 포함
    - 업종코드가 지주회사 (64992, 659906)
    - DART corp_cls = 'H' (지주회사 분류)
    """
    if name:
        if "홀딩스" in name or "지주" in name:
            return True
    if industry_code and industry_code in INDUSTRY_HOLDING_CODES:
        return True
    if corp_cls and corp_cls == "H":   # DART corp_cls: Y(코스피)/K(코스닥)/N(코넥스)/E(기타)
        return True
    return False


def _slugify(name: str) -> str:
    """사업장명을 URL-safe slug로 변환."""
    s = name.strip().lower()
    s = re.sub(r"\(주\)|\(유\)|주식회사|㈜", "", s).strip()
    s = re.sub(r"[^\w\s가-힣a-z0-9-]", "", s)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s if s else "unknown"


def _estimate_salary(notified_amount: int, subscribers: int) -> int:
    """NPS 고지금액(원) 에서 연봉 추정. 반환 단위: 원.
    이상치(상한 5억 초과)는 0으로 처리 — 데이터 오류 또는 비정상 사업장 거름.
    """
    if subscribers <= 0 or notified_amount <= 0:
        return 0
    monthly = notified_amount / subscribers / PENSION_RATE
    annual = round(monthly * 12)
    if annual > EST_SALARY_OUTLIER:
        return 0
    return annual


def _is_temporary_workplace(name: str) -> bool:
    """일용직/상용직 등 임시 사업장인지 판별."""
    patterns = ["/일용/", "(일용)", "（일용）", "-(일용)",
                "/상용/", "(상용)", "（상용）", "-(상용)"]
    return any(p in name for p in patterns)


def _bulk_insert(db, model, rows):
    """리스트를 BATCH_SIZE씩 나눠서 bulk insert."""
    for i in range(0, len(rows), BATCH_SIZE):
        db.bulk_insert_mappings(model, rows[i:i + BATCH_SIZE])
    db.flush()


def compute(top_n: int = 50, target_year_month: str | None = None):
    db = SessionLocal()

    try:
        # 기준 월 결정
        if target_year_month:
            parts = target_year_month.split("-")
            ym = date(int(parts[0]), int(parts[1]), 1)
        else:
            ym = db.query(func.max(NpsMonthly.year_month)).scalar()
            if ym is None:
                print("nps_monthly에 데이터가 없습니다. 먼저 ingest를 실행하세요.")
                return

        ym_prev = date(ym.year - 1, ym.month, 1)
        print(f"기준월: {ym.strftime('%Y-%m')}, 비교월(전년 동월): {ym_prev.strftime('%Y-%m')}")

        # ── 데이터 로드 ──
        print("데이터 로드 중...")
        current_rows = (
            db.query(NpsMonthly, NpsRaw)
            .join(NpsRaw, NpsMonthly.nps_id == NpsRaw.id)
            .filter(NpsMonthly.year_month == ym)
            .all()
        )
        print(f"최신월 사업장: {len(current_rows)}건")

        # 전년 동월
        prev_map = {}
        for pr in db.query(NpsMonthly).filter(NpsMonthly.year_month == ym_prev).all():
            prev_map[pr.nps_id] = pr

        # 최근 36개월 HR 트렌드 (상세 페이지 인력 차트용)
        thirty_six_months_ago = date(ym.year - 3, ym.month, 1)
        hr_trends_map: dict[int, list[NpsMonthly]] = {}
        for rm in (
            db.query(NpsMonthly)
            .filter(NpsMonthly.year_month > thirty_six_months_ago, NpsMonthly.year_month <= ym)
            .order_by(NpsMonthly.nps_id, NpsMonthly.year_month)
            .all()
        ):
            hr_trends_map.setdefault(rm.nps_id, []).append(rm)

        # 12개월 이직률
        twelve_months_ago = date(ym.year - 1, ym.month, 1)
        turnover_map = {}
        for row in (
            db.query(
                NpsMonthly.nps_id,
                func.sum(NpsMonthly.lost_subscribers).label("total_lost"),
                func.avg(NpsMonthly.subscribers).label("avg_subs"),
            )
            .filter(NpsMonthly.year_month > twelve_months_ago, NpsMonthly.year_month <= ym)
            .group_by(NpsMonthly.nps_id)
            .all()
        ):
            if row.avg_subs and row.avg_subs > 0:
                val = round(float(row.total_lost or 0) / float(row.avg_subs) * 100, 2)
                # numeric(10,2) 로 확장했으므로 캡 불필요
                turnover_map[row.nps_id] = val

        # 업종 코드 → 이름 매핑 (한번에 로드)
        industry_map = {r.code: r.name for r in db.query(Industry).all()}

        # NPS id → 그룹 키 매핑 — D 알고리즘 (본체 자동 식별 + 형제 흡수)
        print("NPS 본체 자동 식별 (D 알고리즘) 중...")
        all_nps_for_groups = db.query(NpsRaw).all()
        nps_id_to_group_key: dict[int, tuple[str, str]] = detect_company_groups(all_nps_for_groups)
        # 그룹 키 → 멤버 NPS row 리스트 (aliases 수집용)
        group_key_to_members: dict[tuple[str, str], list] = {}
        for n in all_nps_for_groups:
            gk = nps_id_to_group_key.get(n.id)
            if gk:
                group_key_to_members.setdefault(gk, []).append(n)

        # DART 매핑: group_key → (eng_name, website, dart_id, corp_cls, stock_name,
        #                          corp_name, address, est_dt)  ← 뒤 3개는 DART 앵커(정체성)용
        group_dart_info: dict[tuple[str, str], tuple] = {}
        for m, d in (
            db.query(Mapping, DartRaw)
            .join(DartRaw, Mapping.dart_id == DartRaw.id)
            .filter(Mapping.status == "matched")
            .all()
        ):
            gk = nps_id_to_group_key.get(m.nps_id)
            if gk:
                group_dart_info[gk] = (
                    d.eng_name, d.website, d.id, d.corp_cls, d.stock_name,
                    d.corp_name, d.address, d.est_dt,
                )

        # DART empSttus: dart_id → 최신연도 전사 평균연봉 (원)
        # 남/여 row 를 headcount 가중평균:
        #   SUM(avg_annual_salary × headcount) / SUM(headcount)
        # 주의: fyer_salary_totamt(연간급여총액) 은 이연성과급 포함으로 이상값 자주 나옴
        #       대신 jan_salary_am(1인 평균) 을 쓰는 avg_annual_salary 사용
        emp_agg = (
            db.query(
                DartEmpSttus.dart_id,
                DartEmpSttus.year,
                func.sum(DartEmpSttus.avg_annual_salary * DartEmpSttus.total_count)
                    .label("weighted_total"),
                func.sum(DartEmpSttus.total_count).label("total_headcount"),
            )
            .filter(
                DartEmpSttus.avg_annual_salary.isnot(None),
                DartEmpSttus.avg_annual_salary > 0,
                DartEmpSttus.total_count.isnot(None),
                DartEmpSttus.total_count > 0,
            )
            .group_by(DartEmpSttus.dart_id, DartEmpSttus.year)
            .order_by(DartEmpSttus.dart_id, DartEmpSttus.year.desc())
            .all()
        )
        emp_latest_map: dict[int, int] = {}
        emp_outlier_skipped = 0
        for row in emp_agg:
            if row.dart_id in emp_latest_map:
                continue  # 이미 더 최신연도 저장됨
            if row.total_headcount and row.total_headcount > 0:
                avg_man = int(row.weighted_total / row.total_headcount)
                # 이상치 필터: 평균 10억 초과 = DART 단위 오류 또는 보고 이상 (예: 팸텍 2025 = 453억)
                if avg_man <= 0:
                    continue
                if avg_man > 1_000_000_000:
                    emp_outlier_skipped += 1
                    continue
                emp_latest_map[row.dart_id] = avg_man
        print(f"  empSttus 공시 연봉 매핑: {len(emp_latest_map)}개 상장사 (이상치 {emp_outlier_skipped}건 제외)")

        # DART 직원현황 → dart_id별 "최신연도 인원 합계" (상장사 인원의 권위 출처).
        # NPS 사업장 그룹핑이 틀려도 상장사 인원은 DART 공시로 정확히 채운다.
        # 부문×성별로 쪼개진 행을 합산. 단 '합계'성 business_unit 이 있으면 그것만 써서 중복합산 방지.
        hc_rows = (
            db.query(
                DartEmpSttus.dart_id, DartEmpSttus.year,
                DartEmpSttus.business_unit, DartEmpSttus.total_count,
            )
            .filter(DartEmpSttus.total_count.isnot(None), DartEmpSttus.total_count > 0)
            .all()
        )
        _hc_by_dy: dict[int, dict[int, list]] = {}
        for r in hc_rows:
            _hc_by_dy.setdefault(r.dart_id, {}).setdefault(r.year, []).append(
                ((r.business_unit or ""), r.total_count)
            )
        emp_headcount_map: dict[int, int] = {}
        for did, by_year in _hc_by_dy.items():
            rows_y = by_year[max(by_year)]   # 최신연도
            total_like = [tc for bu, tc in rows_y if "합계" in bu]
            emp_headcount_map[did] = sum(total_like) if total_like else sum(tc for _, tc in rows_y)
        print(f"  DART 직원현황 인원: {len(emp_headcount_map)}개 상장사")

        # ── 서비스 테이블 초기화 ──
        # companies: slug 기반 UPSERT 로 보존 (company_id 유지 → comments FK 안 깨짐)
        # company_comments: 보존 (사용자 입력)
        # 나머지 파생 테이블은 TRUNCATE 후 재생성
        print("서비스 테이블 초기화 중 (companies/comments 보존)...")
        db.execute(text("""
            TRUNCATE company_finance_history, company_finance,
                     company_hr_trends, company_hr, company_summary,
                     employee_growth
        """))
        db.flush()

        # ── 메모리에서 데이터 준비 ──
        print("서비스 데이터 계산 중...")
        company_rows = []
        summary_rows = []
        hr_rows = []
        hr_trend_rows = []
        growth_list = []       # (idx, name, count, growth_pct)

        seen_slugs: set[str] = set()

        # ── NPS 사업장 그룹화 (D 알고리즘 결과 사용) ──
        # 본체 자동 식별로 묶인 그룹 단위로 1 row per 회사
        # subscribers/notified_amount/HR 트렌드 모두 그룹 내 합산
        print("NPS 사업장 그룹화 중...")
        nps_groups: dict[tuple[str, str], list] = {}
        for wm, wp in current_rows:
            gk = nps_id_to_group_key.get(wp.id)
            if gk is None:
                continue
            nps_groups.setdefault(gk, []).append((wm, wp))
        print(f"  NPS 그룹: {len(nps_groups)}건 (사업장 {len(current_rows)} → 회사 단위 통합)")

        # 그룹 단위 루프 — 그룹별 1 row
        company_dart_ids: list[int | None] = []   # company_rows 와 동일 인덱스, finance 가공용
        idx = -1
        skipped_holdings = 0
        for gk, members in nps_groups.items():
            # 대표 row 결정:
            #   1순위: 법인 키워드 보유 ("(주)", "주식회사" 등) row 중 가입자수 최대 (회사 정식명)
            #   2순위: (1순위 없으면) 전체 중 가입자수 최대
            LEGAL_KW = ["(주)", "주식회사", "㈜", "(유)", "유한회사", "유한책임회사"]
            legal_members = [
                (wm, wp) for wm, wp in members
                if any(kw in (wp.name or "") for kw in LEGAL_KW)
            ]
            if legal_members:
                rep_wm, rep_wp = max(legal_members, key=lambda x: (x[0].subscribers or 0))
            else:
                rep_wm, rep_wp = max(members, key=lambda x: (x[0].subscribers or 0))

            # 홀딩스/지주회사 스킵
            if _is_holding_company(rep_wp.name, rep_wp.industry_code):
                skipped_holdings += 1
                continue

            # 그룹 내 합산 (NPS)
            total_subscribers = sum((wm.subscribers or 0) for wm, _ in members)
            total_notified = sum((wm.notified_amount or 0) for wm, _ in members)
            est_salary = _estimate_salary(total_notified, total_subscribers)

            dart_info = group_dart_info.get(gk)
            eng_name = dart_info[0] if dart_info else None
            website = dart_info[1] if dart_info else None
            dart_id = dart_info[2] if dart_info else None
            corp_cls = dart_info[3] if dart_info else None
            stock_name = dart_info[4] if dart_info else None
            avg_salary_official = emp_latest_map.get(dart_id) if dart_id else None
            # 상장 여부: DART corp_cls가 Y(코스피)/K(코스닥)/N(코넥스)이면 True
            is_listed = corp_cls in ("Y", "K", "N") if corp_cls else False

            idx += 1

            # ── 정체성(이름·slug·주소·설립) — DART 매칭 회사는 DART 기준(앵커),
            #    아니면 NPS 대표 사업장 기준. 업종은 NPS 코드 유지(Industry 테이블 정합).
            if dart_info:
                disp_name = dart_info[5] or rep_wp.name          # DART corp_name
                address = dart_info[6] or rep_wp.address_road or rep_wp.address_lot
                dart_est = dart_info[7]                           # DART 설립일(실제 설립)
                founded_year = dart_est.year if dart_est else (
                    rep_wp.applied_at.year if rep_wp.applied_at else None
                )
            else:
                disp_name = rep_wp.name
                address = rep_wp.address_road or rep_wp.address_lot
                founded_year = rep_wp.applied_at.year if rep_wp.applied_at else None

            industry_name = industry_map.get(rep_wp.industry_code) if rep_wp.industry_code else None

            slug = _slugify(disp_name)
            if slug in seen_slugs:
                slug = f"{slug}-{rep_wp.id}"
            seen_slugs.add(slug)

            # aliases — DART 정식명 + 같은 그룹 사업장명(옛 표기 검색 보존) + 영문명·종목명
            member_names = [wp.name for _, wp in members]
            aliases = _build_aliases(disp_name, member_names, eng_name, stock_name)

            company_rows.append({
                "slug": slug,
                "name": disp_name,
                "eng_name": eng_name,
                "industry": industry_name,
                "industry_code": rep_wp.industry_code,
                "address": address,
                "founded_year": founded_year,
                "website": website,
                "nps_id": rep_wp.id,
                "is_listed": is_listed,
                "aliases": aliases,
            })
            company_dart_ids.append(dart_id)

            # 이직률은 대표 사업장 기준 (그룹 합산 비율은 사업장 통합·분리 영향 받기 쉬워 대표값이 안정적)
            turnover = turnover_map.get(rep_wp.id)

            # 전년 동월: 그룹 내 모든 멤버의 전년 데이터 합산
            prev_total_subs = sum(
                (prev_map[wp.id].subscribers or 0) for _, wp in members if wp.id in prev_map
            )
            prev_total_notif = sum(
                (prev_map[wp.id].notified_amount or 0) for _, wp in members if wp.id in prev_map
            )
            prev_salary = _estimate_salary(prev_total_notif, prev_total_subs) if prev_total_subs > 0 else None

            # 인원수: DART 매칭 회사는 직원현황 공시(권위) 우선, 없으면 NPS 그룹 합산.
            # → NPS 사업장 그룹핑이 틀려도 상장사 인원은 정확.
            dart_headcount = emp_headcount_map.get(dart_id) if dart_id else None
            headcount = dart_headcount if dart_headcount else total_subscribers

            summary_rows.append({
                "_idx": idx,
                "avg_salary": est_salary if est_salary > 0 else None,
                "avg_salary_official": avg_salary_official,
                "avg_salary_capped": est_salary >= 80_690_000,   # NPS 연소득 상한 8,069만원
                "prev_year_avg_salary": prev_salary if prev_salary else None,
                "total_employees": headcount,
                "turnover_rate": turnover,
            })

            hr_rows.append({"_idx": idx, "current": headcount})

            # HR 트렌드: 그룹 내 모든 멤버의 월별 in/out + 가입자수 합산
            trend_agg: dict = {}   # year_month -> [in, out, subscribers]
            for _, wp in members:
                for t in hr_trends_map.get(wp.id, []):
                    if t.year_month not in trend_agg:
                        trend_agg[t.year_month] = [0, 0, 0]
                    trend_agg[t.year_month][0] += t.new_acquisitions or 0
                    trend_agg[t.year_month][1] += t.lost_subscribers or 0
                    trend_agg[t.year_month][2] += t.subscribers or 0
            for ym_key, (in_cnt, out_cnt, subs) in trend_agg.items():
                hr_trend_rows.append({
                    "_idx": idx,
                    "year_month": ym_key,
                    "in_count": in_cnt,
                    "out_count": out_cnt,
                    "subscribers": subs,
                })

            # employee_growth — 전년 합산 50명 이상 + 30~500% 성장
            if (
                prev_total_subs >= 50
                and not _is_temporary_workplace(rep_wp.name)
            ):
                growth_pct = round(
                    (total_subscribers - prev_total_subs) / prev_total_subs * 100, 2
                )
                if 30 <= growth_pct <= 500:
                    growth_list.append((idx, rep_wp.name, total_subscribers, growth_pct))

        print(f"  NPS-base 회사: {idx + 1}건 (홀딩스 스킵 {skipped_holdings}건)")

        # ── DART-only 상장사 추가 ──
        # NPS 미매칭 상장사도 companies에 추가 (사용자 결정 B)
        # NPS 정보 없이 DART empSttus 공시값 + finance 만으로 노출
        print("DART-only 상장사 추가 중...")
        matched_dart_ids = {info[2] for info in group_dart_info.values() if info[2] is not None}
        unmatched_darts = (
            db.query(DartRaw)
            .filter(~DartRaw.id.in_(matched_dart_ids))
            .all()
        )
        dart_only_count = 0
        dart_only_skipped_holdings = 0
        for dart in unmatched_darts:
            if _is_holding_company(dart.corp_name, dart.induty_code, dart.corp_cls):
                dart_only_skipped_holdings += 1
                continue

            idx += 1
            slug = _slugify(dart.corp_name)
            if slug in seen_slugs:
                slug = f"{slug}-d{dart.id}"
            seen_slugs.add(slug)

            industry_name = industry_map.get(dart.induty_code) if dart.induty_code else None
            avg_salary_official = emp_latest_map.get(dart.id)

            # aliases — DART 영문명·종목명 + 한영 변환
            aliases = _build_aliases(dart.corp_name, [], dart.eng_name, dart.stock_name)

            company_rows.append({
                "slug": slug,
                "name": dart.corp_name,
                "eng_name": dart.eng_name,
                "industry": industry_name,
                "industry_code": dart.induty_code,
                "address": dart.address,
                "founded_year": dart.est_dt.year if dart.est_dt else None,
                "website": dart.website,
                "nps_id": None,   # NPS 데이터 없음
                "is_listed": dart.corp_cls in ("Y", "K", "N") if dart.corp_cls else False,
                "aliases": aliases,
            })
            company_dart_ids.append(dart.id)

            # DART 직원현황 인원 (위에서 미리 집계한 map 사용)
            dart_emp_count = emp_headcount_map.get(dart.id)

            summary_rows.append({
                "_idx": idx,
                "avg_salary": None,
                "avg_salary_official": avg_salary_official,
                "avg_salary_capped": False,
                "prev_year_avg_salary": None,
                "total_employees": dart_emp_count,
                "turnover_rate": None,
            })

            hr_rows.append({"_idx": idx, "current": dart_emp_count or 0})

            dart_only_count += 1

        print(f"  DART-only 회사: {dart_only_count}건 (홀딩스 스킵 {dart_only_skipped_holdings}건)")
        print(f"  계산 완료 총 {len(company_rows)}건")

        # ── companies UPSERT (slug 기반, 기존 id 유지) ──
        print("companies upsert 중...")
        for i in range(0, len(company_rows), BATCH_SIZE):
            batch = company_rows[i:i + BATCH_SIZE]
            stmt = pg_insert(Company).values(batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=["slug"],
                set_={
                    "name": stmt.excluded.name,
                    "eng_name": stmt.excluded.eng_name,
                    "industry": stmt.excluded.industry,
                    "industry_code": stmt.excluded.industry_code,
                    "address": stmt.excluded.address,
                    "founded_year": stmt.excluded.founded_year,
                    "website": stmt.excluded.website,
                    "nps_id": stmt.excluded.nps_id,
                    "aliases": stmt.excluded.aliases,
                    "is_listed": stmt.excluded.is_listed,
                },
            )
            db.execute(stmt)
        db.flush()

        # company_id 매핑 (slug → id). orphan(이번 계산에 없는 기존 회사) 도 포함되지만
        # summary/hr/finance 등 파생 테이블은 TRUNCATE 됐으므로 실질적 영향 없음
        slug_to_id = {r.slug: r.id for r in db.query(Company.slug, Company.id).all()}

        # ── bulk insert: company_summary ──
        print("company_summary bulk insert 중...")
        summary_final = []
        for row in summary_rows:
            slug = company_rows[row["_idx"]]["slug"]
            cid = slug_to_id[slug]
            summary_final.append({
                "company_id": cid,
                "avg_salary": row["avg_salary"],
                "avg_salary_official": row["avg_salary_official"],
                "avg_salary_capped": row["avg_salary_capped"],
                "prev_year_avg_salary": row["prev_year_avg_salary"],
                "total_employees": row["total_employees"],
                "turnover_rate": row["turnover_rate"],
            })
        _bulk_insert(db, CompanySummary, summary_final)

        # ── bulk insert: company_hr ──
        print("company_hr bulk insert 중...")
        hr_final = []
        for row in hr_rows:
            slug = company_rows[row["_idx"]]["slug"]
            cid = slug_to_id[slug]
            hr_final.append({"company_id": cid, "current": row["current"]})
        _bulk_insert(db, CompanyHR, hr_final)

        # ── bulk insert: company_hr_trends ──
        print("company_hr_trends bulk insert 중...")
        hr_trend_final = []
        for row in hr_trend_rows:
            slug = company_rows[row["_idx"]]["slug"]
            cid = slug_to_id[slug]
            hr_trend_final.append({
                "company_id": cid,
                "year_month": row["year_month"],
                "in_count": row["in_count"],
                "out_count": row["out_count"],
                "subscribers": row["subscribers"],
            })
        _bulk_insert(db, CompanyHRTrend, hr_trend_final)

        # ── employee_growth (TOP N) ──
        print(f"employee_growth TOP {top_n} 생성 중...")
        growth_list.sort(key=lambda x: x[3], reverse=True)
        growth_rows = []
        for idx, name, count, gpct in growth_list[:top_n]:
            slug = company_rows[idx]["slug"]
            cid = slug_to_id[slug]
            growth_rows.append({"company_id": cid, "name": name, "count": count, "growth": gpct})
        _bulk_insert(db, EmployeeGrowth, growth_rows)

        # ── company_finance / company_finance_history ──
        # dart_finance_raw 에서 매출/영업이익/순이익 추출 → 회사 단위 집계
        # statement_type 우선순위: 단일 포괄손익계산서 > 손익계산서 > 포괄손익계산서
        print("company_finance 가공 중...")
        finance_dart_ids = [d for d in company_dart_ids if d is not None]
        finance_current_rows = []
        finance_history_rows = []
        if finance_dart_ids:
            fr_rows = (
                db.query(DartFinanceRaw)
                .filter(
                    DartFinanceRaw.dart_id.in_(finance_dart_ids),
                    DartFinanceRaw.account_id.in_(FINANCE_ALL_ACCOUNTS),
                )
                .all()
            )

            # (dart_id, year, account_id) → 최우선 statement_type row 선택
            finance_map: dict[tuple, DartFinanceRaw] = {}
            for fr in fr_rows:
                key = (fr.dart_id, fr.bsns_year, fr.account_id)
                existing = finance_map.get(key)
                if not existing or _stmt_priority(fr.statement_type) < _stmt_priority(existing.statement_type):
                    finance_map[key] = fr

            # dart_id → {year → {account: amount}} 형태로 재정렬
            by_dart: dict[int, dict] = {}
            for (dart_id, year, account), fr in finance_map.items():
                by_dart.setdefault(dart_id, {}).setdefault(year, {})[account] = fr.current_amount

            def _extract_revenue(year_dict: dict) -> int | None:
                """매출 추출 우선순위:
                1) 일반업종 매출 (ifrs-full_Revenue)
                2) 보험사 보험수익 (보험사 핵심 매출)
                3) 은행/증권/금융기타: 이자수익 + 수수료수익 합산
                """
                rev = year_dict.get(FINANCE_ACCOUNT_REVENUE)
                if rev is not None:
                    return rev
                # 보험사: 보험수익 우선 (회사마다 InsuranceRevenue 또는 OperatingIncomeInsurance 사용)
                insurance = year_dict.get(FINANCE_ACCOUNT_INSURANCE_REV)
                if insurance is not None:
                    return insurance
                insurance_svc = year_dict.get(FINANCE_ACCOUNT_INSURANCE_SVC)
                if insurance_svc is not None:
                    return insurance_svc
                # 은행/증권/금융기타
                interest = year_dict.get(FINANCE_ACCOUNT_INTEREST)
                fee = year_dict.get(FINANCE_ACCOUNT_FEE)
                if interest is None and fee is None:
                    return None
                return (interest or 0) + (fee or 0)

            def _extract_operating(year_dict: dict) -> int | None:
                """일반업종 영업이익 우선, 없으면 금융업 영업이익(ProfitLossFromOperatingActivities)."""
                op = year_dict.get(FINANCE_ACCOUNT_OPERATING)
                if op is not None:
                    return op
                return year_dict.get(FINANCE_ACCOUNT_OPERATING_FIN)

            # company_rows 순회하며 finance 채움
            for row_idx, (cr, dart_id) in enumerate(zip(company_rows, company_dart_ids)):
                if dart_id is None:
                    continue
                year_data = by_dart.get(dart_id, {})
                if not year_data:
                    continue
                years_sorted = sorted(year_data.keys(), reverse=True)  # 최신 연도 먼저
                latest_year = years_sorted[0]
                prev_year = years_sorted[1] if len(years_sorted) > 1 else None

                latest = year_data[latest_year]
                prev = year_data.get(prev_year, {}) if prev_year else {}

                rev_cur = _extract_revenue(latest)
                op_cur = _extract_operating(latest)
                np_cur = latest.get(FINANCE_ACCOUNT_NETPROFIT)
                rev_prev = _extract_revenue(prev) if prev else None
                op_prev = _extract_operating(prev) if prev else None
                np_prev = prev.get(FINANCE_ACCOUNT_NETPROFIT) if prev else None

                def _growth(cur, prev_v):
                    if cur is None or prev_v is None or prev_v == 0:
                        return None
                    g = round((cur - prev_v) / abs(prev_v) * 100, 2)
                    # NUMERIC(10,2) 한도 보호 + 비정상 급변(전년 오파싱) 제거
                    return g if -99999 <= g <= 99999 else None

                slug = cr["slug"]
                cid = slug_to_id[slug]
                finance_current_rows.append({
                    "company_id": cid,
                    "year": latest_year,
                    "revenue_current": rev_cur,
                    "revenue_growth": _growth(rev_cur, rev_prev),
                    "operating_profit_current": op_cur,
                    "operating_profit_growth": _growth(op_cur, op_prev),
                    "net_profit_current": np_cur,
                    "net_profit_growth": _growth(np_cur, np_prev),
                })

                # 연도별 history (rev + 영업이익 사용)
                for yr in years_sorted:
                    yd = year_data[yr]
                    finance_history_rows.append({
                        "company_id": cid,
                        "year": yr,
                        "rev": _extract_revenue(yd),
                        "profit": _extract_operating(yd),
                    })

        _bulk_insert(db, CompanyFinance, finance_current_rows)
        _bulk_insert(db, CompanyFinanceHistory, finance_history_rows)
        db.flush()

        # ── 재무 sanity 정리 (DART 데이터 자체의 상식으로만 — 외부 참조 안 씀) ──
        # 1) 매출 ≤ 0 = 파싱오류(hy 등)
        # 2) 1인당 매출 명백 비정상(>1000억/명) = 오파싱/오매칭(안테나 38조/58명 = 6700억/명).
        #    ※ 임계값 주의: 정유·종합상사·석유화학은 1인당매출이 원래 150~270억/명으로 높다
        #      (GS칼텍스 160억·포스코인터 187억·E1 269억/명). 100억/명이면 이들 정상 대기업까지
        #      통째로 지워짐 → 절대 안전한 1000억/명(1e11)으로만 컷. 금융·지주는 추가 제외.
        bad = db.execute(text("""
            SELECT f.company_id FROM company_finance f
            JOIN companies c ON c.id = f.company_id
            LEFT JOIN company_summary s ON s.company_id = f.company_id
            WHERE f.revenue_current IS NOT NULL
              AND (
                f.revenue_current <= 0
                OR (s.total_employees >= 20
                    AND f.revenue_current::numeric / s.total_employees > 1e11
                    AND c.name !~ '(생명|손해|화재|은행|증권|카드|캐피탈|금융|보험|홀딩스|지주|투자|파트너스)')
              )
        """)).scalars().all()
        if bad:
            db.execute(text("DELETE FROM company_finance WHERE company_id = ANY(:ids)"), {"ids": bad})
            db.execute(text("DELETE FROM company_finance_history WHERE company_id = ANY(:ids)"), {"ids": bad})
        print(f"  재무 sanity 정리: {len(bad)}건 제거 (매출0/음수·1인당매출>1000억/명)")

        # ── company_summary.revenue 비정규화 (라우터가 직접 읽는 최신연도 매출 — compute가 소유) ──
        # 예전엔 deprecated ingest_finance 꼬리에서 했으나, 정식 경로(compute)가 책임지도록 흡수.
        # sanity 로 지워진 회사는 company_finance 에 행이 없어 자동으로 NULL 유지(잘못된 매출 노출 안 됨).
        rev_updated = db.execute(text("""
            UPDATE company_summary cs
            SET revenue = latest.revenue_current
            FROM (
                SELECT DISTINCT ON (company_id) company_id, revenue_current
                FROM company_finance
                WHERE revenue_current IS NOT NULL
                ORDER BY company_id, year DESC
            ) latest
            WHERE cs.company_id = latest.company_id
        """)).rowcount
        print(f"  summary.revenue 갱신: {rev_updated}건")

        # ── orphan 제거: 이번 빌드에 없는 옛 회사 삭제 (slug 변경·사업장 폐업 등) ──
        # 댓글 보존 안 함(사용자 결정) — orphan 의 댓글도 함께 삭제.
        produced_slugs = {r["slug"] for r in company_rows}
        orphan_ids = [
            cid for slug, cid in db.query(Company.slug, Company.id).all()
            if slug not in produced_slugs
        ]
        if orphan_ids:
            for i in range(0, len(orphan_ids), 5000):
                chunk = orphan_ids[i:i + 5000]
                db.execute(text("DELETE FROM company_comments WHERE company_id = ANY(:ids)"), {"ids": chunk})
                db.execute(text("DELETE FROM companies WHERE id = ANY(:ids)"), {"ids": chunk})
            db.flush()
        print(f"  orphan 회사 삭제: {len(orphan_ids)}건")

        db.commit()

        # ── QA: 상장사인데 '매출 큰데 인원 적음' = 매칭 의심 (자동 점검 리포트) ──
        anomalies = db.execute(text("""
            SELECT c.name, s.total_employees AS emp, f.revenue_current AS rev
            FROM companies c
            JOIN company_summary s ON s.company_id = c.id
            JOIN company_finance f ON f.company_id = c.id
            WHERE c.is_listed AND s.total_employees < 50 AND f.revenue_current > 1e11
            ORDER BY f.revenue_current DESC
        """)).mappings().all()
        print(f"\n[QA] 의심(상장·매출>1000억·인원<50): {len(anomalies)}건")
        for a in anomalies[:15]:
            print(f"   emp={a['emp']!s:>5} rev={a['rev']/1e12:>6.2f}조 | {a['name']}")

        print(f"\n완료!")
        print(f"  companies: {len(company_rows)}건")
        print(f"  company_hr_trends: {len(hr_trend_final)}건")
        print(f"  employee_growth: {len(growth_rows)}건")
        print(f"  company_finance: {len(finance_current_rows)}건")
        print(f"  company_finance_history: {len(finance_history_rows)}건")

    except Exception as e:
        db.rollback()
        print(f"오류 발생: {e}")
        raise
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="원본 데이터 → 서비스 테이블 재계산")
    parser.add_argument("--top", type=int, default=50, help="랭킹/성장률 TOP N (기본: 50)")
    parser.add_argument("--year-month", type=str, default=None, help="기준 월 (예: 2026-01). 미지정 시 최신월")
    args = parser.parse_args()
    compute(top_n=args.top, target_year_month=args.year_month)


if __name__ == "__main__":
    main()
