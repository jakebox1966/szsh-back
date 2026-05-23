"""
노베이스 DB 초기 세팅. 1회만 실행.

매월 갱신: python -m app.monthly
연 1회:   python -m app.yearly --year 2025

    python -m app.init_db [--force] [--skip-cache]

.env 의 DART_API_KEY 자동 사용.
"""
import argparse
import subprocess
import sys
from pathlib import Path

from app.config import settings


def _ensure_db(force: bool):
    from sqlalchemy import create_engine, text

    db_url = settings.database_url
    base = db_url.rsplit("/", 1)[0] + "/postgres"
    name = db_url.rsplit("/", 1)[1]

    eng = create_engine(base, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        if force:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :n AND pid <> pg_backend_pid()"
                ),
                {"n": name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            print(f"DB '{name}' 드롭 완료")

        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"),
            {"n": name},
        ).fetchone()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
            print(f"DB '{name}' 생성 완료")
        else:
            print(f"DB '{name}' 이미 존재")
    eng.dispose()


def _grant_privileges():
    from sqlalchemy import create_engine, text

    db_url = settings.database_url
    eng = create_engine(db_url)
    with eng.connect() as conn:
        current_user = conn.execute(text("SELECT current_user")).scalar()
        if current_user == "szsh":
            eng.dispose()
            return
    eng.dispose()

    try:
        eng = create_engine(db_url, isolation_level="AUTOCOMMIT")
        with eng.connect() as conn:
            conn.execute(text("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO szsh"))
            conn.execute(text("GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO szsh"))
            conn.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO szsh"))
            conn.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO szsh"))
        print("szsh 유저 권한 부여 완료")
    except Exception:
        pass
    finally:
        eng.dispose()


def _run_alembic():
    print("\n═══ 1. 테이블 생성 (alembic) ═══")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True, text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr)
        sys.exit(1)


def _refuse_if_already_setup(force: bool):
    if force:
        return
    from app.database import SessionLocal
    from sqlalchemy import text

    db = SessionLocal()
    try:
        existing = db.execute(text("SELECT count(*) FROM nps_monthly")).scalar() or 0
        if existing > 0:
            print(f"❌ 이미 세팅됨 (nps_monthly {existing}건)")
            print("   월간 갱신: python -m app.monthly")
            print("   완전 재세팅: python -m app.init_db --force")
            sys.exit(1)
    except Exception:
        # 테이블 없으면 그냥 진행
        pass
    finally:
        db.close()


def _ingest_nps(nps_dir: str, min_subscribers: int):
    from app.ingest import ingest

    print("\n═══ 2. 국민연금 데이터 적재 ═══")
    nps_path = Path(nps_dir)
    if not nps_path.exists():
        print(f"❌ NPS 폴더 없음: {nps_path}")
        sys.exit(1)

    files = sorted(nps_path.glob("*.csv"))
    if not files:
        print(f"❌ CSV 없음: {nps_path}")
        sys.exit(1)

    print(f"CSV {len(files)}개 발견")
    for f in files:
        print(f"\n── {f.name} ──")
        ingest(str(f), min_subscribers)


def _ingest_dart_map(api_key: str, skip_cache: bool):
    from app.ingest_dart import build_map, restore_from_csv

    print("\n═══ 3. DART 기업개황 ═══")
    backup_csv = Path("data/dart/dart_raw_backup.csv")
    if backup_csv.exists() and not skip_cache:
        print(f"🗃️  {backup_csv} 감지 → API 호출 스킵")
        restore_from_csv(str(backup_csv))
        print("\n신규 상장사 diff 체크 중...")
        build_map(api_key)
    else:
        reason = "CSV 없음" if not backup_csv.exists() else "--skip-cache 지정"
        print(f"🌐 {reason} → DART API 호출 (~30분)")
        build_map(api_key)


def _ingest_empsttus(api_key: str, years: list[int], skip_cache: bool):
    from app.ingest_dart import ingest_empsttus, restore_empsttus_from_csv

    print(f"\n═══ 4. DART empSttus (상장사 공시 연봉) ═══")
    backup_csv = Path("data/dart/dart_emp_sttus_backup.csv")
    if backup_csv.exists() and not skip_cache:
        print(f"🗃️  {backup_csv} 감지 → API 호출 2시간 스킵")
        restore_empsttus_from_csv(str(backup_csv))
    else:
        reason = "CSV 없음" if not backup_csv.exists() else "--skip-cache 지정"
        print(f"🌐 {reason} → DART API 호출 (~2시간)")
        ingest_empsttus(api_key, years)


