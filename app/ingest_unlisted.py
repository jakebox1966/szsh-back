"""
비상장사 재무 적재 (Phase A): 사업보고서 제출 비상장사 → fnlttSinglAcntAll API.

기존 재무 경로(상장사 일괄 txt 파일)는 비상장을 못 받으므로, 회사별 재무 API로 보강한다.
compute.py 가 쓰는 표준 XBRL account_id 를 그대로 적재 → match + compute 가 자동으로 서빙에 반영.

흐름:
  1) corpCode.xml 로 corp_name → corp_code 해석 (로컬 캐시 우선)
  2) company.json → dart_raw upsert (bizr_no 등 — match.py 가 NPS 와 매칭하는 데 필요)
  3) fnlttSinglAcntAll(연도별) → dart_finance_raw upsert
  이후: python -m app.match (새 dart 를 NPS 와 매칭) → python -m app.compute (서빙 반영)
  ※ 사업보고서 미제출(감사보고서만) 회사는 API 가 013 → 스킵(= Phase B 감사보고서 파서 대상)

사용:
  python -m app.ingest_unlisted                         # 내장 SEED
  python -m app.ingest_unlisted --names "컬리,야놀자"   # 지정
"""
import argparse
import io
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests

from app.config import settings
from app.database import SessionLocal
from app.models import DartRaw, DartFinanceRaw

BASE = "https://opendart.fss.or.kr/api"
CORPCODE_CACHE = Path("data/dart/CORPCODE.xml")

# compute.py 가 매출/영업이익/순이익 추출에 쓰는 표준 account_id (이것만 적재하면 충분)
FIN_ACCOUNTS = {
    "ifrs-full_Revenue",
    "dart_OperatingIncomeLoss",
    "ifrs-full_ProfitLoss",
    "ifrs-full_RevenueFromInterest",
    "ifrs-full_FeeAndCommissionIncome",
    "ifrs-full_InsuranceRevenue",
    "dart_OperatingIncomeInsurance",
    "ifrs-full_ProfitLossFromOperatingActivities",
}

# Phase A 시드 — 사업보고서 제출 비상장사 위주(미제출이면 013 으로 자동 스킵되니 안전)
SEED = [
    "컬리", "비바리퍼블리카", "야놀자", "무신사", "우아한형제들",
    "에스케이온", "현대엔지니어링", "씨제이올리브영", "쿠팡", "오아시스",
    "당근마켓", "버킷플레이스", "메가존클라우드",
]


def _load_corpcode() -> list[tuple[str, str, str]]:
    """(corp_code, corp_name, stock_code) 목록. 캐시 없으면 다운로드 후 캐시."""
    if CORPCODE_CACHE.exists():
        xml = CORPCODE_CACHE.read_bytes()
    else:
        print("corpCode.xml 다운로드 중...")
        r = requests.get(f"{BASE}/corpCode.xml", params={"crtfc_key": settings.dart_api_key}, timeout=60)
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        xml = zf.read(zf.namelist()[0])
        CORPCODE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        CORPCODE_CACHE.write_bytes(xml)
    root = ET.fromstring(xml)
    return [
        ((e.findtext("corp_code") or "").strip(),
         (e.findtext("corp_name") or "").strip(),
         (e.findtext("stock_code") or "").strip())
        for e in root.iter("list")
    ]


def _resolve(names: list[str], corpcodes: list[tuple[str, str, str]]):
    by_name: dict[str, list] = {}
    for cc, nm, sc in corpcodes:
        by_name.setdefault(nm, []).append((cc, nm, sc))
    picked = []
    for n in names:
        ms = by_name.get(n)
        if not ms:
            print(f"  ⚠ corp_code 못찾음: {n!r}")
            continue
        unlisted = [m for m in ms if not m[2]]  # 비상장(stock_code 빈) 우선
        picked.append((unlisted or ms)[0])
    return picked


def _get(url, params):
    return requests.get(url, params={**params, "crtfc_key": settings.dart_api_key}, timeout=20).json()


