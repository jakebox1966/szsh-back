"""
DART 데이터 → DB 적재 스크립트.

사용법:
    # 매핑 테이블 구축 (최초 1회, 이후 신규 상장사만 추가)
    python -m app.ingest_dart build-map --api-key YOUR_API_KEY

    # dart_raw_backup.csv 로 빠르게 복구 (API 스킵)
    python -m app.ingest_dart restore-from-csv --csv data/dart/dart_raw_backup.csv

    # DART 직원현황(empSttus) 적재 - 상장사 공시 연봉
    python -m app.ingest_dart empsttus --api-key YOUR_API_KEY

    # 손익계산서 적재
    python -m app.ingest_dart ingest --folder /path/to/2024_4Q_PL_.../

    # 둘 다 한번에
    python -m app.ingest_dart all --api-key YOUR_API_KEY --folder /path/to/2024_4Q_PL_.../
"""
import argparse
import csv
import io
import os
import re
import sys
import tempfile
import time
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import requests
from sqlalchemy.dialects.postgresql import insert as pg_insert


@contextmanager
def _atomic_csv_open(target_path: Path):
    """
    target_path 에 CSV 를 atomic 하게 쓰기.

    동일 디렉토리의 tempfile 에 먼저 쓰고, 정상 완료 시에만 os.replace 로 교체.
    프로세스 중단 시 기존 파일은 손상되지 않음 (tempfile 만 버려짐).

    with _atomic_csv_open(Path("data/out.csv")) as f:
        writer = csv.writer(f)
        writer.writerow(...)
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", newline="", encoding="utf-8-sig",
        dir=target_path.parent, delete=False, suffix=".tmp",
    )
    tmp_path = Path(tmp.name)
    try:
        yield tmp
        tmp.flush()
        tmp.close()
        os.replace(tmp_path, target_path)
    except Exception:
        tmp.close()
        tmp_path.unlink(missing_ok=True)
        raise

from app.database import engine, SessionLocal, Base
from app.models import (
    DartRaw, DartEmpSttus, DartFinanceRaw,
    Company, CompanyFinance, CompanyFinanceHistory, NpsRaw,
)

DART_API_BASE = "https://opendart.fss.or.kr/api"

# 손익계산서에서 뽑을 항목코드
TARGET_ACCOUNTS = {
    "ifrs-full_Revenue": "revenue",
    "dart_Revenue": "revenue",
    "dart_TotalSellingGeneralAdministrativeExpenses": None,  # skip
    "dart_OperatingIncomeLoss": "operating_profit",
    "ifrs-full_ProfitLoss": "net_profit",
}


# ── Phase 2: 매핑 테이블 구축 ────────────────────────────────────


def build_map(api_key: str, corpcode_file: str | None = None):
    """CORPCODE.xml 파싱 → 기업개황 API 호출 → dart_corp_map 테이블 구축."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    try:
        # 1) CORPCODE.xml 로드 (로컬 파일 또는 API 다운로드)
        if corpcode_file:
            print(f"CORPCODE.xml 로컬 파일 로딩: {corpcode_file}")
            tree = ET.parse(corpcode_file)
            root = tree.getroot()
        else:
            print("CORPCODE.xml 다운로드 중...")
            resp = requests.get(
                f"{DART_API_BASE}/corpCode.xml",
                params={"crtfc_key": api_key},
            )
            if resp.status_code != 200:
                print(f"CORPCODE.xml 다운로드 실패: HTTP {resp.status_code}")
                sys.exit(1)

            z = zipfile.ZipFile(io.BytesIO(resp.content))
            xml_content = z.read("CORPCODE.xml")
            root = ET.fromstring(xml_content)

        # 상장사만 추출 (stock_code가 있는 것)
        corps = []
        for item in root.findall("list"):
            stock_code = (item.findtext("stock_code") or "").strip()
            if stock_code:
                corps.append({
                    "corp_code": item.findtext("corp_code", "").strip(),
                    "corp_name": item.findtext("corp_name", "").strip(),
                    "stock_code": stock_code,
                })

        print(f"상장사: {len(corps)}개")

        # raw_json 까지 채워진 건만 스킵 (이전 스키마에서 기본 필드만 받은 row 는 재호출 대상)
        existing_complete = set(
            r[0] for r in db.query(DartRaw.corp_code).filter(
                DartRaw.raw_json.isnot(None),
            ).all()
        )
        new_corps = [c for c in corps if c["corp_code"] not in existing_complete]
        print(f"API 호출 대상: {len(new_corps)}개 (raw_json 보유 기존: {len(existing_complete)}개)")

        if not new_corps:
            print("호출할 건이 없습니다.")
            return

        # 2) 기업개황 API 호출 — 응답 필드 전부 보존 (raw_json 포함)
        print(f"기업개황 API 호출 중 ({len(new_corps)}건, 예상 {len(new_corps) * 0.5 / 60:.0f}분)...")
        success = 0
        fail = 0

        csv_path = Path("data/dart/dart_raw_backup.csv")
        # atomic 쓰기: 기존 backup 은 루프가 끝난 뒤에야 교체됨 (중간 kill 시 보존)
        with _atomic_csv_open(csv_path) as csv_file:
            csv_writer = csv.writer(csv_file)
            csv_writer.writerow([
                "corp_code", "corp_name", "stock_name", "stock_code",
                "bizr_no", "jurir_no", "ceo_nm", "corp_cls",
                "address", "eng_name", "website", "ir_url",
                "phn_no", "fax_no", "induty_code", "est_dt", "acc_mt",
            ])

            for idx, corp in enumerate(new_corps):
                try:
                    resp = requests.get(
                        f"{DART_API_BASE}/company.json",
                        params={
                            "crtfc_key": api_key,
                            "corp_code": corp["corp_code"],
                        },
                        timeout=30,
                    )
                    data = resp.json()

                    fields = {
                        "corp_name": corp["corp_name"],
                        "stock_name": None, "stock_code": corp["stock_code"],
                        "bizr_no": None, "jurir_no": None, "ceo_nm": None, "corp_cls": None,
                        "address": None, "eng_name": None, "website": None, "ir_url": None,
                        "phn_no": None, "fax_no": None, "induty_code": None,
                        "est_dt": None, "acc_mt": None,
                    }
                    raw_json = None
                    if data.get("status") == "000":
                        # status/message 같은 메타는 제외하고 응답 본문만 raw_json 으로 저장
                        raw_json = {k: v for k, v in data.items() if k not in ("status", "message")}
                        # corp_name 은 API 가 더 정확할 수 있음
                        fields["corp_name"] = (data.get("corp_name") or "").strip() or corp["corp_name"]
                        fields["stock_name"] = (data.get("stock_name") or "").strip() or None
                        # API stock_code 가 비어 있으면 CORPCODE.xml 값 유지
                        api_stock = (data.get("stock_code") or "").strip()
                        if api_stock:
                            fields["stock_code"] = api_stock
                        fields["bizr_no"] = (data.get("bizr_no") or "").strip() or None
                        fields["jurir_no"] = (data.get("jurir_no") or "").strip() or None
                        fields["ceo_nm"] = (data.get("ceo_nm") or "").strip() or None
                        fields["corp_cls"] = (data.get("corp_cls") or "").strip() or None
                        fields["address"] = (data.get("adres") or "").strip() or None
                        fields["eng_name"] = (data.get("corp_name_eng") or "").strip() or None
                        fields["website"] = (data.get("hm_url") or "").strip() or None
                        fields["ir_url"] = (data.get("ir_url") or "").strip() or None
                        fields["phn_no"] = (data.get("phn_no") or "").strip() or None
                        fields["fax_no"] = (data.get("fax_no") or "").strip() or None
                        fields["induty_code"] = (data.get("induty_code") or "").strip() or None
                        fields["acc_mt"] = (data.get("acc_mt") or "").strip() or None
                        # est_dt: YYYYMMDD → Date
                        est_dt_str = (data.get("est_dt") or "").strip()
                        if len(est_dt_str) == 8 and est_dt_str.isdigit():
                            try:
                                fields["est_dt"] = date(
                                    int(est_dt_str[:4]),
                                    int(est_dt_str[4:6]),
                                    int(est_dt_str[6:8]),
                                )
                            except ValueError:
                                fields["est_dt"] = None

                    stmt = pg_insert(DartRaw).values(
                        corp_code=corp["corp_code"],
                        raw_json=raw_json,
                        fetched_at=datetime.utcnow(),
                        **fields,
                    )
                    stmt = stmt.on_conflict_do_update(
                        index_elements=["corp_code"],
                        set_={
                            **fields,
                            "raw_json": raw_json,
                            "fetched_at": datetime.utcnow(),
                        },
                    )
                    db.execute(stmt)
                    csv_writer.writerow([
                        corp["corp_code"], fields["corp_name"], fields["stock_name"] or "",
                        fields["stock_code"] or "",
                        fields["bizr_no"] or "", fields["jurir_no"] or "",
                        fields["ceo_nm"] or "", fields["corp_cls"] or "",
                        fields["address"] or "", fields["eng_name"] or "",
                        fields["website"] or "", fields["ir_url"] or "",
                        fields["phn_no"] or "", fields["fax_no"] or "",
                        fields["induty_code"] or "",
                        fields["est_dt"].isoformat() if fields["est_dt"] else "",
                        fields["acc_mt"] or "",
                    ])
                    success += 1

                except Exception as e:
                    print(f"  오류 [{corp['corp_code']}] {corp['corp_name']}: {e}")
                    fail += 1

                if (idx + 1) % 100 == 0:
                    db.flush()
                    print(f"  진행: {idx + 1}/{len(new_corps)} (성공: {success}, 실패: {fail})")

                time.sleep(0.5)  # API rate limit 방지
            # with 블록 종료 시점에 tempfile → csv_path 로 atomic rename

        db.commit()
        mapped = db.query(DartRaw).filter(DartRaw.bizr_no.isnot(None)).count()
        print(f"\n완료! 성공: {success}, 실패: {fail}")
        print(f"전체 매핑 현황: 사업자등록번호 있음 {mapped}건")
        print(f"CSV 백업: {csv_path}")

    except Exception as e:
        db.rollback()
        print(f"오류 발생: {e}")
        raise
    finally:
        db.close()


