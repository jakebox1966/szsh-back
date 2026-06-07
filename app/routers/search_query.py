"""검색 실행 + 일일 한도(회원).

/api/companies?q= 와 같은 결과를 주되, 로그인 회원이면 일일 검색 횟수를 서버에서
차감/차단한다(우회 불가). 비회원(2회/일)은 User row 가 없으므로 프론트 localStorage 가
담당하고, 여기서는 그대로 결과를 반환한다.
※ 자동완성(타이핑 중)은 기존 /api/companies 를 쓰며 카운트하지 않는다 — 여기는 "검색 제출"용.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.routers.auth import get_current_user_optional
from app.routers.companies import run_company_search
from app.schemas import CompanyListItem

router = APIRouter(prefix="/api/search", tags=["search"])

# tier 별 일일 검색 한도. premium 은 아직 미구현 → 한도 없음(None). 모르는 tier 는 free 취급.
SEARCH_DAILY_LIMITS: dict[str, int | None] = {"free": 20, "premium": None}


def _kst_today():
    """KST(UTC+9) 기준 오늘 날짜. 백엔드 시간은 naive UTC 저장 → +9h 후 .date()."""
    return (datetime.utcnow() + timedelta(hours=9)).date()


@router.get("", response_model=list[CompanyListItem])
def search(
    q: str = Query("", description="검색어"),
    user: User | None = Depends(get_current_user_optional),
    db: Session = Depends(get_db),
):
    if user is not None:
        limit = SEARCH_DAILY_LIMITS.get(user.tier, 20)
        if limit is not None:  # None = 무제한(premium)
            today = _kst_today()
            if user.search_calls_reset_at != today:  # 날짜 바뀌면 리셋
                user.search_calls_today = 0
                user.search_calls_reset_at = today
            if user.search_calls_today >= limit:
                db.commit()  # 리셋만이라도 반영
                raise HTTPException(
                    status_code=429,
                    detail={"reason": "free", "limit": limit, "used": user.search_calls_today},
                )
            user.search_calls_today += 1
            db.commit()
    return run_company_search(q, db)
