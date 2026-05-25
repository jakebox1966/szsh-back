from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.industry_groups import classify_industry
from app.models import Company, CompanyComment, User
from app.routers.auth import get_current_user
from app.schemas import (
    CompanyListItem, CompanyDetailOut, CompanyInfoOut, IndustryGroupOut, SummaryOut,
    CommentOut, CommentCreate, CommentUpdate, HROut, HRTrendOut, FinanceOut,
)

router = APIRouter(prefix="/api/companies", tags=["companies"])


def _format_history(founded_year: int | None) -> str | None:
    """설립연도 INT → '29년차 (1995년 설립)' 문자열."""
    if not founded_year:
        return None
    years = date.today().year - founded_year
    if years < 1:
        years = 1
    return f"{years}년차 ({founded_year}년 설립)"


def _format_time(dt):
    if dt is None:
        return ""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    dt_aware = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    diff = now - dt_aware
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "방금 전"
    if seconds < 3600:
        return f"{seconds // 60}분 전"
    if seconds < 86400:
        return f"{seconds // 3600}시간 전"
    days = seconds // 86400
    if days == 1:
        return "1일 전"
    return f"{days}일 전"


@router.get("", response_model=list[CompanyListItem])
def search_companies(q: str = Query("", description="Search keyword"), db: Session = Depends(get_db)):
    if not q:
        return db.query(Company).limit(20).all()
    # pg_trgm 유사도 + aliases 배열 매칭 (GIN 인덱스 활용)
    # similarity(name, q) 또는 q ∈ aliases 또는 부분 일치 fallback
    rows = db.execute(text("""
        SELECT * FROM companies
        WHERE name % :q
           OR :q = ANY(aliases)
           OR name ILIKE :like
           OR eng_name ILIKE :like
        ORDER BY similarity(name, :q) DESC,
                 (name = :q) DESC,
                 LENGTH(name) ASC
        LIMIT 20
    """), {"q": q, "like": f"%{q}%"}).mappings().all()
    return [dict(r) for r in rows]


