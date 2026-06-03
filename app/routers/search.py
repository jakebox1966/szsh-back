"""
글로벌 최신 검색어 (전체 사용자).

- POST /api/search-terms        : 검색어 1건 기록 (정규화 + 길이/블록 필터 후 upsert)
- GET  /api/search-terms/recent : 최신순 N개

최신순(A)이라 집계 없음 — last_searched_at 인덱스로 ORDER BY + LIMIT.
노출 리스크(PII·욕설·스팸) 때문에 기록 단계에서 정규화·필터를 거친다.
"""
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import SearchTerm

router = APIRouter(prefix="/api/search-terms", tags=["search-terms"])

_MIN_LEN = 2
_MAX_LEN = 40
# TODO: 운영 시 욕설/스팸 블록리스트 확장 (지금은 최소 길이/공백 정규화만)
_BLOCKLIST: set[str] = set()


def _normalize(raw: str) -> str | None:
    kw = " ".join((raw or "").split())  # 앞뒤 공백·중복 공백 정리
    if len(kw) < _MIN_LEN or len(kw) > _MAX_LEN:
        return None
    if kw.lower() in _BLOCKLIST:
        return None
    return kw


class RecordIn(BaseModel):
    keyword: str


class SearchTermOut(BaseModel):
    keyword: str

    model_config = {"from_attributes": True}


@router.post("", status_code=204)
def record(body: RecordIn, db: Session = Depends(get_db)):
    kw = _normalize(body.keyword)
    if not kw:
        return  # 필터 탈락은 조용히 무시
    now = datetime.utcnow()
    stmt = (
        pg_insert(SearchTerm)
        .values(keyword=kw, search_count=1, last_searched_at=now, created_at=now)
        .on_conflict_do_update(
            index_elements=["keyword"],
            set_={
                "search_count": SearchTerm.search_count + 1,
                "last_searched_at": now,
            },
        )
    )
    db.execute(stmt)
    db.commit()


@router.get("/recent", response_model=list[SearchTermOut])
def recent(limit: int = 10, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 20))
    return (
        db.query(SearchTerm)
        .order_by(SearchTerm.last_searched_at.desc())
        .limit(limit)
        .all()
    )
