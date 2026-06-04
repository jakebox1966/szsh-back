# szsh-back (FastAPI + SQLAlchemy + Alembic)

백엔드 API. 독립 git repo(jakebox1966/szsh-back). PostgreSQL.
배포·데이터 상세는 루트 `../CLAUDE.md` 및 `../docs/` 참고.

## ⭐ 코드 변경 시 (사이드 이펙트·무오류)
**"됐다" 전에 반드시:**
- `python3 -m py_compile <파일>` (가능하면 앱 import 확인).
- **새 테이블/컬럼 → alembic 마이그레이션 필수.** 안 만들면 운영에서 테이블 없음(자동 생성 안 됨, `alembic upgrade head`만 돎).
  - 새 리비전: `down_revision`을 현재 head로. head 확인: `alembic heads` (또는 versions 파일에서 자식 없는 revision).
- **응답 스키마(Pydantic) 바꾸면** 프론트 `lib/api-types.ts`/매퍼/소비처도 같이(다른 repo지만 한 세트로 작업).
- 같은 모델을 직렬화하는 다른 라우터도 확인(예: User의 id 노출 제거 시 CommentOut도).

## 핵심 규칙
- **시간은 `datetime.utcnow()`**(naive UTC)로 저장. 프론트가 UTC로 해석함(타임존 안 붙음 주의).
- **외부 노출 식별자**: User는 내부 `id`(정수, JWT subject·FK용)와 **`public_id`(nanoid 12, API/URL 노출용)** 분리. API엔 public_id만.
- **auth-aware 엔드포인트**: `get_current_user_optional`(`routers/auth.py`)로 로그인 인지. 비로그인엔 **가려질 본문을 응답에 넣지 않는다**(코멘트 피드/다이제스트가 그렇게 동작).
- camelCase 직렬화: `Field(serialization_alias="...")` + `model_config = {"populate_by_name": True}`.
- soft delete: `company_comments.deleted_at` — 조회 시 `deleted_at.is_(None)` 필터 필수.

## 마이그레이션 / 실행
- 컨테이너 시작 커맨드 = `alembic upgrade head && uvicorn …` (docker-compose). 즉 **컨테이너 재생성 시점에 마이그레이션 적용**.
- 로컬: `docker compose up -d db` → `.venv/bin/alembic upgrade head` → `.venv/bin/uvicorn app.main:app --reload --port 8000`
- DB env는 호스트 셸에 없음 → psql은 컨테이너 안: `docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "..."'`
- 읽기전용 점검은 `.venv/bin/python`으로 `app.database.SessionLocal` 써서 빠르게.

## 라우터 (prefix)
- `routers/home.py` `/api/home`, `routers/spotlight.py` `/api/...`, `routers/companies.py` `/api/companies`,
  `routers/comments.py` `/api/comments`, `routers/search.py` `/api/search-terms`, `routers/auth.py` `/auth`
- 새 라우터는 `app/main.py`에서 `include_router` 등록.

## 데이터 (요약, 상세는 ../docs/)
- 회사 ~37만(NPS). **재무는 DART 상장사 ~2,652곳만**(`is_listed`). 비상장 재무는 DART 감사보고서 별도 파싱.
- 모델: `app/models.py`. 스키마 지도는 `../docs/db-schema.md`.

## 배포
- Mac 빌드 → save → scp → EC2 load → **`docker compose up -d --force-recreate api`** (같은 태그라 force-recreate 필수). 상세 `../docs/deploy-runbook.md`.
- ⚠️ 운영 DB 변경(마이그레이션 등)은 명령만 제공하고 **사용자가 직접 실행**.
