"""
외부 공개용 사용자 식별자(public_id) 생성기 — nanoid 방식.

내부 PK(users.id, 정수 auto-increment)는 조인·FK·정렬용으로 그대로 두고,
API/URL 등 외부에는 추측 불가능한 이 public_id 만 노출한다(enumeration 방지).

길이 12자, 알파벳 64개(A-Za-z0-9_-) → 2^72 경우의 수.
  - 100만 유저 충돌 확률 ≈ 1e-10, 1천만 유저 ≈ 1e-8 수준이라 사실상 무충돌.
  - 생성순 정렬은 내부 정수 id 가 담당하므로 시간정렬(ULID/UUIDv7) 불필요.
"""
import secrets

from sqlalchemy.orm import Session

# nanoid 기본 URL-safe 알파벳 (64개)
_ALPHABET = (
    "0123456789"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "_-"
)
SIZE = 12


def random_public_id() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(SIZE))


def generate_public_id(db: Session) -> str:
    """DB UNIQUE 충돌 없는 새 public_id 생성. nickname.generate_nickname 과 동일 패턴.

    2^72 라 재시도 루프는 현실적으로 절대 돌지 않지만,
    경쟁 상황 대비 최종 방어선은 DB 의 UNIQUE 제약이 담당한다.
    """
    # 지연 import (순환 의존 방지)
    from app.models import User

    for _ in range(5):
        pid = random_public_id()
        if not db.query(User).filter(User.public_id == pid).first():
            return pid

    raise RuntimeError("public_id 생성 실패 (5회 연속 충돌 — 사실상 불가능)")
