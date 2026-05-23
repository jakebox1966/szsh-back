"""분야별 로테이션 랭킹 API.

GET /api/home/spotlight?date=YYYY-MM-DD&group=<key>&metric=<m>

기본은 날짜 시드로 (분야, 지표) 자동 회전. group/metric 명시 시 해당 조합 반환.
"""
from datetime import date as date_cls, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import (
    SpotlightOut, SpotlightItem,
    IndustryCompareOut, CompareCompany, CompareHistoryItem,
    CompanyOfDayOut, CompanyOfDayHistory,
)
from app.industry_groups import INDUSTRY_GROUPS, get_group, IndustryGroup

router = APIRouter(prefix="/api/home", tags=["home"])


METRICS = [
    ("salary", "평균연봉 TOP"),
    ("revenue", "매출·영업이익 TOP"),
    ("growth", "인원 성장 TOP"),
]


def _data_as_of(db: Session, metric: str) -> str | None:
    """지표별 데이터 기준 시점 문자열. UI 부제로 노출."""
    if metric == "salary":
        row = db.execute(text("SELECT MAX(year) FROM dart_emp_sttus")).scalar()
        return f"{row}년 사업보고서 공시 기준" if row else None
    if metric == "revenue":
        row = db.execute(text("SELECT MAX(bsns_year) FROM dart_finance_raw")).scalar()
        return f"{row} 사업연도 기준" if row else None
    if metric == "growth":
        row = db.execute(text("SELECT MAX(year_month) FROM company_hr_trends")).scalar()
        if not row:
            return None
        return f"{row.strftime('%Y.%m')} NPS 기준 · 최근 12개월"
    return None


N_PAGES = 2   # 한 그룹 안에서 1~5위, 6~10위 두 페이지


