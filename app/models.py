from datetime import datetime

from sqlalchemy import (
    Column, Integer, BigInteger, String, Text, Numeric, DateTime, Date, Boolean,
    ForeignKey, ARRAY, UniqueConstraint, Index
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.database import Base


# ── 사용자 ──────────────────────────────────────────────────────


class User(Base):
    """
    소셜 로그인 사용자 (Google / Kakao).
    가입 시점에 닉네임 풀에서 자동 발급.
    JWT + refresh token 기반 인증 + AI 기능 일일 호출 제한 카운터 포함.
    """
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_users_provider_pid"),
    )

    id = Column(Integer, primary_key=True, index=True)
    # 외부 노출용 식별자 (nanoid 12자). 내부 PK(id)는 노출하지 않고 이 값만 API/URL 에 사용.
    public_id = Column(String(12), unique=True, nullable=False, index=True)
    nickname = Column(String, unique=True, nullable=False, index=True)

    # 소셜 인증
    provider = Column(String, nullable=False)                 # "google" | "kakao"
    provider_user_id = Column(String, nullable=False)         # provider 내부 고유 ID
    email = Column(String, nullable=True, index=True)         # provider가 제공 안 할 수도 있음
    profile_image = Column(String, nullable=True)

    refresh_token_hash = Column(String, nullable=True)        # 로그아웃/탈취 대응

    # AI 기능 호출 제한
    tier = Column(String, default="free", nullable=False)     # "free" | "premium"
    ai_calls_today = Column(Integer, default=0, nullable=False)
    ai_calls_reset_at = Column(Date, nullable=True)
    # 검색 일일 한도(회원). 비회원은 프론트 localStorage 가 담당. reset_at(KST 날짜) 바뀌면 0으로.
    search_calls_today = Column(Integer, default=0, nullable=False)
    search_calls_reset_at = Column(Date, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_active_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    comments = relationship("CompanyComment", back_populates="user")


# ── 코드표 ──────────────────────────────────────────────────────


class Industry(Base):
    __tablename__ = "industries"

    code = Column(String, primary_key=True)        # 사업장업종코드 (예: 722000)
    name = Column(String, nullable=False)           # 업종명

    nps_rows = relationship("NpsRaw", back_populates="industry_rel")


# ── 원본 계층 ───────────────────────────────────────────────────


class NpsRaw(Base):
    __tablename__ = "nps_raw"

    id = Column(Integer, primary_key=True, index=True)
    biz_no = Column(String, nullable=False, index=True)                # 사업자등록번호 (앞 6자리)
    name = Column(String, nullable=False)                              # 사업장명

    __table_args__ = (
        UniqueConstraint("biz_no", "name", name="uq_nps_raw_biz_no_name"),
    )
    enrollment_status = Column(Integer)                                # 1=등록, 2=탈퇴
    postal_code = Column(String(10))
    address_lot = Column(String)                                       # 지번주소
    address_road = Column(String)                                      # 도로명주소
    legal_dong_code = Column(String(10))
    admin_dong_code = Column(String(10))
    sido_code = Column(String(2))                                      # 시도
    sigungu_code = Column(String(3))                                   # 시군구
    dong_code = Column(String(3))                                      # 읍면동
    workplace_type = Column(Integer)                                   # 1=법인, 2=개인
    industry_code = Column(String, ForeignKey("industries.code"))
    applied_at = Column(Date)                                          # 적용일자
    re_registered_at = Column(Date)                                    # 재등록일자
    withdrawn_at = Column(Date)                                        # 탈퇴일자

    industry_rel = relationship("Industry", back_populates="nps_rows")
    monthly = relationship("NpsMonthly", back_populates="nps", order_by="NpsMonthly.year_month")


class NpsMonthly(Base):
    __tablename__ = "nps_monthly"
    __table_args__ = (
        UniqueConstraint("nps_id", "year_month", name="uq_nps_monthly_yearmonth"),
        Index("idx_nps_monthly_yearmonth", "year_month"),
        Index("idx_nps_monthly_nps_yearmonth", "nps_id", "year_month"),
    )

    id = Column(Integer, primary_key=True, index=True)
    nps_id = Column(Integer, ForeignKey("nps_raw.id"), nullable=False)
    year_month = Column(Date, nullable=False)          # 자료생성년월 (예: 2026-01-01)
    subscribers = Column(Integer)                       # 가입자수
    notified_amount = Column(BigInteger)                # 당월고지금액 (원)
    new_acquisitions = Column(Integer)                  # 신규취득자수
    lost_subscribers = Column(Integer)                  # 상실가입자수

    nps = relationship("NpsRaw", back_populates="monthly")


class DartRaw(Base):
    """DART 기업개황 API 응답 raw 미러. 응답 필드는 가급적 그대로 보존."""
    __tablename__ = "dart_raw"

    id = Column(Integer, primary_key=True, index=True)
    corp_code = Column(String, unique=True, nullable=False)   # DART 고유번호
    corp_name = Column(String)                                 # 회사명
    stock_name = Column(String)                                # 종목명 (보통 corp_name과 동일하나 다를 수도)
    stock_code = Column(String, index=True)                    # 종목코드
    bizr_no = Column(String, index=True)                       # 사업자등록번호 (10자리)
    jurir_no = Column(String)                                  # 법인등록번호
    ceo_nm = Column(String)                                    # 대표자명
    corp_cls = Column(String(1))                               # 시장구분 Y=유가증권 / K=코스닥 / N=코넥스 / E=기타
    address = Column(String)                                   # 주소 (adres)
    eng_name = Column(String)                                  # 영문명 (corp_name_eng)
    website = Column(String)                                   # 홈페이지 (hm_url)
    ir_url = Column(String)                                    # IR 페이지
    phn_no = Column(String)                                    # 전화번호
    fax_no = Column(String)                                    # 팩스
    induty_code = Column(String)                               # 업종코드 (DART 기준)
    est_dt = Column(Date)                                      # 설립일자 (YYYYMMDD → Date)
    acc_mt = Column(String(2))                                 # 결산월 (예: "12")
    raw_json = Column(JSONB)                                   # 기업개황 API 응답 전체 (보존용)
    fetched_at = Column(DateTime, default=datetime.utcnow)     # 마지막 API 호출 시각

    emp_sttus = relationship("DartEmpSttus", back_populates="dart")
    finance_raw = relationship("DartFinanceRaw", back_populates="dart")


class DartEmpSttus(Base):
    """DART 사업보고서 '직원의 현황' 공시 데이터 raw. dart_raw와 1:N (연도×성별×사업부문)."""
    __tablename__ = "dart_emp_sttus"

    id = Column(Integer, primary_key=True, index=True)
    dart_id = Column(Integer, ForeignKey("dart_raw.id"), nullable=False, index=True)
    year = Column(String, nullable=False, index=True)          # bsns_year
    sex = Column(String)                                       # "남" / "여" / "합계" / None
    business_unit = Column(String)                             # fo_bbm (사업부문) — 회사가 부문별로 쪼개서 공시할 때
    regular_count = Column(Integer)                            # rgllbr_co (정규직 수)
    regular_part_count = Column(Integer)                       # rgllbr_abacpt_labrr_co (정규직 중 단시간 근로자)
    contract_count = Column(Integer)                           # cnttk_co (기간제 수)
    contract_part_count = Column(Integer)                      # cnttk_abacpt_labrr_co (기간제 중 단시간)
    total_count = Column(Integer)                              # sm (합계)
    annual_salary_total = Column(BigInteger)                   # fyer_salary_totamt (연간급여총액, 원)
    avg_annual_salary = Column(BigInteger)                     # jan_salary_am (1인 평균급여액, 원)
    avg_tenure_months = Column(Integer)                        # avrg_cnwk_sdytrn → 개월 변환
    avg_tenure_raw = Column(String)                            # 원본 평균 근속연수 문자열 (예: "10년 3개월")
    remark = Column(Text)                                      # rm (비고)
    raw_json = Column(JSONB)                                   # empSttus API 응답 항목 전체 (보존용)

    dart = relationship("DartRaw", back_populates="emp_sttus")

    __table_args__ = (
        # business_unit 까지 포함하지 않으면 부문별 row가 겹침. None은 PG에서 unique 무시되므로 COALESCE 사용해야 하나
        # 우선 기존 호환 위해 (dart_id, year, sex) 유지 — business_unit 있는 회사는 ingest 시 sex에 부문명 부착하거나 별도 처리
        UniqueConstraint("dart_id", "year", "sex", "business_unit", name="uq_emp_sttus"),
    )


class DartFinanceRaw(Base):
    """DART 재무정보 일괄다운로드(.txt) raw 미러.
    파일의 각 항목코드를 1행씩 그대로 보존. company_finance 는 여기서 파생."""
    __tablename__ = "dart_finance_raw"

    id = Column(Integer, primary_key=True, index=True)
    dart_id = Column(Integer, ForeignKey("dart_raw.id"), nullable=True, index=True)  # stock_code 기반 매핑. 매핑 실패해도 raw는 보존
    stock_code = Column(String, index=True)                    # 종목코드 (파일에 [001040] 형태로 들어옴 → 정제)
    bsns_year = Column(String, nullable=False, index=True)     # 사업연도 (파일명 prefix: 2024_사업보고서_...)
    report_type = Column(String)                               # 보고서종류 (예: "사업보고서", "1분기보고서")
    statement_type = Column(String)                            # 재무제표종류 (예: "손익계산서, 성격별 분류 - 별도")
    market_div = Column(String)                                # 시장구분 (유가증권시장상장법인 등)
    industry_code = Column(String)                             # 업종 코드
    industry_name = Column(String)                             # 업종명
    acc_mt = Column(String(2))                                 # 결산월
    end_dt = Column(Date)                                      # 결산기준일
    currency = Column(String(8))                               # 통화 (KRW 등)
    account_id = Column(String, index=True)                    # 항목코드 (ifrs-full_Revenue, dart_OperatingIncomeLoss 등)
    account_nm = Column(String)                                # 항목명 (영업이익(손실), 매출액 등)
    current_amount = Column(BigInteger)                        # 당기 금액
    prev_amount = Column(BigInteger)                           # 전기 금액
    prev2_amount = Column(BigInteger)                          # 전전기 금액
    ingested_at = Column(DateTime, default=datetime.utcnow)    # 적재 시각

    dart = relationship("DartRaw", back_populates="finance_raw")

    __table_args__ = (
        # 같은 회사 × 같은 사업연도 × 같은 보고서/재무제표 × 같은 항목코드는 1행
        UniqueConstraint("stock_code", "bsns_year", "report_type", "statement_type", "account_id",
                         name="uq_dart_finance_raw"),
        Index("idx_dart_finance_raw_lookup", "stock_code", "bsns_year", "account_id"),
    )


# ── 매핑 ────────────────────────────────────────────────────────


class Mapping(Base):
    """NPS ↔ DART 매칭 결과."""
    __tablename__ = "mapping"

    id = Column(Integer, primary_key=True, index=True)
    nps_id = Column(Integer, ForeignKey("nps_raw.id"), nullable=False)
    dart_id = Column(Integer, ForeignKey("dart_raw.id"), nullable=False)
    score = Column(Integer)                                    # fuzzy matching 점수
    status = Column(String, default="pending")                 # matched / pending

    nps = relationship("NpsRaw")
    dart = relationship("DartRaw")


# ── 서비스 계층 (Service) ────────────────────────────────────────


class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    eng_name = Column(String)
    logo = Column(String)
    industry = Column(String)
    industry_code = Column(String, nullable=True, index=True)  # KSIC 원본 코드 (NPS 6자리/DART 5자리). 분야 그루핑용
    founded_year = Column(Integer, nullable=True)              # 설립연도 (applied_at.year)
    address = Column(String)
    website = Column(String)
    tags = Column(ARRAY(Text), default=[])
    nps_id = Column(Integer, ForeignKey("nps_raw.id"), nullable=True)  # 원본 연결
    aliases = Column(ARRAY(Text), nullable=True)                       # 회사명 별칭 (fuzzy 검색용)
    is_listed = Column(Boolean, nullable=True)                         # 상장 여부 (DART corp_cls 기반)

    summary = relationship("CompanySummary", uselist=False, back_populates="company")
    comments = relationship("CompanyComment", back_populates="company", order_by="CompanyComment.created_at.desc()")
    hr = relationship("CompanyHR", uselist=False, back_populates="company")
    hr_trends = relationship("CompanyHRTrend", back_populates="company", order_by="CompanyHRTrend.year_month")
    finance = relationship("CompanyFinance", back_populates="company")
    finance_history = relationship("CompanyFinanceHistory", back_populates="company", order_by="CompanyFinanceHistory.id")


class CompanySummary(Base):
    __tablename__ = "company_summary"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), unique=True, nullable=False)
    avg_salary = Column(BigInteger)                            # 원. NPS 역산 추정
    avg_salary_official = Column(BigInteger, nullable=True)    # 원. DART empSttus 공시 (상장사만)
    avg_salary_capped = Column(Boolean, default=False)         # NPS 상한(연 8,069만원=806,900,000원) 걸린 플래그
    prev_year_avg_salary = Column(BigInteger, nullable=True)   # 원. 전년 동월 NPS 역산
    total_employees = Column(Integer)
    turnover_rate = Column(Numeric(10, 2))                     # %.
    revenue = Column(BigInteger)                               # 원

    company = relationship("Company", back_populates="summary")


