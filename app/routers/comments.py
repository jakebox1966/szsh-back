"""
코멘트 모아보기 — 전체 회사 코멘트를 최신순으로 집계.

- GET /api/comments/digest : 홈 카드용. 최신 1개(full) + 전체 개수. (공개)
- GET /api/comments        : 페이지용. 로그인 인지(auth-aware).
    · 비로그인 → 최신 1개만 본문 포함, 나머지는 내려주지 않음 (locked=True)
    · 로그인   → 커서 페이지네이션으로 전체

게이트 핵심: 비로그인 응답에는 가려질 본문을 절대 포함하지 않는다.
(블러는 프론트의 시각 효과일 뿐, 콘텐츠 자체를 서버가 막는다.)
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import Company, CompanyComment, User
from app.routers.auth import get_current_user_optional
from app.schemas import CommentDigestOut, CommentFeedItem, CommentFeedOut

router = APIRouter(prefix="/api/comments", tags=["comments"])

MAX_LIMIT = 50


def _base_query(db: Session):
    return (
        db.query(CompanyComment)
        .options(
            joinedload(CompanyComment.company),
            joinedload(CompanyComment.user),
        )
        .join(Company, Company.id == CompanyComment.company_id)
        .filter(CompanyComment.deleted_at.is_(None))
    )


def _to_item(c: CompanyComment) -> CommentFeedItem:
    return CommentFeedItem(
        id=c.id,
        company_name=c.company.name if c.company else "(삭제됨)",
        company_slug=c.company.slug if c.company else "",
        nickname=c.user.nickname if c.user else "(탈퇴)",
        content=c.content or "",
        created_at=c.created_at,
    )


@router.get("/digest", response_model=CommentDigestOut)
def comment_digest(
    db: Session = Depends(get_db),
    current: User | None = Depends(get_current_user_optional),
):
    # 비로그인 1개 / 로그인 최대 5개 (나머지는 /comments 에서)
    n = 5 if current is not None else 1
    base = _base_query(db)
    total = base.count()
    rows = base.order_by(CompanyComment.id.desc()).limit(n).all()
    return CommentDigestOut(items=[_to_item(r) for r in rows], total_count=total)


@router.get("", response_model=CommentFeedOut)
def list_comments(
    cursor: int | None = None,
    limit: int = 20,
    db: Session = Depends(get_db),
    current: User | None = Depends(get_current_user_optional),
):
    limit = max(1, min(limit, MAX_LIMIT))
    base = _base_query(db)
    total = base.count()

    # 비로그인: 최신 1개만 공개, 나머지 본문 미포함
    if current is None:
        top = base.order_by(CompanyComment.id.desc()).first()
        return CommentFeedOut(
            items=[_to_item(top)] if top else [],
            total_count=total,
            has_more=total > 1,
            locked=True,
            next_cursor=None,
        )

    # 로그인: id 내림차순 커서 페이지네이션 (id auto-increment ≈ 최신순)
    q = base
    if cursor is not None:
        q = q.filter(CompanyComment.id < cursor)
    rows = q.order_by(CompanyComment.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    return CommentFeedOut(
        items=[_to_item(r) for r in rows],
        total_count=total,
        has_more=has_more,
        locked=False,
        next_cursor=rows[-1].id if (rows and has_more) else None,
    )