def _pick_today(target: date_cls) -> tuple[int, int, int]:
    """날짜 시드로 (group_idx, metric_idx, page_idx) 결정.

    24일 주기:
      Day  0~11: 각 그룹의 1~5위 (page 0)
      Day 12~23: 각 그룹의 6~10위 (page 1)
      Day 24:    재시작
    """
    n_groups = len(INDUSTRY_GROUPS)
    n_metrics = len(METRICS)
    day_ord = target.toordinal()
    group_idx = day_ord % n_groups
    page_idx = (day_ord // n_groups) % N_PAGES
    # 메트릭 로테이션은 (group_idx, page_idx) 한 사이클 (24일) 마다 다음 메트릭으로
    metric_idx = (day_ord // (n_groups * N_PAGES)) % n_metrics
    return group_idx, metric_idx, page_idx


def _industry_filter(group: IndustryGroup) -> tuple[str, dict]:
    """SQL fragment + bind params: industry 컬럼 키워드 매칭.
    Returns: (sql_fragment, params_dict)
    """
    inc_clauses = []
    params: dict = {}
    for i, kw in enumerate(group.include):
        key = f"inc_{i}"
        inc_clauses.append(f"c.industry LIKE :{key}")
        params[key] = f"%{kw}%"
    inc_sql = " OR ".join(inc_clauses) if inc_clauses else "FALSE"

    exc_clauses = []
    for i, kw in enumerate(group.exclude):
        key = f"exc_{i}"
        exc_clauses.append(f"c.industry NOT LIKE :{key}")
        params[key] = f"%{kw}%"
    exc_sql = " AND " + " AND ".join(exc_clauses) if exc_clauses else ""

    return f"(({inc_sql}){exc_sql})", params


def _query_salary(db: Session, group: IndustryGroup, limit: int, offset: int = 0) -> list[dict]:
    where_sql, params = _industry_filter(group)
    sql = text(f"""
        SELECT c.slug, c.name, c.industry,
               COALESCE(s.avg_salary_official, s.avg_salary) AS salary,
               s.total_employees AS employees
        FROM companies c
        JOIN company_summary s ON s.company_id = c.id
        WHERE c.nps_id IS NOT NULL
          AND c.industry IS NOT NULL
          AND c.industry_code != '999999'
          AND {where_sql}
          AND COALESCE(s.avg_salary_official, s.avg_salary) IS NOT NULL
          AND s.total_employees >= 30
        ORDER BY salary DESC NULLS LAST
        LIMIT :limit OFFSET :offset
    """)
    params["limit"] = limit
    params["offset"] = offset
    rows = db.execute(sql, params).mappings().all()
    return [dict(r) for r in rows]


def _query_revenue(db: Session, group: IndustryGroup, limit: int, offset: int = 0) -> list[dict]:
    where_sql, params = _industry_filter(group)
    sql = text(f"""
        SELECT c.slug, c.name, c.industry,
               s.total_employees AS employees,
               f.revenue_current AS revenue,
               f.operating_profit_current AS "operatingProfit",
               f.revenue_growth AS "revenueGrowth"
        FROM companies c
        JOIN company_finance f ON f.company_id = c.id
        LEFT JOIN company_summary s ON s.company_id = c.id
        WHERE c.nps_id IS NOT NULL
          AND c.industry IS NOT NULL
          AND c.industry_code != '999999'
          AND {where_sql}
          AND f.revenue_current IS NOT NULL
        ORDER BY f.revenue_current DESC NULLS LAST
        LIMIT :limit OFFSET :offset
    """)
    params["limit"] = limit
    params["offset"] = offset
    rows = db.execute(sql, params).mappings().all()
    return [dict(r) for r in rows]


def _query_growth(db: Session, group: IndustryGroup, limit: int, offset: int = 0) -> list[dict]:
    """분야별 인원 성장률 — company_hr_trends 최근 12개월 in-out 합으로 직접 계산.
    compute.employee_growth (글로벌 TOP 50) 의존성 없이 항상 분야별 TOP 보장."""
    where_sql, params = _industry_filter(group)
    sql = text(f"""
        WITH latest AS (
            SELECT MAX(year_month) AS max_ym FROM company_hr_trends
        ),
        growth_calc AS (
            SELECT
                c.slug, c.name, c.industry,
                s.total_employees AS current_emp,
                COALESCE(SUM(ht.in_count - ht.out_count) FILTER (
                    WHERE ht.year_month > (SELECT max_ym FROM latest) - INTERVAL '12 months'
                ), 0) AS net_change_12m
            FROM companies c
            JOIN company_summary s ON s.company_id = c.id
            JOIN company_hr_trends ht ON ht.company_id = c.id
            WHERE c.nps_id IS NOT NULL
              AND c.industry IS NOT NULL
              AND c.industry_code != '999999'
              AND {where_sql}
              AND s.total_employees IS NOT NULL
              AND s.total_employees >= 50
            GROUP BY c.slug, c.name, c.industry, s.total_employees
        )
        SELECT
            slug, name, industry,
            current_emp AS employees,
            CASE WHEN (current_emp - net_change_12m) >= 50
                 THEN ROUND(net_change_12m::numeric / (current_emp - net_change_12m) * 100, 2)
                 ELSE NULL END AS "employeeGrowth"
        FROM growth_calc
        WHERE (current_emp - net_change_12m) >= 50
          AND net_change_12m > 0
        ORDER BY "employeeGrowth" DESC NULLS LAST
        LIMIT :limit OFFSET :offset
    """)
    params["limit"] = limit
    params["offset"] = offset
    rows = db.execute(sql, params).mappings().all()
    return [dict(r) for r in rows]


METRIC_QUERIES = {
    "salary": _query_salary,
    "revenue": _query_revenue,
    "growth": _query_growth,
}


@router.get("/spotlight", response_model=SpotlightOut)
def get_spotlight(
    date: Optional[str] = Query(None, description="YYYY-MM-DD, default=today"),
    group: Optional[str] = Query(None, description="industry group key (override rotation)"),
    metric: Optional[str] = Query(None, description="salary | revenue | growth (override rotation)"),
    page: Optional[int] = Query(None, ge=0, le=N_PAGES - 1,
                                description=f"page index (override rotation). 0={1}~{5}위, 1={6}~{10}위"),
    limit: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_db),
):
    if date:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="date 형식은 YYYY-MM-DD")
    else:
        target_date = date_cls.today()

    group_idx, metric_idx, page_idx = _pick_today(target_date)
    selected_group = get_group(group) if group else INDUSTRY_GROUPS[group_idx]
    if selected_group is None:
        raise HTTPException(status_code=404, detail=f"group '{group}' not found")

    metric_key = metric or METRICS[metric_idx][0]
    metric_label = next((lbl for k, lbl in METRICS if k == metric_key), None)
    if metric_label is None:
        raise HTTPException(status_code=400, detail=f"metric '{metric}' invalid")

    # page override
    if page is not None:
        page_idx = page

    offset = page_idx * limit
    rank_start = offset + 1

    query_fn = METRIC_QUERIES[metric_key]
    rows = query_fn(db, selected_group, limit, offset)
    items = [SpotlightItem(**r) for r in rows]

    return SpotlightOut(
        date=target_date.isoformat(),
        dataAsOf=_data_as_of(db, metric_key),
        groupKey=selected_group.key,
        groupLabel=selected_group.label,
        metric=metric_key,
        metricLabel=metric_label,
        pageIdx=page_idx,
        rankStart=rank_start,
        items=items,
    )


