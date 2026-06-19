"""검색 실행.

/api/companies?q= 와 같은 결과를 준다.
※ 일일 한도는 검색이 아니라 "회사 상세 진입"(GET /api/companies/{slug})에서 차감한다.
   따라서 여기서는 차감/차단하지 않는다.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.routers.companies import run_company_search
from app.schemas import CompanyListItem

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("", response_model=list[CompanyListItem])
def search(q: str = Query("", description="검색어"), db: Session = Depends(get_db)):
    return run_company_search(q, db)
