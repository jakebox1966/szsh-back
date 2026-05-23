"""
소셜 로그인 (Google / Kakao) 라우터.

흐름:
  1) 프론트 → GET /auth/{provider}/login?next=/path
     서버가 state·next 를 httpOnly 쿠키에 심고 provider authorize URL 로 302
  2) 사용자 동의 → provider → GET /auth/{provider}/callback?code&state
     서버가 state 검증 → code 를 provider token 으로 교환 → userinfo 검증
     → User upsert → 자체 access/refresh JWT 발급 → httpOnly 쿠키 세팅
     → 프론트(next) 로 302
  3) 프론트는 이후 모든 API 호출에 credentials:include 만 붙이면 됨.
     /auth/me, /auth/refresh, /auth/logout 모두 쿠키 기반.
"""
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlencode

import bcrypt
import requests
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Path, Response, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User
from app.nickname import generate_nickname

router = APIRouter(prefix="/auth", tags=["auth"])

Provider = Literal["google", "kakao"]

ACCESS_COOKIE = "szsh_access"
REFRESH_COOKIE = "szsh_refresh"
OAUTH_STATE_COOKIE = "szsh_oauth_state"
OAUTH_NEXT_COOKIE = "szsh_oauth_next"


# ── Pydantic ────────────────────────────────────────────────────


class UserOut(BaseModel):
    id: int
    nickname: str
    email: str | None
    profile_image: str | None
    tier: str

    model_config = {"from_attributes": True}


# ── provider userinfo / token 교환 ──────────────────────────────


_GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO = "https://www.googleapis.com/oauth2/v3/userinfo"

_KAKAO_AUTHORIZE = "https://kauth.kakao.com/oauth/authorize"
_KAKAO_TOKEN = "https://kauth.kakao.com/oauth/token"
_KAKAO_USERINFO = "https://kapi.kakao.com/v2/user/me"


def _provider_authorize_url(provider: Provider, state: str) -> str:
    if provider == "google":
        if not settings.google_client_id:
            raise HTTPException(status_code=500, detail="google oauth not configured")
        params = {
            "client_id": settings.google_client_id,
            "redirect_uri": settings.google_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        }
        return f"{_GOOGLE_AUTHORIZE}?{urlencode(params)}"
    if provider == "kakao":
        if not settings.kakao_client_id:
            raise HTTPException(status_code=500, detail="kakao oauth not configured")
        params = {
            "client_id": settings.kakao_client_id,
            "redirect_uri": settings.kakao_redirect_uri,
            "response_type": "code",
            "state": state,
        }
        return f"{_KAKAO_AUTHORIZE}?{urlencode(params)}"
    raise HTTPException(status_code=400, detail="unsupported provider")


def _exchange_code(provider: Provider, code: str) -> str:
    """provider authorization code → provider access_token."""
    if provider == "google":
        r = requests.post(
            _GOOGLE_TOKEN,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=5,
        )
    elif provider == "kakao":
        data = {
            "grant_type": "authorization_code",
            "client_id": settings.kakao_client_id,
            "redirect_uri": settings.kakao_redirect_uri,
            "code": code,
        }
        if settings.kakao_client_secret:
            data["client_secret"] = settings.kakao_client_secret
        r = requests.post(_KAKAO_TOKEN, data=data, timeout=5)
    else:
        raise HTTPException(status_code=400, detail="unsupported provider")

    if r.status_code != 200:
        raise HTTPException(status_code=401, detail=f"{provider} token exchange failed: {r.text}")
    body = r.json()
    token = body.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail=f"{provider} returned no access_token")
    return token


def _verify_google(access_token: str) -> dict:
    r = requests.get(
        _GOOGLE_USERINFO,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=5,
    )
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="invalid google access_token")
    data = r.json()
    return {
        "provider_user_id": data["sub"],
        "email": data.get("email"),
        "profile_image": data.get("picture"),
    }


def _verify_kakao(access_token: str) -> dict:
    r = requests.get(
        _KAKAO_USERINFO,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=5,
    )
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="invalid kakao access_token")
    data = r.json()
    kakao_account = data.get("kakao_account") or {}
    profile = kakao_account.get("profile") or {}
    return {
        "provider_user_id": str(data["id"]),
        "email": kakao_account.get("email"),
        "profile_image": profile.get("profile_image_url"),
    }


def _verify_provider(provider: Provider, access_token: str) -> dict:
    if provider == "google":
        return _verify_google(access_token)
    if provider == "kakao":
        return _verify_kakao(access_token)
    raise HTTPException(status_code=400, detail="unsupported provider")


# ── JWT / refresh ───────────────────────────────────────────────


def _create_access_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_ttl_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _create_refresh_token(user_id: int) -> str:
    """Opaque refresh token. 형식: '<user_id>.<random>'. 검증은 DB bcrypt hash 비교."""
    return f"{user_id}.{secrets.token_urlsafe(32)}"


def _decode_access_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        raise HTTPException(status_code=401, detail="invalid token")
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="wrong token type")
    return payload


def _hash_refresh(token: str) -> str:
    return bcrypt.hashpw(token.encode(), bcrypt.gensalt()).decode()


def _verify_refresh(token: str, hashed: str) -> bool:
    return bcrypt.checkpw(token.encode(), hashed.encode())


# ── 쿠키 헬퍼 ───────────────────────────────────────────────────


