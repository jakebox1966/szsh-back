from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# back/.env 를 항상 사용 (실행 위치와 무관)
_env_path = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    database_url: str  # .env 의 DATABASE_URL 사용
    dart_api_key: str | None = None  # .env 의 DART_API_KEY 사용

    # JWT
    jwt_secret: str = "dev-secret-change-me"            # 운영에선 .env 에서 강제 주입
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14

    # OAuth
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    kakao_client_id: str = ""
    kakao_client_secret: str = ""             # 카카오 Client Secret '사용함' 일 때만 값. 아니면 빈 문자열
    kakao_redirect_uri: str = "http://localhost:8000/auth/kakao/callback"

    # 프론트 base URL (OAuth 콜백 종료 후 돌려보낼 곳)
    frontend_url: str = "http://localhost:3000"

    # 쿠키 정책 — 로컬은 False, 프로덕션은 True 로 .env 에서 덮어쓰기
    cookie_secure: bool = False
    cookie_domain: str | None = None          # 운영: ".szsh.kr" 같이 부모 도메인. 로컬: None.

    # BFF 내부 API 키. Next.js 서버만 알고 X-Internal-Key 헤더로 전달.
    # 빈 문자열이면 검증 통과 (점진 배포: 프론트 BFF 적용 전까지 호환성 유지).
    internal_api_key: str = ""

    model_config = SettingsConfigDict(
        env_file=_env_path,
        env_ignore_extra=True,
        extra="ignore",
    )


settings = Settings()