@router.get("/{slug}", response_model=CompanyDetailOut)
def get_company(slug: str, db: Session = Depends(get_db)):
    # selectinload: 각 관계를 별도 IN 쿼리로 분리해서 join row 폭발 방지.
    # 1:1 (summary, hr) 도 selectinload 가 동일 성능이라 통일 — 댓글이 늘어도
    # finance_history × hr_trends × comments 카테시안 곱 위험 없음.
    company = (
        db.query(Company)
        .options(
            selectinload(Company.summary),
            selectinload(Company.comments),
            selectinload(Company.hr),
            selectinload(Company.hr_trends),
            selectinload(Company.finance),
            selectinload(Company.finance_history),
        )
        .filter(Company.slug == slug)
        .first()
    )
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    group = classify_industry(company.industry)
    info = CompanyInfoOut(
        name=company.name,
        engName=company.eng_name,
        logo=company.logo,
        industry=company.industry,
        industryGroup=IndustryGroupOut(key=group.key, label=group.label) if group else None,
        history=_format_history(company.founded_year),
        address=company.address,
        website=company.website,
        tags=company.tags or [],
        isListed=company.is_listed,
    )

    summary = None
    if company.summary:
        s = company.summary
        summary = SummaryOut(
            avgSalary=s.avg_salary,
            avgSalaryOfficial=s.avg_salary_official,
            avgSalaryCapped=s.avg_salary_capped,
            totalEmployees=s.total_employees,
            turnoverRate=float(s.turnover_rate) if s.turnover_rate is not None else None,
            revenue=float(s.revenue) if s.revenue is not None else None,
        )

    # avg_monthly 는 공시값 우선 → 없으면 추정치
    display_salary = None
    if company.summary:
        display_salary = company.summary.avg_salary_official or company.summary.avg_salary
    avg_monthly = (display_salary // 12) if display_salary else None

    salary = {
        "avgMonthly": avg_monthly,
    }

    comments = [
        _comment_out(c)
        for c in (company.comments or [])
        if c.deleted_at is None
    ][:10]

    hr_out = None
    if company.hr:
        h = company.hr
        # year_month → "YYYY-MM" 문자열. relationship 이 이미 year_month 순 정렬
        raw_trends = list(company.hr_trends or [])
        trends = [
            HRTrendOut(
                month=t.year_month.strftime("%Y-%m"),
                in_count=t.in_count or 0,
                out_count=t.out_count or 0,
                subscribers=t.subscribers,
            )
            for t in raw_trends
        ]
        incoming = sum(t.in_count or 0 for t in raw_trends)
        outgoing = sum(t.out_count or 0 for t in raw_trends)

        # 이미지 디자인 KPI 카드 — 최신 데이터 월 기준
        latest = raw_trends[-1] if raw_trends else None
        latest_month = latest.year_month.strftime("%Y-%m") if latest else None
        monthly_headcount = latest.subscribers if latest else None
        monthly_in = latest.in_count if latest else None
        monthly_out = latest.out_count if latest else None

        # 연간 퇴사율 = 최근 12개월 퇴사 합 / 그 기간 평균 인원수 * 100
        annual_turnover = None
        last12 = raw_trends[-12:] if raw_trends else []
        if last12:
            out12 = sum(t.out_count or 0 for t in last12)
            subs12 = [t.subscribers for t in last12 if t.subscribers]
            if subs12:
                avg_subs = sum(subs12) / len(subs12)
                if avg_subs > 0:
                    annual_turnover = round(out12 / avg_subs * 100, 1)

        hr_out = HROut(
            current=h.current,
            incoming=incoming,
            outgoing=outgoing,
            latestMonth=latest_month,
            monthlyHeadcount=monthly_headcount,
            monthlyIn=monthly_in,
            monthlyOut=monthly_out,
            annualTurnover=annual_turnover,
            trends=trends,
        )

    finance_out = None
    if company.finance:
        fl = list(company.finance)
        f = fl[0] if fl else None
        if f:
            def _n(v):
                return float(v) if v is not None else None
            history = [
                {"year": fh.year, "rev": _n(fh.rev), "profit": _n(fh.profit)}
                for fh in sorted(company.finance_history or [], key=lambda x: x.year or "")
            ]
            finance_out = FinanceOut(
                revenue={"current": _n(f.revenue_current), "growth": _n(f.revenue_growth)},
                operatingProfit={"current": _n(f.operating_profit_current), "growth": _n(f.operating_profit_growth)},
                netProfit={"current": _n(f.net_profit_current), "growth": _n(f.net_profit_growth)},
                history=history,
            )

    return CompanyDetailOut(
        info=info,
        summary=summary,
        salary=salary,
        sampleComments=comments,
        hr=hr_out,
        finance=finance_out,
    )


def _comment_out(c: CompanyComment) -> CommentOut:
    return CommentOut(
        id=c.id,
        user=c.user.nickname if c.user else "(탈퇴)",
        user_id=c.user_id,
        content=c.content,
        time=_format_time(c.updated_at or c.created_at),
        edited=c.updated_at is not None,
    )


@router.get("/{slug}/comments", response_model=list[CommentOut])
def get_comments(slug: str, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), db: Session = Depends(get_db)):
    company = db.query(Company).filter(Company.slug == slug).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    comments = (
        db.query(CompanyComment)
        .filter(
            CompanyComment.company_id == company.id,
            CompanyComment.deleted_at.is_(None),
        )
        .order_by(CompanyComment.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return [_comment_out(c) for c in comments]


@router.post("/{slug}/comments", response_model=CommentOut, status_code=201)
def create_comment(
    slug: str,
    body: CommentCreate,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    company = db.query(Company).filter(Company.slug == slug).first()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    comment = CompanyComment(company_id=company.id, user_id=current.id, content=body.content)
    db.add(comment)
    db.commit()
    db.refresh(comment)
    return _comment_out(comment)


@router.put("/{slug}/comments/{comment_id}", response_model=CommentOut)
def update_comment(
    slug: str,
    comment_id: int,
    body: CommentUpdate,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    comment = (
        db.query(CompanyComment)
        .join(Company, Company.id == CompanyComment.company_id)
        .filter(
            CompanyComment.id == comment_id,
            Company.slug == slug,
            CompanyComment.deleted_at.is_(None),
        )
        .first()
    )
    if not comment:
        raise HTTPException(status_code=404, detail="comment not found")
    if comment.user_id != current.id:
        raise HTTPException(status_code=403, detail="not your comment")
    comment.content = body.content
    comment.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(comment)
    return _comment_out(comment)


@router.delete("/{slug}/comments/{comment_id}", status_code=204)
def delete_comment(
    slug: str,
    comment_id: int,
    db: Session = Depends(get_db),
    current: User = Depends(get_current_user),
):
    comment = (
        db.query(CompanyComment)
        .join(Company, Company.id == CompanyComment.company_id)
        .filter(
            CompanyComment.id == comment_id,
            Company.slug == slug,
            CompanyComment.deleted_at.is_(None),
        )
        .first()
    )
    if not comment:
        raise HTTPException(status_code=404, detail="comment not found")
    if comment.user_id != current.id:
        raise HTTPException(status_code=403, detail="not your comment")
    comment.deleted_at = datetime.utcnow()
    db.commit()