@router.get("/industry-compare", response_model=IndustryCompareOut)
def get_industry_compare(
    date: Optional[str] = Query(None, description="YYYY-MM-DD, default=today"),
    group: Optional[str] = Query(None, description="industry group key (override rotation)"),
    limit: int = Query(6, ge=2, le=10),
    db: Session = Depends(get_db),
):
    """분야 내 매출 TOP N 회사를 골라 5년치 매출/영업이익 history 반환.
    이미지1 컨셉: 같은 업종 회사들을 나란히 비교."""
    if date:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="date 형식은 YYYY-MM-DD")
    else:
        target_date = date_cls.today()

    # 분야 선택: 명시 없으면 spotlight 와 동일한 시드로 결정 (같은 날 같은 분야 노출)
    group_idx, _, _ = _pick_today(target_date)
    selected_group = get_group(group) if group else INDUSTRY_GROUPS[group_idx]
    if selected_group is None:
        raise HTTPException(status_code=404, detail=f"group '{group}' not found")

    where_sql, params = _industry_filter(selected_group)

    # 1) 분야 내 매출 TOP N 회사 선정 (최신 사업연도 매출 기준)
    top_sql = text(f"""
        SELECT c.id, c.slug, c.name, c.industry
        FROM companies c
        JOIN company_finance f ON f.company_id = c.id
        WHERE c.nps_id IS NOT NULL
          AND c.industry IS NOT NULL
          AND c.industry_code != '999999'
          AND {where_sql}
          AND f.revenue_current IS NOT NULL
        ORDER BY f.revenue_current DESC NULLS LAST
        LIMIT :limit
    """)
    params["limit"] = limit
    top_rows = db.execute(top_sql, params).mappings().all()
    if not top_rows:
        return IndustryCompareOut(
            date=target_date.isoformat(),
            groupKey=selected_group.key,
            groupLabel=selected_group.label,
            dataAsOf=_data_as_of(db, "revenue"),
            items=[],
        )

    company_ids = [r["id"] for r in top_rows]

    # 2) 해당 회사들 5년 history 일괄 조회
    hist_sql = text("""
        SELECT company_id, year, rev, profit
        FROM company_finance_history
        WHERE company_id = ANY(:ids)
        ORDER BY company_id, year
    """)
    hist_rows = db.execute(hist_sql, {"ids": company_ids}).mappings().all()

    # 회사별로 그루핑 + 전년 대비 증감률 계산
    by_company: dict[int, list[dict]] = {cid: [] for cid in company_ids}
    for r in hist_rows:
        by_company[r["company_id"]].append({
            "year": r["year"],
            "rev": r["rev"],
            "profit": r["profit"],
        })

    items: list[CompareCompany] = []
    for c in top_rows:
        rows = sorted(by_company[c["id"]], key=lambda x: x["year"] or "")
        hist: list[CompareHistoryItem] = []
        prev_rev = prev_op = None
        for row in rows:
            rev = row["rev"]
            op = row["profit"]
            rev_growth = None
            op_growth = None
            if prev_rev and rev is not None and prev_rev != 0:
                rev_growth = round((rev - prev_rev) / prev_rev * 100, 2)
            if prev_op and op is not None and prev_op != 0:
                op_growth = round((op - prev_op) / abs(prev_op) * 100, 2)
            hist.append(CompareHistoryItem(
                year=row["year"],
                revenue=rev,
                revenueGrowth=rev_growth,
                operatingProfit=op,
                profitGrowth=op_growth,
            ))
            prev_rev = rev if rev is not None else prev_rev
            prev_op = op if op is not None else prev_op

        items.append(CompareCompany(
            slug=c["slug"],
            name=c["name"],
            industry=c["industry"],
            history=hist,
        ))

    return IndustryCompareOut(
        date=target_date.isoformat(),
        groupKey=selected_group.key,
        groupLabel=selected_group.label,
        dataAsOf=_data_as_of(db, "revenue"),
        items=items,
    )


