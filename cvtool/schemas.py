"""CV 구조화 스키마 (확정본).

한 번에 전부 뽑지 않고 4개 섹션으로 나눠 호출한다.
  - 출력이 길어져 잘리는 것을 막고
  - 모델이 한 번에 한 가지에 집중해 정확도가 올라간다

개인정보 주의: 이름/생년월일/연락처/이메일은 Postgres(또는 로컬 DB)에만 두고,
벡터 DB payload 에는 지원자_ID 만 넣는다.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 엑셀 열 순서 (이 순서 그대로 시트에 나간다)
# ---------------------------------------------------------------------------
COLUMNS: list[str] = [
    # A. 식별·기본
    # 지원자_ID 는 내부 키라서 표·엑셀에 내지 않는다 (레코드에는 그대로 있다).
    "한글_이름",
    "영문_이름",
    "생년월일",
    "전화번호",
    "이메일",
    # B. 현재
    "현재_신분",
    "현재_소속",
    "현재_소속_상세",
    "현재_지도교수",
    # C. 박사
    "박사_석박통합",
    "박사_학교",
    "박사_전공",
    "박사_지도교수",
    "박사_시작",
    "박사_졸업",
    "박사_학위상태",
    # D. 석사
    "석사_학교",
    "석사_전공",
    "석사_지도교수",
    "석사_시작",
    "석사_졸업",
    # E. 학사
    "학사_학교",
    "학사_전공",
    "학사_시작",
    "학사_졸업",
    # F. 연구
    "1저자_해외논문_제출처",
    "저널_수",
    "저널_주저자_수",
    "학회_수",
    "학회_주저자_수",
    "특허_등록_수",
    "특허_출원_수",
    "연구분야_키워드",
    # G. 경력 (가장 최근 것 하나. 6개월 미만·인턴은 빼고 센다)
    "경력_요약",
    "경력_회사",
    "직책",
    "경력_시작",
    "경력_종료",
    # H. 검토
    "검토_필요",
    "검토_사유",
]

#: 열 이름과 **표에 보일 기본 이름**이 다른 것.
#: 열 이름은 코드가 쓰는 값이라 괄호·공백을 넣지 않는다. 화면에 보이는 이름만
#: 사람이 읽기 좋은 쪽으로 둔다 (표 항목 탭에서 더 고칠 수 있다).
DEFAULT_LABELS: dict[str, str] = {
    "경력_회사": "경력_회사(학교)",
    "박사_석박통합": "석박통합",
}

#: 명칭 사전으로 대표명을 붙일 열 (열 이름 -> 사전 종류)
NAME_COLUMNS: dict[str, str] = {
    "현재_소속": "소속",
    "박사_학교": "소속",
    "석사_학교": "소속",
    "학사_학교": "소속",
    "박사_전공": "전공",
    "석사_전공": "전공",
    "학사_전공": "전공",
}

#: 등급별 논문 수 열의 접두사
TIER_COLUMN_PREFIX = "1저자_해외논문_"


def columns(registry=None) -> list[str]:
    """표에 나갈 열 목록.

    등급별 논문 수 열은 담당자가 켠 등급만 나온다(기본 최우수·우수).
    registry 가 없으면 기본 열만 돌려준다.
    """
    if registry is None:
        return list(COLUMNS)
    tiers = registry.column_tiers()
    out: list[str] = []
    for col in COLUMNS:
        out.append(col)
        if col == "1저자_해외논문_제출처":
            out.extend(f"{TIER_COLUMN_PREFIX}{t}" for t in tiers)
    return out


# 엑셀에서 반드시 텍스트로 써야 하는 열 (앞자리 0 보존, 날짜 자동변환 방지)
TEXT_COLUMNS: set[str] = {
    "생년월일",
    "전화번호",
    "경력_시작",
    "경력_종료",
    "박사_시작",
    "박사_졸업",
    "석사_시작",
    "석사_졸업",
    "학사_시작",
    "학사_졸업",
}

# "불명" 이 반드시 있어야 한다. guided_json 은 문법을 강제하므로, 이 값이 없으면
# 모델이 판단이 안 서도 목록에서 하나를 억지로 찍는다 (그럴듯한 오답이 나온다).
현재_신분_ENUM = ["포닥", "박사", "석박통합", "석사", "학사", "타사재직", "기타", "불명"]
학위상태_ENUM = ["졸업", "수료", "재학", "예정", ""]
#: 박사 과정이 석박사 통합과정이었는지. 빈칸이 '아님' 이다 (Y/N 로 두면
#: 아직 안 본 것과 아니라고 본 것이 구분되지 않는다).
석박통합_ENUM = ["", "석박통합"]
#: 논문에서 이 사람의 위치. 주저자 = 제1저자(공동 1저자 포함) 또는 교신저자
저자구분_ENUM = ["주저자", "공저자"]
#: 특허 진행 상태
특허상태_ENUM = ["등록", "출원", "불명"]

#: 계산해서 나오는 열 (사람이 표에서 직접 못 고친다)
COUNT_COLUMNS: tuple[str, ...] = (
    "저널_수", "저널_주저자_수", "학회_수", "학회_주저자_수",
    "특허_등록_수", "특허_출원_수",
)

# ---------------------------------------------------------------------------
# 섹션별 JSON 스키마 (guided_json 에 그대로 들어간다)
# ---------------------------------------------------------------------------

SECTION_BASIC: dict = {
    "type": "object",
    "properties": {
        "한글_이름": {"type": "string"},
        "영문_이름": {"type": "string"},
        "한글_이름_출처": {"type": "string", "enum": ["원문", "추정", "없음"]},
        "영문_이름_출처": {"type": "string", "enum": ["원문", "추정", "없음"]},
        "생년월일": {"type": "string"},
        "전화번호": {"type": "string"},
        "이메일": {"type": "string"},
        "현재_신분": {"type": "string", "enum": 현재_신분_ENUM},
        "현재_소속": {"type": "string"},
        "현재_소속_상세": {"type": "string"},
        "현재_지도교수": {"type": "string"},
    },
    "required": ["한글_이름", "영문_이름", "한글_이름_출처", "영문_이름_출처", "현재_신분"],
}

SECTION_EDUCATION: dict = {
    "type": "object",
    "properties": {
        "박사_학교": {"type": "string"},
        "박사_전공": {"type": "string"},
        "박사_지도교수": {"type": "string"},
        "박사_시작": {"type": "string"},
        "박사_졸업": {"type": "string"},
        "박사_학위상태": {"type": "string", "enum": 학위상태_ENUM},
        "석사_학교": {"type": "string"},
        "석사_전공": {"type": "string"},
        "석사_지도교수": {"type": "string"},
        "석사_시작": {"type": "string"},
        "석사_졸업": {"type": "string"},
        "학사_학교": {"type": "string"},
        "학사_전공": {"type": "string"},
        "학사_시작": {"type": "string"},
        "학사_졸업": {"type": "string"},
        "석박통합_여부": {"type": "boolean"},
    },
    "required": [],
}

SECTION_RESEARCH: dict = {
    "type": "object",
    "properties": {
        # 예전에는 제1저자 논문만 받았다. 이제 **전부** 받고 저자구분으로 나눈다.
        # 저널/학회 총 편수를 세려면 공저자 논문도 있어야 한다.
        "논문": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    # 제목이 그 사람의 분야를 가장 잘 알려준다. 연구분야 키워드와
                    # 과제 매칭이 이걸 읽는다.
                    "제목": {"type": "string"},
                    "제출처": {"type": "string"},
                    "연도": {"type": "string"},
                    "유형": {"type": "string", "enum": ["학회", "저널", "기타"]},
                    "국내해외": {"type": "string", "enum": ["국내", "해외", "불명"]},
                    "저자구분": {"type": "string", "enum": 저자구분_ENUM},
                },
                "required": ["제출처", "저자구분"],
            },
        },
        "특허": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "제목": {"type": "string"},
                    "상태": {"type": "string", "enum": 특허상태_ENUM},
                    "연도": {"type": "string"},
                    "번호": {"type": "string"},
                },
                "required": ["상태"],
            },
        },
        # required 에 넣어야 guided_json 이 이 키를 **반드시** 내게 만든다.
        # 안 넣으면 모델이 조용히 빼먹고, 열이 빈 채로 저장된다.
        "연구분야_키워드": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["논문", "연구분야_키워드"],
}

SECTION_CAREER: dict = {
    "type": "object",
    "properties": {
        "경력": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "회사": {"type": "string"},
                    "직무": {"type": "string"},
                    "시작": {"type": "string"},
                    "종료": {"type": "string"},
                    "인턴여부": {"type": "boolean"},
                },
                "required": ["회사"],
            },
        }
    },
    "required": ["경력"],
}


#: 네 섹션을 한 덩어리로 — **한 번의 호출로 전부** 뽑을 때 쓴다.
#:
#: 섹션을 나눠 부르면 2~5번째 호출이 매번 CV 전문을 다시 보낸다. CV 가 8천
#: 토큰이면 입력만 4만 토큰이고, 느린 원인의 대부분이 여기다. 하나로 합치면
#: 원문을 한 번만 보낸다.
#:
#: 여기서 **새 항목을 만들지 않는다.** 위 네 스키마를 그대로 물려받아서,
#: 나눠 부르든 한 번에 부르든 나오는 모양이 같도록 한다. 한쪽만 고치면
#: 두 길이 조용히 어긋난다.
SECTION_ALL: dict = {
    "type": "object",
    "properties": {
        "basic": SECTION_BASIC,
        "education": SECTION_EDUCATION,
        "research": SECTION_RESEARCH,
        "career": SECTION_CAREER,
    },
    "required": ["basic", "education", "research", "career"],
}


# ---------------------------------------------------------------------------
# Pydantic 모델
# ---------------------------------------------------------------------------
class Paper(BaseModel):
    제목: str = ""
    제출처: str = ""
    연도: str = ""
    유형: str = ""
    국내해외: str = "불명"
    #: 예전 레코드에는 제1저자 논문만 들어 있었다. 그래서 기본이 주저자다 —
    #: 값을 안 채우고 저장된 옛 데이터가 갑자기 공저자로 바뀌면 안 된다.
    저자구분: str = "주저자"

    @property
    def 주저자(self) -> bool:
        return self.저자구분 != "공저자"


class Patent(BaseModel):
    제목: str = ""
    상태: str = "불명"          # 등록 / 출원 / 불명
    연도: str = ""
    번호: str = ""


class Career(BaseModel):
    회사: str = ""
    직무: str = ""
    시작: str = ""
    종료: str = ""
    인턴여부: bool = False


class CVRecord(BaseModel):
    """엑셀 한 줄에 대응하는 지원자 레코드."""

    지원자_ID: str = ""
    한글_이름: str = ""
    영문_이름: str = ""
    생년월일: str = ""
    전화번호: str = ""
    이메일: str = ""

    현재_신분: str = ""
    현재_소속: str = ""
    현재_소속_상세: str = ""
    현재_지도교수: str = ""

    박사_석박통합: str = ""     # "석박통합" 또는 빈칸
    박사_학교: str = ""
    박사_전공: str = ""
    박사_지도교수: str = ""
    박사_시작: str = ""
    박사_졸업: str = ""
    박사_학위상태: str = ""

    석사_학교: str = ""
    석사_전공: str = ""
    석사_지도교수: str = ""
    석사_시작: str = ""
    석사_졸업: str = ""

    학사_학교: str = ""
    학사_전공: str = ""
    학사_시작: str = ""
    학사_졸업: str = ""

    논문: list[Paper] = Field(default_factory=list)
    특허: list[Patent] = Field(default_factory=list)
    연구분야_키워드: str = ""
    경력_요약: str = ""
    # 가장 최근 경력 하나를 열로도 뽑아 둔다 (6개월 미만·인턴 제외)
    경력_회사: str = ""
    직책: str = ""
    경력_시작: str = ""
    경력_종료: str = ""

    검토_필요: str = ""
    검토_사유: str = ""

    # 엑셀에는 안 나가지만 DB/화면에서는 유지하는 항목
    원본_파일명: str = ""
    추출_일시: str = ""

    def papers_view(self, registry=None) -> list[dict]:
        """논문을 화면에 보일 형태로 푼다.

        ⚠️ 저장된 값을 고치지 않는다. 대표명·등급·국내해외를 **볼 때마다**
        사전에서 다시 읽는다. 그래야 관리화면에서 등급을 바꾸면 이미 등록된
        지원자 표에도 곧바로 반영된다.
        """
        out = []
        for p in self.논문:
            종류 = "저널" if p.유형 == "저널" else "학회"
            표시명, 등급, 국내해외 = p.제출처, "", p.국내해외
            if registry is not None and p.제출처:
                found = registry.lookup(종류, p.제출처)
                if found is not None:
                    표시명 = found.표시명
                    등급 = found.등급
                    # 담당자가 판별한 값이 LLM 추측을 이긴다
                    if found.국내해외 in ("국내", "해외"):
                        국내해외 = found.국내해외
            # 학회/저널 구분도 담당자가 판별한 값이 LLM 추측을 이긴다
            if registry is not None and p.제출처:
                found = registry.lookup(종류, p.제출처)
                if found is not None and found.유형 in ("학회", "저널"):
                    종류 = found.유형
            out.append(
                {"제목": p.제목, "표시명": 표시명, "연도": p.연도, "등급": 등급,
                 "국내해외": 국내해외, "유형": 종류, "주저자": p.주저자}
            )
        return out

    def 논문_수(self, registry=None) -> dict[str, int]:
        """저널·학회를 전체와 주저자로 나눠 센다.

        주저자 = 제1저자(공동 1저자 포함) 또는 교신저자. 옛 레코드에는 제1저자
        논문만 들어 있어 전부 주저자로 잡힌다 — 그게 맞다.
        """
        센것 = {"저널_수": 0, "저널_주저자_수": 0, "학회_수": 0, "학회_주저자_수": 0}
        for v in self.papers_view(registry):
            머리 = "저널" if v["유형"] == "저널" else "학회"
            센것[f"{머리}_수"] += 1
            if v["주저자"]:
                센것[f"{머리}_주저자_수"] += 1
        return 센것

    def 특허_수(self) -> dict[str, int]:
        """등록·출원을 따로 센다. 상태를 모르는 것은 어느 쪽에도 안 넣는다."""
        센것 = {"특허_등록_수": 0, "특허_출원_수": 0}
        for pt in self.특허:
            if pt.상태 == "등록":
                센것["특허_등록_수"] += 1
            elif pt.상태 == "출원":
                센것["특허_출원_수"] += 1
        return 센것

    def 해외논문_제출처(self, registry=None) -> str:
        """해외 학회/저널 1저자 논문만 한 열로 합친다."""
        from .normalize import MULTI_SEP

        items = [
            f"{v['표시명']} {v['연도']}".strip()
            for v in self.papers_view(registry)
            if v["국내해외"] == "해외" and v["주저자"]
        ]
        return MULTI_SEP.join(items)

    def 등급별_해외논문_수(self, registry=None) -> dict[str, int]:
        counts: dict[str, int] = {}
        for v in self.papers_view(registry):
            if v["국내해외"] == "해외" and v["주저자"] and v["등급"]:
                counts[v["등급"]] = counts.get(v["등급"], 0) + 1
        return counts

    def to_row(self, registry=None) -> dict[str, str]:
        """표 한 줄(dict)로 변환.

        registry 를 주면 학교·전공·학회 이름이 대표명으로 바뀌고
        등급별 논문 수 열이 붙는다.
        """
        data = self.model_dump()
        data["1저자_해외논문_제출처"] = self.해외논문_제출처(registry)
        # 세어 나오는 값. 0 은 빈칸으로 둔다 — 표가 0 으로 도배되면 안 읽힌다.
        for 열, 값 in {**self.논문_수(registry), **self.특허_수()}.items():
            data[열] = 값 or ""

        if registry is not None:
            from .normalize import MULTI_SEP

            for col, 종류 in NAME_COLUMNS.items():
                raw = str(data.get(col, "") or "")
                if not raw:
                    continue
                # 값이 여러 개면 각각 대표명으로 바꾼다
                parts = [registry.display(종류, part) for part in raw.split(MULTI_SEP)]
                data[col] = MULTI_SEP.join(parts)

            counts = self.등급별_해외논문_수(registry)
            for tier in registry.column_tiers():
                data[f"{TIER_COLUMN_PREFIX}{tier}"] = counts.get(tier, 0) or ""

        cols = columns(registry)
        return {col: str(data.get(col, "") or "") for col in cols}

    def pii_fields(self) -> dict:
        """벡터 DB payload 에 절대 넣으면 안 되는 개인식별정보."""
        return {
            "한글_이름": self.한글_이름,
            "영문_이름": self.영문_이름,
            "생년월일": self.생년월일,
            "전화번호": self.전화번호,
            "이메일": self.이메일,
        }
