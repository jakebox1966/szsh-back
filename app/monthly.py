"""
매월 실행. 신규 NPS CSV 1개 반영 + 서비스 테이블 갱신.

    python -m app.monthly                             # data/nps/ 최신 CSV 자동
    python -m app.monthly --file data/nps/xxx.csv

.env 의 DART_API_KEY 있으면 신규 상장사 diff 체크도 함께 수행.
"""
import argparse
import re
import sys
from pathlib import Path

from app.config import settings


def _find_latest_csv(nps_dir: str) -> Path:
    files = sorted(Path(nps_dir).glob("국민연금 가입 사업장 내역_*.csv"))
    if not files:
        print(f"❌ CSV 없음: {nps_dir}")
        sys.exit(1)
    return files[-1]


def _already_ingested(csv_path: Path) -> bool:
    from app.database import SessionLocal
    from app.models import NpsMonthly

    m = re.search(r"_(\d{8})", csv_path.name)
    if not m:
        return False
    d = m.group(1)
    year_month = f"{d[:4]}-{d[4:6]}-01"

    db = SessionLocal()
    try:
        return (
            db.query(NpsMonthly).filter(NpsMonthly.year_month == year_month).first()
            is not None
        )
    finally:
        db.close()


def monthly(csv_file: str | None = None, min_subscribers: int = 5):
    # 1. CSV 결정
    csv_path = Path(csv_file) if csv_file else _find_latest_csv("data/nps")
    print(f"📅 대상 CSV: {csv_path.name}")

    # 2. 증분 적재
    if _already_ingested(csv_path):
        print("  → 이미 적재됨. 스킵")
    else:
        from app.ingest import ingest
        print("  → 적재 중...")
        ingest(str(csv_path), min_subscribers)

    # 3. DART 기업개황 diff (자동으로 완성된 건 스킵)
    if settings.dart_api_key:
        print("\n🏢 DART 기업개황 diff 체크...")
        from app.ingest_dart import build_map
        build_map(settings.dart_api_key)
    else:
        print("\n⚠️  DART_API_KEY 없음 → 기업개황 diff 스킵")

    # 4. 매칭 증분 (신규 NPS/DART 쌍만)
    print("\n🔗 매칭 갱신...")
    from app.match import match
    match()

    # 5. 서비스 테이블 재계산
    print("\n⚙️  compute...")
    from app.compute import compute
    compute()

    print("\n✅ 월간 갱신 완료")


def main():
    parser = argparse.ArgumentParser(description="월간 NPS 갱신")
    parser.add_argument("--file", type=str, help="적재할 CSV 경로 (기본: 최신 자동 감지)")
    parser.add_argument("--min-subscribers", type=int, default=5)
    args = parser.parse_args()
    monthly(csv_file=args.file, min_subscribers=args.min_subscribers)


if __name__ == "__main__":
    main()