def _run_match():
    from app.match import match

    print("\n═══ 5. NPS ↔ DART 매칭 ═══")
    match()


def _ingest_finance(folder: str):
    from app.ingest_dart import ingest_finance

    print(f"\n═══ 6. DART 손익계산서 ═══")
    ingest_finance(folder)


def _run_compute():
    from app.compute import compute

    print("\n═══ 7. 서비스 테이블 계산 ═══")
    compute()


def _seed_net_pay():
    """국세청 간이세액표 기반 세전/실수령 참고표 적재. 프론트 NetPayCard 용."""
    import csv as _csv
    from app.database import SessionLocal
    from app.models import NetPayReference
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    csv_path = Path("data/seed/net_pay_reference.csv")
    if not csv_path.exists():
        print(f"⚠️  seed CSV 없음: {csv_path} → net_pay_reference 스킵")
        return

    print("\n═══ 8. net_pay_reference seed ═══")
    db = SessionLocal()
    try:
        from sqlalchemy import text
        db.execute(text("TRUNCATE net_pay_reference"))
        with open(csv_path, "r", encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        # CSV 는 사람이 읽기 쉽게 만원 단위. DB 는 원 단위 BIGINT 통일이라 × 10,000.
        for r in rows:
            db.add(NetPayReference(
                gross=int(r["gross"]) * 10_000,
                net=int(r["net"]) * 10_000,
            ))
        db.commit()
        print(f"  net_pay_reference: {len(rows)}건 적재")
    finally:
        db.close()


def init_db(
    nps_dir: str = "data/nps",
    min_subscribers: int = 5,
    dart_folder: str = "data/dart",
    empsttus_years: list[int] | None = None,
    force: bool = False,
    skip_cache: bool = False,
):
    if not settings.dart_api_key:
        print("❌ .env 에 DART_API_KEY 를 추가해주세요")
        sys.exit(1)

    if empsttus_years is None:
        empsttus_years = [2021, 2022, 2023, 2024]

    # 이미 세팅된 DB 감지 (force 없을 때). DB 없으면 내부에서 pass
    _refuse_if_already_setup(force)

    _ensure_db(force)
    _run_alembic()
    _grant_privileges()

    _ingest_nps(nps_dir, min_subscribers)
    _ingest_dart_map(settings.dart_api_key, skip_cache)
    _ingest_empsttus(settings.dart_api_key, empsttus_years, skip_cache)
    _run_match()
    # compute 가 companies/company_summary 를 TRUNCATE + 재생성하므로
    # 재무 적재는 반드시 compute 이후에 실행해야 함 (summary.revenue 업데이트)
    _run_compute()
    _ingest_finance(dart_folder)
    _seed_net_pay()

    print("\n═══ ✅ 초기 세팅 완료 ═══")
    print("  서버 실행:  uvicorn app.main:app --reload --port 8000")
    print("  월간 갱신:  python -m app.monthly")
    print("  연간 갱신:  python -m app.yearly --year 2025")
    print("\n검수 필요 시:")
    print("  python -m app.match --export")
    print("  (CSV 에서 status 수정)")
    print("  python -m app.match --import-csv data/pending_review.csv")


def main():
    parser = argparse.ArgumentParser(description="DB 초기 세팅 (1회용)")
    parser.add_argument("--nps-dir", type=str, default="data/nps")
    parser.add_argument("--min-subscribers", type=int, default=5)
    parser.add_argument("--dart-folder", type=str, default="data/dart")
    parser.add_argument(
        "--empsttus-years",
        type=str,
        default="2021,2022,2023,2024",
        help="쉼표 구분 연도",
    )
    parser.add_argument("--force", action="store_true", help="기존 DB 드롭 후 재생성")
    parser.add_argument(
        "--skip-cache",
        action="store_true",
        help="dart_raw_backup.csv 무시하고 API 재호출",
    )
    args = parser.parse_args()

    years = [int(y.strip()) for y in args.empsttus_years.split(",")]
    init_db(
        nps_dir=args.nps_dir,
        min_subscribers=args.min_subscribers,
        dart_folder=args.dart_folder,
        empsttus_years=years,
        force=args.force,
        skip_cache=args.skip_cache,
    )


if __name__ == "__main__":
    main()
