"""
연 1회 실행. 신규 연도 DART empSttus + 손익계산서 반영.

    python -m app.yearly --year 2025
    python -m app.yearly --year 2025 --finance-folder data/dart/2025

.env 의 DART_API_KEY 필요.
"""
import argparse
import sys
from pathlib import Path

from app.config import settings


def yearly(year: int, finance_folder: str | None = None):
    if not settings.dart_api_key:
        print("❌ .env 에 DART_API_KEY 필요")
        sys.exit(1)

    # 1. empSttus (신규 연도 1개)
    print(f"\n📊 DART empSttus {year} 수집 중 (~30분)...")
    from app.ingest_dart import ingest_empsttus, export_empsttus
    ingest_empsttus(settings.dart_api_key, [year])

    # 1-1. 백업 CSV 갱신 (다음 재세팅 시 API 스킵용)
    print("\n💾 dart_emp_sttus 백업 CSV 갱신 중...")
    export_empsttus()

    # 2. 손익계산서 (폴더 있을 때만)
    if finance_folder is None:
        finance_folder = f"data/dart/{year}"

    folder_path = Path(finance_folder)
    if folder_path.exists():
        print(f"\n💰 DART 손익계산서 {year} 적재 ({folder_path})...")
        from app.ingest_dart import ingest_finance
        ingest_finance(str(folder_path))
    else:
        print(f"\n⚠️  손익계산서 폴더 없음: {folder_path}")
        print("   수동 다운로드 후 --finance-folder 로 다시 실행하거나 스킵")

    # 3. 서비스 테이블 재계산
    print("\n⚙️  compute 재실행...")
    from app.compute import compute
    compute()

    print("\n✅ 연간 갱신 완료")


def main():
    parser = argparse.ArgumentParser(description="연간 DART 갱신")
    parser.add_argument("--year", type=int, required=True, help="반영할 사업연도 (예: 2025)")
    parser.add_argument(
        "--finance-folder",
        type=str,
        default=None,
        help="손익계산서 TXT 폴더 (기본: data/dart/{year})",
    )
    args = parser.parse_args()
    yearly(year=args.year, finance_folder=args.finance_folder)


if __name__ == "__main__":
    main()
