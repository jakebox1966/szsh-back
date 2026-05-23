# 백엔드 (정석: DB만 Docker, API는 로컬)

## 연결 구조

- **DB**: Docker 컨테이너 (`postgres:16-alpine`) — 포트 5432
- **API**: 로컬에서 uvicorn으로 실행 — 포트 8000, DB는 `localhost:5432`로 접속

## 1. .env 설정

`back/.env` (없으면 `.env.example` 복사 후 수정):

```env
POSTGRES_USER=szsh
POSTGRES_PASSWORD=원하는비번
POSTGRES_DB=szsh
DATABASE_URL=postgresql://szsh:원하는비번@localhost:5432/szsh
```

유저/비번/DB명은 위 네 줄이 **서로 같아야** 함.

## 2. DB만 띄우기

```bash
docker compose up -d db
```

## 3. 로컬에서 API 실행

```bash
source .venv/bin/activate
alembic upgrade head   # 최초 1회
python -m app.seed     # 최초 1회
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

- API: http://localhost:8000  
- Swagger: http://localhost:8000/docs  

자세한 내용은 프로젝트 루트 `docs/run-front-back.md` 참고.