def _set_auth_cookies(response: Response, access: str, refresh: str) -> None:
    common = {
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": "lax",
        "domain": settings.cookie_domain,
    }
    response.set_cookie(
        ACCESS_COOKIE,
        access,
        max_age=settings.access_token_ttl_minutes * 60,
        path="/",
        **common,
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh,
        max_age=settings.refresh_token_ttl_days * 24 * 3600,
        path="/auth",
        **common,
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/", domain=settings.cookie_domain)
    response.delete_cookie(REFRESH_COOKIE, path="/auth", domain=settings.cookie_domain)


def _set_oauth_state_cookies(response: Response, state: str, next_path: str) -> None:
    common = {
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": "lax",
        "domain": settings.cookie_domain,
        "max_age": 600,             # 10분이면 충분
        "path": "/auth",
    }
    response.set_cookie(OAUTH_STATE_COOKIE, state, **common)
    response.set_cookie(OAUTH_NEXT_COOKIE, next_path, **common)


def _clear_oauth_state_cookies(response: Response) -> None:
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/auth", domain=settings.cookie_domain)
    response.delete_cookie(OAUTH_NEXT_COOKIE, path="/auth", domain=settings.cookie_domain)


def _safe_next(next_path: str | None) -> str:
    """오픈 리다이렉트 방지: 같은 사이트 내부 경로(/...)만 허용."""
    if not next_path or not next_path.startswith("/") or next_path.startswith("//"):
        return "/"
    return next_path


# ── 의존성: 현재 로그인 유저 ─────────────────────────────────────


def get_current_user(
    authorization: str | None = Header(default=None),
    access_cookie: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    db: Session = Depends(get_db),
) -> User:
    # 쿠키 우선, 없으면 Authorization 헤더(Bearer)도 허용 — 추후 모바일 등 대비
    token: str | None = access_cookie
    if not token and authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1]
    if not token:
        raise HTTPException(status_code=401, detail="not authenticated")
    payload = _decode_access_token(token)
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        raise HTTPException(status_code=401, detail="user not found")
    return user


# ── 라우트 ──────────────────────────────────────────────────────


@router.get("/{provider}/login")
def oauth_login(
    provider: Provider = Path(...),
    next: str = "/",
):
    state = secrets.token_urlsafe(24)
    safe_next = _safe_next(next)
    url = _provider_authorize_url(provider, state)
    response = RedirectResponse(url, status_code=302)
    _set_oauth_state_cookies(response, state, safe_next)
    return response


@router.get("/{provider}/callback")
def oauth_callback(
    provider: Provider = Path(...),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    state_cookie: str | None = Cookie(default=None, alias=OAUTH_STATE_COOKIE),
    next_cookie: str | None = Cookie(default=None, alias=OAUTH_NEXT_COOKIE),
    db: Session = Depends(get_db),
):
    if error:
        raise HTTPException(status_code=400, detail=f"oauth error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="missing code or state")
    if not state_cookie or not secrets.compare_digest(state, state_cookie):
        raise HTTPException(status_code=400, detail="state mismatch")

    provider_token = _exchange_code(provider, code)
    info = _verify_provider(provider, provider_token)

    user = (
        db.query(User)
        .filter(User.provider == provider, User.provider_user_id == info["provider_user_id"])
        .first()
    )

    now = datetime.utcnow()
    if user is None:
        user = User(
            provider=provider,
            provider_user_id=info["provider_user_id"],
            email=info.get("email"),
            profile_image=info.get("profile_image"),
            nickname=generate_nickname(db),
            created_at=now,
            last_active_at=now,
        )
        db.add(user)
    else:
        user.email = info.get("email") or user.email
        user.profile_image = info.get("profile_image") or user.profile_image
        user.last_active_at = now
    db.flush()

    access = _create_access_token(user.id)
    refresh = _create_refresh_token(user.id)
    user.refresh_token_hash = _hash_refresh(refresh)
    db.commit()

    redirect_to = settings.frontend_url.rstrip("/") + _safe_next(next_cookie)
    response = RedirectResponse(redirect_to, status_code=302)
    _set_auth_cookies(response, access, refresh)
    _clear_oauth_state_cookies(response)
    return response


@router.get("/me", response_model=UserOut)
def me(current: User = Depends(get_current_user)):
    return UserOut.model_validate(current)


@router.post("/refresh")
def refresh(
    response: Response,
    refresh_cookie: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    db: Session = Depends(get_db),
):
    if not refresh_cookie:
        raise HTTPException(status_code=401, detail="missing refresh token")
    try:
        user_id_str, _ = refresh_cookie.split(".", 1)
        user_id = int(user_id_str)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=401, detail="invalid refresh token")

    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.refresh_token_hash:
        raise HTTPException(status_code=401, detail="invalid refresh token")
    if not _verify_refresh(refresh_cookie, user.refresh_token_hash):
        raise HTTPException(status_code=401, detail="invalid refresh token")

    new_access = _create_access_token(user.id)
    new_refresh = _create_refresh_token(user.id)
    user.refresh_token_hash = _hash_refresh(new_refresh)
    user.last_active_at = datetime.utcnow()
    db.commit()
    _set_auth_cookies(response, new_access, new_refresh)
    return {"ok": True}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    refresh_cookie: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
    db: Session = Depends(get_db),
):
    # 쿠키에서 user_id 추출해 DB refresh 무효화. 토큰 검증 실패해도 쿠키는 일단 비움.
    if refresh_cookie:
        try:
            user_id = int(refresh_cookie.split(".", 1)[0])
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                user.refresh_token_hash = None
                db.commit()
        except (ValueError, AttributeError):
            pass
    _clear_auth_cookies(response)
