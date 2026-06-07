"""
비상장 재무 QA: 엑셀(참고치) vs 우리 서빙 재무 대조 → 오파싱·미수집·오매칭 자동 검출.

사용:
  python -m app.qa_unlisted --xlsx ~/Downloads/top_1000_unlisted_comprehensive.xlsx
"""
import argparse
import csv
import re
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import text

from app.database import SessionLocal

TARGETS = Path("data/dart/unlisted_targets.csv")
OUT = Path("data/dart/unlisted_anomalies.csv")


def _won(s):
    s = str(s or "").strip()
    if not s or s in ("None", "-"):
        return None
    m = re.search(r"(-?\d[\d,]*\.?\d*)\s*(조|억)?", s.replace(" ", ""))
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    u = m.group(2)
    if u == "조":
        return v * 1e12
    if u == "억":
        return v * 1e8
    return v * (1e12 if abs(v) < 1000 else 1e8)  # 단위 없으면 휴리스틱(<1000=조)


def _excel_revenue(xlsx: str) -> dict:
    """회사명 → 최근(2024, 없으면 2023) 매출(원)."""
    wb = load_workbook(Path(xlsx).expanduser(), read_only=True, data_only=True)
    out = {}
    for sn in wb.sheetnames:
        rows = list(wb[sn].iter_rows(values_only=True))
        hdr = rows[0]
        c24 = hdr.index("매출_2024") if "매출_2024" in hdr else None
        c23 = hdr.index("매출_2023") if "매출_2023" in hdr else None
        for r in rows[1:]:
            if not (r and r[1]):
                continue
            v = _won(r[c24]) if c24 is not None else None
            if v is None and c23 is not None:
                v = _won(r[c23])
            if v:
                out.setdefault(str(r[1]).strip(), v)
    return out


# 금융사: 매출 정의(보험수익 vs 수입보험료, 이자수익 등)가 달라 엑셀과 큰 차이 정상 → 게이트 제외
_FINANCIAL = ("생명", "손해", "화재", "은행", "증권", "카드", "캐피탈", "금융", "보험", "저축", "자산운용")


def run(xlsx: str, apply: bool = False):
    excel = _excel_revenue(xlsx)
    targets = list(csv.DictReader(open(TARGETS, encoding="utf-8-sig")))
    db = SessionLocal()
    ok = miss = mism = noco = gated = 0
    anomalies = []
    try:
        for t in targets:
            en, cc = t["excel_name"], t["corp_code"]
            dn = db.execute(text("SELECT corp_name FROM dart_raw WHERE corp_code=:c"), {"c": cc}).scalar()
            comp = db.execute(
                text("SELECT c.id, f.revenue_current FROM companies c "
                     "LEFT JOIN company_finance f ON f.company_id=c.id "
                     "WHERE c.name=:n ORDER BY f.revenue_current DESC NULLS LAST LIMIT 1"),
                {"n": dn},
            ).first() if dn else None
            exv = excel.get(en)
            if comp is None:
                noco += 1
                anomalies.append((en, dn or "?", "서빙회사없음(미매칭)", exv))
                continue
            cid, serv = comp[0], comp[1]
            if serv is None:
                miss += 1
                anomalies.append((en, dn, "재무없음(파서실패/미제출)", exv))
                continue
            # 엑셀 재무는 더미라 "참고 표시만" — DB는 절대 안 건드림(읽기전용 리포트).
            # 실제 불량 제거는 compute 내장 internal sanity 가 DART 데이터로만 판단함.
            financial = any(k in (dn or "") for k in _FINANCIAL)
            if exv and not financial and not (0.4 <= serv / exv <= 2.5):
                mism += 1
                anomalies.append((en, dn, f"엑셀과 차이(참고: 서빙 {serv/1e12:.2f}조 vs 엑셀 {exv/1e12:.2f}조)", exv))
            else:
                ok += 1
        _ = (apply, gated)  # apply 무시 — 엑셀로 DB 수정 안 함
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with open(OUT, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["excel_name", "dart_name", "issue", "excel_revenue"])
            for a in anomalies:
                w.writerow(a)
        print(f"QA (타깃 {len(targets)}): OK {ok}, 재무없음 {miss}, 매출불일치 {mism}, 서빙회사못찾음 {noco}")
        print(f"이상치 저장: {OUT}")
        print("=== 이상치 샘플 20 ===")
        for a in anomalies[:20]:
            print(f"  {a[0]:24} | {a[2]}")
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default="~/Downloads/top_1000_unlisted_comprehensive.xlsx")
    ap.add_argument("--apply", action="store_true", help="엑셀과 크게 다른 비금융사 재무 삭제(게이트)")
    a = ap.parse_args()
    run(a.xlsx, apply=a.apply)


if __name__ == "__main__":
    main()