# 1일 1컴퍼니 풀: 매출 큰 상장사. 너무 작은 회사 노출 막기 위해 5천억 이상으로 필터.
COMPANY_OF_DAY_MIN_REVENUE = 500_000_000_000  # 5천억


def _pick_company_of_day(db: Session, target: date_cls) -> int | None:
    """매일 다른 회사 선정. 결정적 (같은 날 같은 회사).
    풀 = 매출 5천억+ 상장사 + NPS+DART 매칭.
    정렬 = 매출 내림차순 (Day1=매출 1위, Day2=2위, ... 기획서 사양).
    """
    sql = text("""
        SELECT c.id
        FROM companies c
        JOIN company_finance f ON f.company_id = c.id
        WHERE c.nps_id IS NOT NULL
          AND c.industry_code != '999999'
          AND c.is_listed = TRUE
          AND f.revenue_current >= :min_rev
        ORDER BY f.revenue_current DESC, c.id
    """)
    rows = db.execute(sql, {"min_rev": COMPANY_OF_DAY_MIN_REVENUE}).all()
    if not rows:
        return None
    return rows[target.toordinal() % len(rows)][0]


@router.get("/company-of-day", response_model=CompanyOfDayOut)
def get_company_of_day(
    date: Optional[str] = Query(None, description="YYYY-MM-DD, default=today"),
    db: Session = Depends(get_db),
):
    """1일 1컴퍼니 — 매일 다른 회사를 결정적으로 선택해서 핵심 재무 카드 노출."""
    if date:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="date 형식은 YYYY-MM-DD")
    else:
        target_date = date_cls.today()

    cid = _pick_company_of_day(db, target_date)
    if cid is None:
        raise HTTPException(status_code=404, detail="후보 회사 없음")

    # 회사 + 직원수 + 종목 정보 + 5년 history 일괄 조회
    info = db.execute(text("""
        SELECT c.id, c.slug, c.name, c.industry, c.nps_id,
               s.total_employees AS employees,
               d.stock_code, d.corp_cls
        FROM companies c
        LEFT JOIN company_summary s ON s.company_id = c.id
        LEFT JOIN dart_raw d ON d.corp_name = c.name
        WHERE c.id = :cid
        LIMIT 1
    """), {"cid": cid}).mappings().first()
    if not info:
        raise HTTPException(status_code=404, detail="회사 정보 없음")

    hist_rows = db.execute(text("""
        SELECT year, rev, profit
        FROM company_finance_history
        WHERE company_id = :cid
        ORDER BY year
    """), {"cid": cid}).mappings().all()

    history = [
        CompanyOfDayHistory(
            year=r["year"],
            revenue=r["rev"],
            operatingProfit=r["profit"],
        )
        for r in hist_rows
    ]

    # 최신 연도 지표 계산
    latest = history[-1] if history else None
    op_margin = None
    rev_per_emp = None
    if latest and latest.revenue and latest.revenue > 0:
        if latest.operatingProfit is not None:
            op_margin = round(latest.operatingProfit / latest.revenue * 100, 2)
        if info["employees"] and info["employees"] > 0:
            rev_per_emp = int(latest.revenue / info["employees"])

    market_map = {"Y": "코스피", "K": "코스닥", "N": "코넥스"}
    market = market_map.get(info["corp_cls"]) if info["corp_cls"] else None

    return CompanyOfDayOut(
        date=target_date.isoformat(),
        dataAsOf=_data_as_of(db, "revenue"),
        slug=info["slug"],
        name=info["name"],
        stockCode=info["stock_code"],
        market=market,
        industry=info["industry"],
        employees=info["employees"],
        history=history,
        operatingMargin=op_margin,
        revenuePerEmployee=rev_per_emp,
    )