# ── restore-from-csv: dart_raw_backup.csv 로 빠르게 복구 ──────────


def restore_from_csv(csv_path: str):
    """dart_raw_backup.csv → dart_raw UPSERT. build-map API 호출(~30분) 대체용."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    try:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = [
                {k: (v.strip() or None) for k, v in row.items()}
                for row in reader
            ]

        print(f"CSV 로드: {len(rows)}건")

        for row in rows:
            stmt = pg_insert(DartRaw).values(**row).on_conflict_do_update(
                index_elements=["corp_code"],
                set_={
                    k: row[k] for k in [
                        "corp_name", "stock_code",
                        "bizr_no", "address", "eng_name", "website",
                    ]
                },
            )
            db.execute(stmt)

        db.commit()
        total = db.query(DartRaw).count()
        print(f"복구 완료: dart_raw {total}건")
    except Exception as e:
        db.rollback()
        print(f"오류: {e}")
        raise
    finally:
        db.close()


# ── empSttus: export / restore (API 호출 2시간 스킵용) ───────────


def export_empsttus(csv_path: str = "data/dart/dart_emp_sttus_backup.csv"):
    """dart_emp_sttus → CSV 덤프. 재세팅 시 API 재호출 대신 사용."""
    db = SessionLocal()
    try:
        rows = (
            db.query(DartEmpSttus, DartRaw.corp_code)
            .join(DartRaw, DartEmpSttus.dart_id == DartRaw.id)
            .order_by(DartRaw.corp_code, DartEmpSttus.year, DartEmpSttus.sex)
            .all()
        )

        with _atomic_csv_open(Path(csv_path)) as f:
            writer = csv.writer(f)
            writer.writerow([
                "corp_code", "year", "sex", "business_unit",
                "regular_count", "regular_part_count",
                "contract_count", "contract_part_count",
                "total_count", "annual_salary_total",
                "avg_annual_salary", "avg_tenure_months",
                "avg_tenure_raw", "remark",
            ])
            for emp, corp_code in rows:
                writer.writerow([
                    corp_code, emp.year, emp.sex or "", emp.business_unit or "",
                    emp.regular_count or "", emp.regular_part_count or "",
                    emp.contract_count or "", emp.contract_part_count or "",
                    emp.total_count or "", emp.annual_salary_total or "",
                    emp.avg_annual_salary or "", emp.avg_tenure_months or "",
                    emp.avg_tenure_raw or "", emp.remark or "",
                ])
        print(f"✅ 백업 완료: {csv_path} ({len(rows)}건)")
    finally:
        db.close()


def restore_empsttus_from_csv(csv_path: str = "data/dart/dart_emp_sttus_backup.csv"):
    """CSV → dart_emp_sttus UPSERT. build-map 이 먼저 실행되어 dart_raw 존재 필수."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    try:
        # corp_code → dart_id 매핑 로드
        corp_map = {r.corp_code: r.id for r in db.query(DartRaw.corp_code, DartRaw.id).all()}
        if not corp_map:
            print("❌ dart_raw 비어있음. build-map 또는 restore-from-csv 먼저 실행")
            return

        with open(csv_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        print(f"CSV 로드: {len(rows)}건")

        inserted = 0
        skipped = 0
        for row in rows:
            dart_id = corp_map.get(row["corp_code"])
            if not dart_id:
                skipped += 1
                continue

            def _nullable_int(v):
                v = (v or "").strip()
                return int(v) if v else None

            stmt = pg_insert(DartEmpSttus).values(
                dart_id=dart_id,
                year=row["year"],
                sex=row["sex"].strip() or None,
                regular_count=_nullable_int(row.get("regular_count")),
                contract_count=_nullable_int(row.get("contract_count")),
                total_count=_nullable_int(row.get("total_count")),
                annual_salary_total=_nullable_int(row.get("annual_salary_total")),
                avg_annual_salary=_nullable_int(row.get("avg_annual_salary")),
                avg_tenure_months=_nullable_int(row.get("avg_tenure_months")),
            ).on_conflict_do_update(
                index_elements=["dart_id", "year", "sex"],
                set_={
                    "regular_count": _nullable_int(row.get("regular_count")),
                    "contract_count": _nullable_int(row.get("contract_count")),
                    "total_count": _nullable_int(row.get("total_count")),
                    "annual_salary_total": _nullable_int(row.get("annual_salary_total")),
                    "avg_annual_salary": _nullable_int(row.get("avg_annual_salary")),
                    "avg_tenure_months": _nullable_int(row.get("avg_tenure_months")),
                },
            )
            db.execute(stmt)
            inserted += 1

            if inserted % 2000 == 0:
                db.commit()
                print(f"  진행: {inserted}/{len(rows)}")

        db.commit()
        total = db.query(DartEmpSttus).count()
        print(f"✅ 복구 완료: 반영 {inserted}건, 스킵 {skipped}건 (corp_code 없음)")
        print(f"   dart_emp_sttus 총 {total}건")
    except Exception as e:
        db.rollback()
        print(f"오류: {e}")
        raise
    finally:
        db.close()


# ── empSttus: DART 직원현황 수집 ─────────────────────────────────


EMPSTTUS_URL = f"{DART_API_BASE}/empSttus.json"


def _parse_tenure(tenure_str: str | None) -> int | None:
    """'13년 4개월' → 160 (개월수). '13.3' 같은 float 포맷도 허용."""
    if not tenure_str:
        return None
    s = tenure_str.strip()
    if not s or s == "-":
        return None
    m = re.match(r"(\d+)\s*년\s*(?:(\d+)\s*개월)?", s)
    if m:
        years = int(m.group(1))
        months = int(m.group(2) or 0)
        return years * 12 + months
    try:
        return int(float(s) * 12)
    except (ValueError, TypeError):
        return None


def ingest_empsttus(api_key: str, years: list[int]):
    """DART empSttus API → dart_emp_sttus UPSERT.
    상장사 × 연도 = API 호출. 1초당 2회 기준 ~2시간 소요 (4년치)."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    corps = db.query(DartRaw).filter(DartRaw.stock_code.isnot(None)).all()
    total = len(corps) * len(years)
    print(f"empSttus 대상: 상장사 {len(corps)} × 연도 {len(years)} = {total} API 호출")
    print(f"예상 소요: 약 {total * 0.5 / 60:.0f}분")

    success = 0
    fail = 0
    done = 0

    csv_path = Path("data/dart/dart_emp_sttus_backup.csv")
    try:
        with _atomic_csv_open(csv_path) as csv_file:
            csv_writer = csv.writer(csv_file)
            csv_writer.writerow([
                "corp_code", "year", "sex", "business_unit",
                "regular_count", "regular_part_count",
                "contract_count", "contract_part_count",
                "total_count", "annual_salary_total",
                "avg_annual_salary", "avg_tenure_months",
                "avg_tenure_raw", "remark",
            ])

            for corp in corps:
                for year in years:
                    done += 1
                    try:
                        resp = requests.get(
                            EMPSTTUS_URL,
                            params={
                                "crtfc_key": api_key,
                                "corp_code": corp.corp_code,
                                "bsns_year": str(year),
                                "reprt_code": "11011",
                            },
                            timeout=30,
                        )
                        data = resp.json()

                        if data.get("status") != "000":
                            # 미공시/미상장 케이스는 정상. 로그 불필요
                            continue

                        # 응답엔 bsns_year 필드 없음. 요청 파라미터로 쓴 year 사용
                        year_str = str(year)

                        for row in data.get("list", []):
                            sex_raw = row.get("sexdstn")
                            sex = sex_raw if sex_raw and sex_raw != "-" else None

                            bu_raw = row.get("fo_bbm")
                            business_unit = bu_raw if bu_raw and bu_raw != "-" else None

                            regular_count = _parse_amount(row.get("rgllbr_co"))
                            regular_part = _parse_amount(row.get("rgllbr_abacpt_labrr_co"))
                            contract_count = _parse_amount(row.get("cnttk_co"))
                            contract_part = _parse_amount(row.get("cnttk_abacpt_labrr_co"))
                            total_count = _parse_amount(row.get("sm"))
                            annual_total = _parse_amount(row.get("fyer_salary_totamt"))
                            per_capita_won = _parse_amount(row.get("jan_salary_am"))
                            tenure_raw = row.get("avrg_cnwk_sdytrn")
                            tenure = _parse_tenure(tenure_raw)
                            remark = row.get("rm")
                            if remark and remark.strip() in ("", "-"):
                                remark = None

                            # UNIQUE (dart_id, year, sex, business_unit) — 부문별 공시 회사 분리 보존
                            stmt = pg_insert(DartEmpSttus).values(
                                dart_id=corp.id,
                                year=year_str,
                                sex=sex,
                                business_unit=business_unit,
                                regular_count=regular_count,
                                regular_part_count=regular_part,
                                contract_count=contract_count,
                                contract_part_count=contract_part,
                                total_count=total_count,
                                annual_salary_total=annual_total,
                                avg_annual_salary=per_capita_won,
                                avg_tenure_months=tenure,
                                avg_tenure_raw=tenure_raw,
                                remark=remark,
                                raw_json=row,
                            ).on_conflict_do_update(
                                constraint="uq_emp_sttus",
                                set_={
                                    "regular_count": regular_count,
                                    "regular_part_count": regular_part,
                                    "contract_count": contract_count,
                                    "contract_part_count": contract_part,
                                    "total_count": total_count,
                                    "annual_salary_total": annual_total,
                                    "avg_annual_salary": per_capita_won,
                                    "avg_tenure_months": tenure,
                                    "avg_tenure_raw": tenure_raw,
                                    "remark": remark,
                                    "raw_json": row,
                                },
                            )
                            db.execute(stmt)
                            csv_writer.writerow([
                                corp.corp_code, year_str, sex or "", business_unit or "",
                                regular_count or "", regular_part or "",
                                contract_count or "", contract_part or "",
                                total_count or "", annual_total or "",
                                per_capita_won or "", tenure or "",
                                tenure_raw or "", remark or "",
                            ])

                        success += 1
                    except Exception as e:
                        print(f"  오류 [{corp.corp_code}] {corp.corp_name} {year}: {e}")
                        fail += 1

                    if done % 200 == 0:
                        db.commit()
                        print(f"  진행: {done}/{total} (성공 {success}, 실패 {fail})")

                    time.sleep(0.5)  # DART API rate limit

        db.commit()
        total_rows = db.query(DartEmpSttus).count()
        print(f"\n완료: 성공 {success}, 실패 {fail}")
        print(f"dart_emp_sttus 총 {total_rows}건")
    except Exception as e:
        db.rollback()
        print(f"오류 발생: {e}")
        raise
    finally:
        db.close()


# ── 손익계산서 raw 적재 ─────────────────────────────────────────


def _parse_stock_code(raw: str) -> str | None:
    """파일에 들어온 '[001040]' → '001040'."""
    if not raw:
        return None
    s = raw.strip().strip("[]")
    return s if s else None


def _parse_date_iso(s: str) -> date | None:
    """'2024-12-31' → date. 빈/이상 값은 None."""
    if not s:
        return None
    s = s.strip()
    parts = s.split("-")
    if len(parts) != 3:
        return None
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, TypeError):
        return None


# 파일명 패턴: "2024_사업보고서_02_손익계산서_20260326.txt"
_FNAME_RE = re.compile(r"^(\d{4})_([^_]+)_(\d{2})_([^_]+?)(?:_([^_]+))?_(\d{8})\.txt$")


def _parse_filename(name: str) -> tuple[str, str, str | None] | None:
    """파일명 → (bsns_year, report_type, statement_type_suffix). 매칭 실패 시 None.

    예) 2024_사업보고서_02_손익계산서_20260326.txt → ('2024', '사업보고서', None)
        2024_사업보고서_03_포괄손익계산서_20260326.txt → ('2024', '사업보고서', '포괄손익계산서')
    """
    m = _FNAME_RE.match(name)
    if not m:
        return None
    return (m.group(1), m.group(2), m.group(4))


def ingest_finance_raw(folder: str):
    """손익계산서 TXT 파일을 dart_finance_raw 에 그대로 보존.
    파일의 모든 항목(매출/원가/판관비/영업이익/세전이익/순이익/주당이익 등)을 1행씩 적재.

    company_finance 같은 가공 테이블은 compute.py 에서 dart_finance_raw 를 읽어 생성.
    """
    folder_path = Path(folder)
    if not folder_path.exists():
        print(f"폴더를 찾을 수 없습니다: {folder_path}")
        sys.exit(1)

    # 하위 연도 폴더 자동 탐색 (예: data/dart/2024, 2025)
    search_dirs: list[Path] = []
    sub_dirs = sorted([d for d in folder_path.iterdir() if d.is_dir() and d.name.isdigit()])
    search_dirs = sub_dirs if sub_dirs else [folder_path]

    # 별도 손익계산서 + 포괄손익계산서만 — raw 단계에서도 일반업종 별도 만 받음
    # (연결/금융업종은 회계기준 달라 raw 에도 섞지 않음. 필요하면 별도 함수로 분리 가능)
    target_files: list[Path] = []
    for search_dir in search_dirs:
        for f in sorted(search_dir.iterdir()):
            name = unicodedata.normalize("NFC", f.name)
            if not name.endswith(".txt"):
                continue
            if "_연결_" in name:
                continue
            # 금융업 별도재무제표 (_금융기타/_보험/_은행/_증권)도 적재 — compute 가공 시 별도 매핑
            if "_02_손익계산서_" in name or "_03_포괄손익계산서_" in name:
                target_files.append(f)

    if not target_files:
        print("손익계산서/포괄손익계산서 파일을 찾을 수 없습니다.")
        sys.exit(1)

    print(f"대상 파일 {len(target_files)}개:")
    for f in target_files:
        print(f"  {f.name}")

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # stock_code → dart_id 매핑 미리 로드
    stock_to_dart = {
        sc: did for sc, did in
        db.query(DartRaw.stock_code, DartRaw.id).filter(DartRaw.stock_code.isnot(None)).all()
    }
    print(f"DartRaw 매핑: {len(stock_to_dart)}개 stock_code")

    BATCH = 1000
    total_loaded = 0
    total_skipped = 0

    try:
        for fpath in target_files:
            fname = unicodedata.normalize("NFC", fpath.name)
            meta = _parse_filename(fname)
            if meta is None:
                print(f"  ⚠ 파일명 패턴 미스: {fname} → 스킵")
                continue
            f_year, f_report, f_stmt_suffix = meta

            # 인코딩 자동 감지
            text = None
            for enc in ("cp949", "euc-kr", "utf-8-sig", "utf-8"):
                try:
                    text = fpath.read_text(encoding=enc)
                    print(f"  {fname} (enc={enc})")
                    break
                except UnicodeDecodeError:
                    continue
            if text is None:
                print(f"  ⚠ 인코딩 미상: {fname} → 스킵")
                continue

            lines = text.splitlines()
            if len(lines) < 2:
                continue

            # 파일 타입에 따라 컬럼 인덱스 다름:
            # - 일반업종 (.txt): 19 컬럼, 당기=14번째(idx 13), 전기=17(idx 16), 전전기=18(idx 17)
            # - 금융업 (_금융기타/_보험/_은행/_증권): 16 컬럼, 당기=13(idx 12), 전기=14(idx 13), 전전기=15(idx 14)
            is_financial = any(x in fname for x in ["_금융기타", "_보험", "_은행", "_증권"])
            if is_financial:
                min_cols = 15
                cur_idx, prev_idx, prev2_idx = 12, 13, 14
            else:
                min_cols = 18
                cur_idx, prev_idx, prev2_idx = 13, 16, 17

            buffer: list[dict] = []
            file_loaded = 0
            file_skipped = 0
            for line in lines[1:]:  # 헤더 스킵
                cells = line.split("\t")
                if len(cells) < min_cols:
                    file_skipped += 1
                    continue

                stock_code = _parse_stock_code(cells[1])
                if not stock_code:
                    file_skipped += 1
                    continue

                statement_type = cells[0].strip() or None
                market_div = cells[3].strip() or None
                industry_code = cells[4].strip() or None
                industry_name = cells[5].strip() or None
                acc_mt = cells[6].strip() or None
                end_dt = _parse_date_iso(cells[7])
                report_type = cells[8].strip() or f_report
                currency = cells[9].strip() or None
                account_id = cells[10].strip() or None
                account_nm = cells[11].strip() or None
                cur_amt = _parse_amount(cells[cur_idx])
                prev_amt = _parse_amount(cells[prev_idx])
                prev2_amt = _parse_amount(cells[prev2_idx])

                if not account_id:
                    file_skipped += 1
                    continue

                buffer.append({
                    "dart_id": stock_to_dart.get(stock_code),
                    "stock_code": stock_code,
                    "bsns_year": f_year,
                    "report_type": report_type,
                    "statement_type": statement_type,
                    "market_div": market_div,
                    "industry_code": industry_code,
                    "industry_name": industry_name,
                    "acc_mt": acc_mt,
                    "end_dt": end_dt,
                    "currency": currency,
                    "account_id": account_id,
                    "account_nm": account_nm,
                    "current_amount": cur_amt,
                    "prev_amount": prev_amt,
                    "prev2_amount": prev2_amt,
                    "ingested_at": datetime.utcnow(),
                })

                if len(buffer) >= BATCH:
                    _upsert_finance_raw(db, buffer)
                    file_loaded += len(buffer)
                    buffer = []

            if buffer:
                _upsert_finance_raw(db, buffer)
                file_loaded += len(buffer)

            db.commit()
            total_loaded += file_loaded
            total_skipped += file_skipped
            print(f"    적재 {file_loaded}건, 스킵 {file_skipped}건")

        print(f"\n완료! 총 {total_loaded}건 적재, {total_skipped}건 스킵")

    except Exception as e:
        db.rollback()
        print(f"오류 발생: {e}")
        raise
    finally:
        db.close()


def _upsert_finance_raw(db, rows: list[dict]):
    """배치 UPSERT — UNIQUE(stock_code, bsns_year, report_type, statement_type, account_id) 기준."""
    if not rows:
        return
    # 배치 내 dedup: 같은 unique key 중복 시 마지막 row 유지 (PG ON CONFLICT는 한 statement 안 중복 거부)
    dedup: dict[tuple, dict] = {}
    for r in rows:
        key = (
            r["stock_code"], r["bsns_year"], r["report_type"],
            r["statement_type"], r["account_id"],
        )
        dedup[key] = r
    rows = list(dedup.values())
    stmt = pg_insert(DartFinanceRaw).values(rows)
    update_cols = {
        "dart_id": stmt.excluded.dart_id,
        "market_div": stmt.excluded.market_div,
        "industry_code": stmt.excluded.industry_code,
        "industry_name": stmt.excluded.industry_name,
        "acc_mt": stmt.excluded.acc_mt,
        "end_dt": stmt.excluded.end_dt,
        "currency": stmt.excluded.currency,
        "account_nm": stmt.excluded.account_nm,
        "current_amount": stmt.excluded.current_amount,
        "prev_amount": stmt.excluded.prev_amount,
        "prev2_amount": stmt.excluded.prev2_amount,
        "ingested_at": stmt.excluded.ingested_at,
    }
    stmt = stmt.on_conflict_do_update(constraint="uq_dart_finance_raw", set_=update_cols)
    db.execute(stmt)


# ── 손익계산서 → company_finance 적재 (기존, deprecated 예정) ─────


def _parse_amount(val: str) -> int | None:
    """금액 문자열을 정수로 변환."""
    if not val:
        return None
    s = val.strip().replace(",", "")
    if not s or s == "-":
        return None
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def ingest_finance(folder: str):
    """손익계산서 TXT 파일에서 매출/영업이익/순이익 추출 → company_finance에 적재."""
    folder_path = Path(folder)
    if not folder_path.exists():
        print(f"폴더를 찾을 수 없습니다: {folder_path}")
        sys.exit(1)

    # 별도 손익계산서 + 포괄손익계산서 파일 찾기 (연결/금융 제외)
    # 하위 연도 폴더가 있으면 재귀 탐색
    search_dirs = []
    sub_dirs = sorted([d for d in folder_path.iterdir() if d.is_dir() and d.name.isdigit()])
    if sub_dirs:
        search_dirs = sub_dirs
    else:
        search_dirs = [folder_path]

    target_files = []
    for search_dir in search_dirs:
        for f in sorted(search_dir.iterdir()):
            name = unicodedata.normalize("NFC", f.name)
            if not name.endswith(".txt"):
                continue
            if "_연결_" in name:
                continue
            # 금융업 별도재무제표 (_금융기타/_보험/_은행/_증권)도 적재 — compute 가공 시 별도 매핑
            if "_02_손익계산서_" in name or "_03_포괄손익계산서_" in name:
                target_files.append(f)

    if not target_files:
        print("손익계산서/포괄손익계산서 파일을 찾을 수 없습니다.")
        sys.exit(1)

    print(f"대상 파일 {len(target_files)}개:")
    for f in target_files:
        print(f"  {f.name}")

    # 파일 파싱: (종목코드, 연도) → {revenue, operating_profit, net_profit}
    finance_data: dict[tuple[str, str], dict] = {}  # (stock_code, year) → data

    for filepath in target_files:
        for enc in ["cp949", "euc-kr", "utf-8"]:
            try:
                with open(filepath, "r", encoding=enc) as f:
                    lines = f.readlines()
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
        else:
            print(f"  인코딩 실패: {filepath.name}")
            continue

        for line in lines[1:]:  # 헤더 스킵
            parts = line.split("\t")
            if len(parts) < 15:
                continue

            stock_code = parts[1].strip().strip("[]")
            account_code = parts[10].strip() if len(parts) > 10 else ""
            year_end = parts[7].strip() if len(parts) > 7 else ""  # 결산기준일

            # 당기 금액
            amount_str = parts[13].strip() if len(parts) > 13 else ""
            amount = _parse_amount(amount_str)

            # 전기 금액
            prev_amount_str = parts[16].strip() if len(parts) > 16 else ""
            prev_amount = _parse_amount(prev_amount_str)

            if account_code not in TARGET_ACCOUNTS:
                continue
            field = TARGET_ACCOUNTS[account_code]
            if field is None:
                continue

            year = year_end[:4] if year_end else None
            if not year:
                continue
            key = (stock_code, year)

            if key not in finance_data:
                finance_data[key] = {
                    "year": year,
                    "revenue": None,
                    "operating_profit": None,
                    "net_profit": None,
                    "prev_revenue": None,
                    "prev_operating_profit": None,
                    "prev_net_profit": None,
                }

            d = finance_data[key]
            if d[field] is None:  # 첫 매칭만 사용 (중복 방지)
                d[field] = amount
                d[f"prev_{field}"] = prev_amount

    print(f"\n파싱된 회사: {len(finance_data)}개")

    # DB 적재
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    try:
        # dart_raw에서 종목코드 → dart_id 매핑
        stock_to_dart_id = {
            r.stock_code: r.id
            for r in db.query(DartRaw).filter(DartRaw.stock_code.isnot(None)).all()
        }
        print(f"DART 종목: {len(stock_to_dart_id)}건")

        # mapping 테이블에서 dart_id → nps_id (matched만)
        from app.models import Mapping
        dart_to_nps = {
            r.dart_id: r.nps_id
            for r in db.query(Mapping).filter(Mapping.status == "matched").all()
        }
        print(f"매칭된 매핑: {len(dart_to_nps)}건")

        # companies에서 nps_id → company_id
        company_map = {
            r.nps_id: r.id
            for r in db.query(Company.nps_id, Company.id).filter(Company.nps_id.isnot(None)).all()
        }

        matched = 0
        unmatched_stocks = []
        no_mapping = 0
        no_company = 0

        for (stock_code, _year), data in finance_data.items():
            dart_id = stock_to_dart_id.get(stock_code)
            if not dart_id:
                unmatched_stocks.append(stock_code)
                continue

            nps_id = dart_to_nps.get(dart_id)
            if not nps_id:
                no_mapping += 1
                continue

            company_id = company_map.get(nps_id)
            if not company_id:
                no_company += 1
                continue

            year = data.get("year")
            revenue = data.get("revenue")
            op_profit = data.get("operating_profit")
            net_profit = data.get("net_profit")

            # 성장률 계산 (Numeric(5,2) 범위: -999.99 ~ 999.99)
            def _growth(cur, prev):
                if cur is not None and prev is not None and prev != 0:
                    val = round((cur - prev) / abs(prev) * 100, 2)
                    return max(min(val, 999.99), -999.99)
                return None

            rev_growth = _growth(revenue, data.get("prev_revenue"))
            op_growth = _growth(op_profit, data.get("prev_operating_profit"))
            net_growth = _growth(net_profit, data.get("prev_net_profit"))

            # 금액 컬럼은 모두 원 단위 BIGINT 로 저장 (표시 단위 변환은 프론트에서)
            # company_finance UPSERT
            existing = db.query(CompanyFinance).filter(
                CompanyFinance.company_id == company_id,
                CompanyFinance.year == year,
            ).first()

            if existing:
                existing.revenue_current = revenue
                existing.revenue_growth = rev_growth
                existing.operating_profit_current = op_profit
                existing.operating_profit_growth = op_growth
                existing.net_profit_current = net_profit
                existing.net_profit_growth = net_growth
            else:
                db.add(CompanyFinance(
                    company_id=company_id,
                    revenue_current=revenue,
                    revenue_growth=rev_growth,
                    operating_profit_current=op_profit,
                    operating_profit_growth=op_growth,
                    net_profit_current=net_profit,
                    net_profit_growth=net_growth,
                    year=year,
                ))

            # company_finance_history UPSERT
            existing_hist = db.query(CompanyFinanceHistory).filter(
                CompanyFinanceHistory.company_id == company_id,
                CompanyFinanceHistory.year == year,
            ).first()

            if existing_hist:
                existing_hist.rev = revenue
                existing_hist.profit = net_profit
            else:
                db.add(CompanyFinanceHistory(
                    company_id=company_id,
                    year=year,
                    rev=revenue,
                    profit=net_profit,
                ))

            matched += 1

        # company_summary.revenue 업데이트 (최신 연도 매출)
        # Raw SQL 로 한 방에 UPDATE. 이전 ORM 루프 방식은 session flush 타이밍 이슈로 0건 업데이트됨
        print("company_summary.revenue 업데이트 중...")
        from sqlalchemy import text
        db.flush()  # 방금 insert 한 company_finance 가 DB 에 반영되도록
        result = db.execute(text("""
            UPDATE company_summary cs
            SET revenue = latest.revenue_current
            FROM (
                SELECT DISTINCT ON (company_id) company_id, revenue_current
                FROM company_finance
                WHERE revenue_current IS NOT NULL
                ORDER BY company_id, year DESC
            ) latest
            WHERE cs.company_id = latest.company_id
        """))
        rev_updated = result.rowcount
        db.commit()
        print(f"\n완료!")
        print(f"  재무 적재: {matched}건")
        print(f"  summary.revenue 업데이트: {rev_updated}건")
        print(f"  종목코드 없음: {len(unmatched_stocks)}건")
        print(f"  NPS 매핑 없음: {no_mapping}건")
        print(f"  companies 없음: {no_company}건")

    except Exception as e:
        db.rollback()
        print(f"오류 발생: {e}")
        raise
    finally:
        db.close()


# ── CLI ──────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="DART 데이터 → DB 적재")
    sub = parser.add_subparsers(dest="command")

    # build-map
    p_map = sub.add_parser("build-map", help="DART 매핑 테이블 구축")
    p_map.add_argument("--api-key", required=True, help="DART OpenAPI 인증키")
    p_map.add_argument("--corpcode-file", help="CORPCODE.xml 로컬 파일 경로 (없으면 API 다운로드)")

    # restore-from-csv
    p_restore = sub.add_parser("restore-from-csv", help="dart_raw_backup.csv → dart_raw 복구 (API 스킵)")
    p_restore.add_argument("--csv", default="data/dart/dart_raw_backup.csv")

    # empsttus
    p_emp = sub.add_parser("empsttus", help="DART 직원현황 적재 (상장사 공시 연봉)")
    p_emp.add_argument("--api-key", required=True)
    p_emp.add_argument("--years", default="2021,2022,2023,2024",
                       help="쉼표로 구분된 연도 목록 (기본: 2021~2024)")

    # export-empsttus
    p_exp = sub.add_parser("export-empsttus", help="dart_emp_sttus → CSV 백업")
    p_exp.add_argument("--csv", default="data/dart/dart_emp_sttus_backup.csv")

    # restore-empsttus
    p_rest_emp = sub.add_parser("restore-empsttus", help="CSV → dart_emp_sttus 복구 (API 스킵)")
    p_rest_emp.add_argument("--csv", default="data/dart/dart_emp_sttus_backup.csv")

    # finance-raw: .txt → dart_finance_raw (모든 항목 raw 보존)
    p_fin_raw = sub.add_parser("finance-raw", help="손익계산서 TXT → dart_finance_raw 적재 (모든 항목 raw 보존)")
    p_fin_raw.add_argument("--folder", required=True, help="손익계산서 TXT 파일이 있는 폴더 (하위 연도 폴더 자동 탐색)")

    # ingest (기존, deprecated 예정): .txt → company_finance 직행
    p_ingest = sub.add_parser("ingest", help="[DEPRECATED] 손익계산서 → company_finance 직행 (대신 finance-raw + compute 사용 권장)")
    p_ingest.add_argument("--folder", required=True, help="손익계산서 TXT 파일이 있는 폴더")

    # all
    p_all = sub.add_parser("all", help="매핑 구축 + empSttus + 손익계산서 적재")
    p_all.add_argument("--api-key", required=True)
    p_all.add_argument("--corpcode-file", help="CORPCODE.xml 로컬 파일 경로")
    p_all.add_argument("--folder", required=True)
    p_all.add_argument("--years", default="2021,2022,2023,2024")

    args = parser.parse_args()

    if args.command == "build-map":
        build_map(args.api_key, args.corpcode_file)
    elif args.command == "restore-from-csv":
        restore_from_csv(args.csv)
    elif args.command == "empsttus":
        years = [int(y.strip()) for y in args.years.split(",")]
        ingest_empsttus(args.api_key, years)
    elif args.command == "export-empsttus":
        export_empsttus(args.csv)
    elif args.command == "restore-empsttus":
        restore_empsttus_from_csv(args.csv)
    elif args.command == "finance-raw":
        ingest_finance_raw(args.folder)
    elif args.command == "ingest":
        ingest_finance(args.folder)
    elif args.command == "all":
        years = [int(y.strip()) for y in args.years.split(",")]
        build_map(args.api_key, args.corpcode_file)
        ingest_empsttus(args.api_key, years)
        ingest_finance_raw(args.folder)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
