import re

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.routers import home, companies, auth, spotlight

app = FastAPI(title="SZSH API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "https://szsh.kr",
        "https://www.szsh.kr",
    ],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── BFF 내부 API 키 검증 미들웨어 ──────────────────────────────────
# Next.js 서버만 INTERNAL_API_KEY 보유, X-Internal-Key 헤더로 전달.
# settings.internal_api_key 가 빈 문자열이면 검증 비활성 (점진 배포 모드).
# OAuth login/callback 은 외부 provider/사용자 브라우저가 직접 호출하므로 예외.
_PUBLIC_PATH_RE = re.compile(r"^/auth/[^/]+/(login|callback)$")
_ALWAYS_PUBLIC = {"/", "/docs", "/redoc", "/openapi.json"}


@app.middleware("http")
async def internal_key_guard(request: Request, call_next):
    if not settings.internal_api_key:
        return await call_next(request)
    path = request.url.path
    if path in _ALWAYS_PUBLIC or _PUBLIC_PATH_RE.match(path):
        return await call_next(request)
    if request.headers.get("X-Internal-Key") != settings.internal_api_key:
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    return await call_next(request)


app.include_router(home.router)
app.include_router(spotlight.router)
app.include_router(companies.router)
app.include_router(auth.router)


@app.get("/")
def root():
    return {"status": "ok", "docs": "/docs"}