class CompanyComment(Base):
    __tablename__ = "company_comments"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    content = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=True)               # 수정 시각 (수정 안되면 None)
    deleted_at = Column(DateTime, nullable=True, index=True)   # soft delete

    company = relationship("Company", back_populates="comments")
    user = relationship("User", back_populates="comments")


class CompanyHR(Base):
    __tablename__ = "company_hr"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), unique=True, nullable=False)
    current = Column(Integer)

    company = relationship("Company", back_populates="hr")


class CompanyHRTrend(Base):
    __tablename__ = "company_hr_trends"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year_month = Column(Date, nullable=False)                 # 월별 시점 (정렬/계산 가능)
    in_count = Column(Integer)
    out_count = Column(Integer)
    subscribers = Column(Integer)                             # 월별 가입자수 (= 그 시점 직원수)

    company = relationship("Company", back_populates="hr_trends")


class CompanyFinance(Base):
    __tablename__ = "company_finance"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    revenue_current = Column(BigInteger)                       # 원
    revenue_growth = Column(Numeric(10, 2))                    # %.
    operating_profit_current = Column(BigInteger)              # 원
    operating_profit_growth = Column(Numeric(10, 2))           # %.
    net_profit_current = Column(BigInteger)                    # 원
    net_profit_growth = Column(Numeric(10, 2))                 # %.
    year = Column(String)

    company = relationship("Company", back_populates="finance")


class CompanyFinanceHistory(Base):
    __tablename__ = "company_finance_history"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(String)
    rev = Column(BigInteger)                                   # 원
    profit = Column(BigInteger)                                # 원

    company = relationship("Company", back_populates="finance_history")


class NetPayReference(Base):
    __tablename__ = "net_pay_reference"

    id = Column(Integer, primary_key=True, index=True)
    gross = Column(BigInteger)  # 원 (세전)
    net = Column(BigInteger)    # 원 (실수령)


class EmployeeGrowth(Base):
    __tablename__ = "employee_growth"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    count = Column(Integer)
    growth = Column(Numeric(10, 2))                            # %. 캡 해제
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)

    company = relationship("Company")


# ── 최신 검색어 (글로벌, 전체 사용자) ──────────────────────────────


class SearchTerm(Base):
    """전체 사용자 검색어. 최신순 노출용 — keyword 단위 upsert.

    search_count 도 함께 올려, 추후 인기/급상승(B)으로 확장 시 재사용 가능.
    """
    __tablename__ = "search_terms"

    id = Column(Integer, primary_key=True, index=True)
    keyword = Column(String, unique=True, nullable=False, index=True)
    search_count = Column(Integer, default=1, nullable=False)
    last_searched_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
