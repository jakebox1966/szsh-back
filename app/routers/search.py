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
from app.models import SearchTerm, Company

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


class RecentItemOut(BaseModel):
    keyword: str          # 기록된 원본 검색어(= 조회 당시 회사명)
    name: str             # 화면 표시명: 매칭되면 정식명, 아니면 keyword
    slug: str | None = None  # 매칭된 회사 slug (있으면 클릭 시 바로 상세 이동)


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


@router.get("/recent", response_model=list[RecentItemOut])
def recent(limit: int = 10, db: Session = Depends(get_db)):
    """최근 조회 기업. 검색어(=조회 당시 회사명)를 회사와 매칭해 정식명·slug 부여.
    같은 회사로 매칭되는 변형들은 1개로 흡수(dedupe) → 여유있게 가져와서 추린다.
    """
    limit = max(1, min(limit, 20))
    raw = (
        db.query(SearchTerm)
        .order_by(SearchTerm.last_searched_at.desc())
        .limit(limit * 5)
        .all()
    )
    keywords = [t.keyword for t in raw]
    # 이름 정확일치 일괄 조회 (대부분 여기서 해결)
    name_matches = {
        c.name: c for c in db.query(Company).filter(Company.name.in_(keywords)).all()
    }

    out: list[RecentItemOut] = []
    seen: set[str] = set()
    for t in raw:
        company = name_matches.get(t.keyword)
        if company is None:
            # 옛 표기 등은 별칭(aliases)으로 매칭 (예: "현대건설(주)일용근로자" → 현대건설(주))
            company = db.query(Company).filter(Company.aliases.any(t.keyword)).first()
        key = company.slug if company else f"kw:{t.keyword}"
        if key in seen:
            continue  # 같은 회사(또는 같은 미매칭 검색어) 중복 제거
        seen.add(key)
        out.append(
            RecentItemOut(
                keyword=t.keyword,
                name=company.name if company else t.keyword,
                slug=company.slug if company else None,
            )
        )
        if len(out) >= limit:
            break
    return out
