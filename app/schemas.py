from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ingest 단계에서 사업자등록번호 누락 시 industry 컬럼에 박힌 placeholder.
# 사용자 화면에는 노출하지 않음 (DB 값은 ingest meta 보존 위해 그대로 둠).
_INDUSTRY_PLACEHOLDERS = {"BIZ_NO미존재사업장"}


def _clean_industry(v: Optional[str]) -> Optional[str]:
    return None if v in _INDUSTRY_PLACEHOLDERS else v


# --- Home ---

class NetPayOut(BaseModel):
    gross: int  # 원
    net: int    # 원

    model_config = {"from_attributes": True}


class EmployeeGrowthOut(BaseModel):
    name: str
    count: int
    growth: float  # %

    model_config = {"from_attributes": True}


# --- Company ---

class CompanyListItem(BaseModel):
    slug: str
    name: str
    engName: Optional[str] = Field(None, validation_alias="eng_name")
    logo: Optional[str] = None
    industry: Optional[str] = None
    isListed: Optional[bool] = Field(None, validation_alias="is_listed")

    _clean_industry = field_validator("industry", mode="before")(_clean_industry)

    model_config = {"from_attributes": True, "populate_by_name": True}


class SummaryOut(BaseModel):
    avgSalary: Optional[int] = None         # 원 (NPS 추정, 전 기업)
    avgSalaryOfficial: Optional[int] = None # 원 (DART 공시, 상장사만)
    avgSalaryCapped: Optional[bool] = None  # NPS 상한(연 806,900,000원) 걸림 여부
    totalEmployees: Optional[int] = None
    turnoverRate: Optional[float] = None    # %
    revenue: Optional[int] = None           # 원

    model_config = {"from_attributes": True}


class CommentOut(BaseModel):
    id: int
    user: str
    user_id: int = Field(serialization_alias="userId")
    content: str
    time: str
    edited: bool = False

    model_config = {"from_attributes": True, "populate_by_name": True}


class CommentCreate(BaseModel):
    content: str


class CommentUpdate(BaseModel):
    content: str


class HRTrendOut(BaseModel):
    month: str                                                  # "YYYY-MM" 형식
    in_count: int = Field(serialization_alias="in")
    out_count: int = Field(serialization_alias="out")
    subscribers: Optional[int] = None                            # 월별 가입자수 (인원수 라인차트용)

    model_config = {"from_attributes": True, "populate_by_name": True}


class HROut(BaseModel):
    current: int                                                 # 최신 인원수
    incoming: int                                                # 최근 N개월 입사 합 (참고)
    outgoing: int                                                # 최근 N개월 퇴사 합 (참고)
    # 이미지 디자인 KPI 카드용
    latestMonth: Optional[str] = None                            # 최신 데이터 월 (예: "2026-03")
    monthlyHeadcount: Optional[int] = None                       # 최신월 인원수
    monthlyIn: Optional[int] = None                              # 최신월 입사
    monthlyOut: Optional[int] = None                             # 최신월 퇴사
    annualTurnover: Optional[float] = None                       # 연간 퇴사율 (%)
    trends: list[HRTrendOut] = []

    model_config = {"from_attributes": True}


class FinanceOut(BaseModel):
    revenue: dict          # { current: int(원), growth: float(%) }
    operatingProfit: dict
    netProfit: dict
    history: list[dict] = []  # [{ year, rev(원), profit(원) }]

    model_config = {"from_attributes": True}


class IndustryGroupOut(BaseModel):
    key: str        # URL 필터링용 식별자 (예: "it")
    label: str      # 표시 라벨 (예: "IT·플랫폼")


class CompanyInfoOut(BaseModel):
    name: str
    engName: Optional[str] = None
    logo: Optional[str] = None
    industry: Optional[str] = None
    industryGroup: Optional[IndustryGroupOut] = None   # 상위 분야 분류
    history: Optional[str] = None
    address: Optional[str] = None
    website: Optional[str] = None
    tags: list[str] = []
    isListed: Optional[bool] = Field(None, validation_alias="is_listed")

    _clean_industry = field_validator("industry", mode="before")(_clean_industry)

    model_config = {"from_attributes": True, "populate_by_name": True}


# --- Company of Day (1일 1컴퍼니) ---

class CompanyOfDayHistory(BaseModel):
    year: str
    revenue: Optional[int] = None         # 원
    operatingProfit: Optional[int] = None # 원


class CompanyOfDayOut(BaseModel):
    date: str
    dataAsOf: Optional[str] = None
    slug: str
    name: str
    stockCode: Optional[str] = None
    market: Optional[str] = None          # "코스피" | "코스닥" | "코넥스" | None
    industry: Optional[str] = None
    employees: Optional[int] = None
    history: list[CompanyOfDayHistory] = []
    # 최신 사업연도 기준 핵심 지표
    operatingMargin: Optional[float] = None      # %, 영업이익률
    revenuePerEmployee: Optional[int] = None     # 원, 1인당 생산성

    _clean_industry = field_validator("industry", mode="before")(_clean_industry)


# --- Industry Compare (분야 내 회사 비교 카드) ---

class CompareHistoryItem(BaseModel):
    year: str
    revenue: Optional[int] = None         # 원
    revenueGrowth: Optional[float] = None # %, 전년 대비
    operatingProfit: Optional[int] = None # 원
    profitGrowth: Optional[float] = None  # %, 전년 대비


class CompareCompany(BaseModel):
    slug: str
    name: str
    industry: Optional[str] = None
    history: list[CompareHistoryItem] = []

    _clean_industry = field_validator("industry", mode="before")(_clean_industry)


class IndustryCompareOut(BaseModel):
    date: str
    groupKey: str
    groupLabel: str
    dataAsOf: Optional[str] = None
    items: list[CompareCompany] = []


# --- Spotlight (분야별 로테이션 랭킹) ---

class SpotlightItem(BaseModel):
    slug: str
    name: str
    industry: Optional[str] = None
    # 지표별 노출값. 클라이언트에서 metric 보고 어느 필드 쓸지 결정.
    salary: Optional[int] = None          # 원, metric=salary 일 때
    employees: Optional[int] = None
    revenue: Optional[int] = None         # 원, metric=revenue 일 때
    operatingProfit: Optional[int] = None # 원
    revenueGrowth: Optional[float] = None # %
    employeeGrowth: Optional[float] = None # %, metric=growth 일 때

    _clean_industry = field_validator("industry", mode="before")(_clean_industry)

    model_config = {"from_attributes": True}


class SpotlightOut(BaseModel):
    date: str                # 호출 일자 (로테이션 시드용)
    dataAsOf: Optional[str] = None  # 데이터 기준 시점 표시용 (예: "2024 사업보고서 기준")
    groupKey: str            # "it" | "electronics" | ...
    groupLabel: str
    metric: str              # "salary" | "revenue" | "growth"
    metricLabel: str
    pageIdx: int = 0         # 0 = 1~5위, 1 = 6~10위
    rankStart: int = 1       # UI 순위 표시 시작값 (1 또는 6)
    items: list[SpotlightItem] = []


class CompanyDetailOut(BaseModel):
    info: CompanyInfoOut
    summary: Optional[SummaryOut] = None
    salary: dict
    sampleComments: list[CommentOut] = []
    hr: Optional[HROut] = None
    finance: Optional[FinanceOut] = None

    model_config = {"from_attributes": True}