def _won(s):
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _parse_dt(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y%m%d").date()
    except ValueError:
        return None


def run(names: list[str], years: list[int]):
    targets = _resolve(names, _load_corpcode())
    db = SessionLocal()
    dart_ok = fin_rows = 0
    try:
        for cc, nm, _sc in targets:
            info = _get(f"{BASE}/company.json", {"corp_code": cc})
            if info.get("status") != "000":
                print(f"  ⚠ company.json 실패: {nm} ({info.get('message')})")
                continue

            vals = dict(
                corp_code=cc, corp_name=info.get("corp_name") or nm,
                stock_name=info.get("stock_name"),
                stock_code=(info.get("stock_code") or "").strip() or None,
                bizr_no=info.get("bizr_no"), jurir_no=info.get("jurir_no"),
                ceo_nm=info.get("ceo_nm"), corp_cls=info.get("corp_cls"),
                address=info.get("adres"), eng_name=info.get("corp_name_eng"),
                induty_code=info.get("induty_code"),
                est_dt=_parse_dt(info.get("est_dt")), acc_mt=info.get("acc_mt"),
                fetched_at=datetime.utcnow(),
            )
            dart = db.query(DartRaw).filter(DartRaw.corp_code == cc).first()
            if dart:
                for k, v in vals.items():
                    setattr(dart, k, v)
            else:
                dart = DartRaw(**vals)
                db.add(dart)
            db.flush()
            dart_ok += 1

            got_year = False
            for year in years:
                picked_fs = None
                for fs in ("CFS", "OFS"):
                    j = _get(f"{BASE}/fnlttSinglAcntAll.json",
                             {"corp_code": cc, "bsns_year": str(year), "reprt_code": "11011", "fs_div": fs})
                    if j.get("status") == "000" and j.get("list"):
                        picked_fs = (fs, j["list"])
                        break
                    time.sleep(0.05)
                if not picked_fs:
                    continue
                fs, items = picked_fs
                got_year = True
                # account_id 별 "첫 유효값"(= 손익계산서 본문 총계; 귀속/포괄 중복은 뒤라 무시)
                seen: dict[str, tuple] = {}
                for it in items:
                    aid = it.get("account_id")
                    if aid in FIN_ACCOUNTS and aid not in seen:
                        amt = _won(it.get("thstrm_amount"))
                        if amt is not None:
                            seen[aid] = (it.get("account_nm"), amt, _won(it.get("frmtrm_amount")))
                stmt_type = "포괄손익계산서 - 연결" if fs == "CFS" else "포괄손익계산서 - 별도"
                for aid, (anm, cur, prev) in seen.items():
                    ex = (
                        db.query(DartFinanceRaw)
                        .filter_by(dart_id=dart.id, bsns_year=str(year),
                                   account_id=aid, statement_type=stmt_type)
                        .first()
                    )
                    if ex:
                        ex.current_amount, ex.prev_amount, ex.account_nm = cur, prev, anm
                    else:
                        db.add(DartFinanceRaw(
                            dart_id=dart.id, stock_code=None, bsns_year=str(year),
                            report_type="사업보고서", statement_type=stmt_type,
                            account_id=aid, account_nm=anm,
                            current_amount=cur, prev_amount=prev,
                            currency="KRW", ingested_at=datetime.utcnow(),
                        ))
                    fin_rows += 1

            print(f"  {'✅' if got_year else '⏭️ '} {nm:16} dart_id={dart.id} "
                  f"{'재무 적재' if got_year else '재무 없음(013) → Phase B 감사보고서 대상'}")
        db.commit()
        print(f"\n완료: dart_raw {dart_ok}건, finance {fin_rows}행")
        print("다음 단계: python -m app.match  →  python -m app.compute")
    except Exception as e:
        db.rollback()
        print(f"오류: {e}")
        raise
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser(description="비상장사 재무 적재 (Phase A)")
    ap.add_argument("--names", type=str, default="", help="쉼표구분 회사명 (없으면 SEED)")
    ap.add_argument("--years", type=str, default="2021,2022,2023,2024")
    a = ap.parse_args()
    names = [x.strip() for x in a.names.split(",") if x.strip()] or SEED
    run(names, [int(y) for y in a.years.split(",")])


if __name__ == "__main__":
    main()
