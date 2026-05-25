"""분야(industry group) 정의.

KSIC 코드 prefix 는 9차/10차에서 의미가 달라 (예: 9차 21=종이, 10차 21=의약품)
prefix 매핑 만으로는 안전하지 않음. companies.industry (한국어 업종명) 키워드
매칭 방식 사용 — 차수와 무관하게 일관된 결과 제공.

각 그룹은 (1) industry 텍스트에 포함되어야 할 keywords 와 (2) 제외 키워드(false positive 회피) 정의.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class IndustryGroup:
    key: str                # URL/내부 키
    label: str              # 한글 표시명
    include: tuple[str, ...]  # industry 컬럼에 이 중 하나라도 포함되면 해당 그룹
    exclude: tuple[str, ...] = field(default=())  # 단, 이 키워드 들어가면 제외


# 랭킹·스포트라이트에 노출되는 분야들.
INDUSTRY_GROUPS: list[IndustryGroup] = [
    IndustryGroup(
        key="it",
        label="IT·플랫폼",
        include=("소프트웨어", "정보 서비스", "온라인", "포털", "데이터", "응용", "시스템 통합", "컴퓨터 프로그래밍", "정보처리"),
        exclude=("도매업", "소매업"),
    ),
    IndustryGroup(
        key="electronics",
        label="전자·반도체",
        # "영상 기기" 추가 — 삼성전자 industry "비디오 및 기타 영상 기기 제조업" 매칭
        include=(
            "반도체", "디스플레이", "전자", "컴퓨터 제조",
            "통신장비", "통신 장비", "전기장비", "전기 장비",
            "영상 기기", "음향 기기",
        ),
    ),
    IndustryGroup(
        key="automotive",
        label="자동차·모빌리티",
        include=("자동차", "모터사이클", "운송장비", "운송 장비", "차량용"),
        exclude=("판매", "수리", "정비"),
    ),
    IndustryGroup(
        key="chemical",
        label="화학·소재",
        include=("화학", "석유", "고무", "플라스틱", "1차 금속", "1차금속", "비철금속", "철강", "금속 가공"),
        exclude=("의약품", "화장품"),
    ),
    IndustryGroup(
        key="food",
        label="식음료",
        include=("식료품", "음료", "식품", "유제품", "제과", "주류", "비알코올"),
    ),
    IndustryGroup(
        key="bio",
        label="바이오·제약",
        include=("의약품", "제약", "바이오", "의학", "약학", "생물학", "한의약품", "의료용품"),
    ),
    IndustryGroup(
        key="retail",
        label="유통·리테일",
        include=("도매업", "소매업", "백화점", "편의점", "마트", "전자상거래"),
    ),
    IndustryGroup(
        key="logistics",
        label="운수·물류",
        # "화물 운송" 만 잡고 "화물 자동차 제조" 는 제외 (현대차 등 자동차 회사 매칭 방지)
        include=("택배", "운송업", "물류", "화물 운송", "창고", "배송", "항공 운송", "해상 운송"),
        exclude=("제조업",),
    ),
    IndustryGroup(
        key="construction",
        label="건설·부동산",
        include=("건설업", "건축", "토목", "부동산", "주택"),
    ),
    IndustryGroup(
        key="entertainment",
        label="콘텐츠·엔터",
        include=("영화", "방송", "게임", "음악", "공연", "엔터테인먼트", "출판", "광고"),
    ),
    IndustryGroup(
        key="banking_securities",
        label="은행·증권·여신",
        # industry 컬럼 실제 값: "국내은행", "증권 중개업", "기타 금융 투자업", "여신 금융업" 등
        include=("은행", "증권", "카드", "금융 투자", "여신"),
        # exclude: 회사명/업종에 "은행"이 들어가지만 금융업 아닌 사업장 회피
        # (NPS 데이터에 "은행제빵소", "은행나무" 등 동음 사업장 다수 존재)
        exclude=("제과점", "음식점", "어린이집", "병원", "학교", "임대업"),
    ),
    IndustryGroup(
        key="insurance",
        label="보험",
        include=("생명보험", "손해보험", "보험 대리"),
    ),
]


def get_group(key: str) -> IndustryGroup | None:
    for g in INDUSTRY_GROUPS:
        if g.key == key:
            return g
    return None


def industry_matches_group(industry_name: str | None, group: IndustryGroup) -> bool:
    if not industry_name:
        return False
    if any(ex in industry_name for ex in group.exclude):
        return False
    return any(inc in industry_name for inc in group.include)


def classify_industry(industry_name: str | None) -> IndustryGroup | None:
    """industry 한국어 텍스트 → 매칭되는 IndustryGroup. 어느 그룹에도 안 맞으면 None."""
    if not industry_name:
        return None
    for g in INDUSTRY_GROUPS:
        if industry_matches_group(industry_name, g):
            return g
    return None
