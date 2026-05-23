from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

app.include_router(home.router)
app.include_router(spotlight.router)
app.include_router(companies.router)
app.include_router(auth.router)


@app.get("/")
def root():
    return {"status": "ok", "docs": "/docs"}
