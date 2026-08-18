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
    "지원자_ID",
    "한글_이름",
    "영문_이름",
    "이름_추정여부",
    "생년월일",
    "전화번호",
    "이메일",
    # B. 현재
    "현재_신분",
    "현재_소속",
    "현재_소속_상세",
    "현재_지도교수",
    # C. 박사
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
    "연구분야_키워드",
    # G. 경력
    "경력_요약",
    # H. 검토
    "검토_필요",
    "검토_사유",
]

#: 명칭 사전으로 대표명을 붙일 열 (열 이름 -> 사전 종류)
NAME_COLUMNS: dict[str, str] = {
    "현재_소속": "학교",
    "박사_학교": "학교",
    "석사_학교": "학교",
    "학사_학교": "학교",
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
        "1저자_논문": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "제출처": {"type": "string"},
                    "연도": {"type": "string"},
                    "유형": {"type": "string", "enum": ["학회", "저널", "기타"]},
                    "국내해외": {"type": "string", "enum": ["국내", "해외", "불명"]},
                },
                "required": ["제출처"],
            },
        },
        "연구분야_키워드": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["1저자_논문"],
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
                },
                "required": ["회사"],
            },
        }
    },
    "required": ["경력"],
}


# ---------------------------------------------------------------------------
# Pydantic 모델
# ---------------------------------------------------------------------------
class Paper(BaseModel):
    제출처: str = ""
    연도: str = ""
    유형: str = ""
    국내해외: str = "불명"


class Career(BaseModel):
    회사: str = ""
    직무: str = ""
    시작: str = ""
    종료: str = ""


class CVRecord(BaseModel):
    """엑셀 한 줄에 대응하는 지원자 레코드."""

    지원자_ID: str = ""
    한글_이름: str = ""
    영문_이름: str = ""
    이름_추정여부: str = ""
    생년월일: str = ""
    전화번호: str = ""
    이메일: str = ""

    현재_신분: str = ""
    현재_소속: str = ""
    현재_소속_상세: str = ""
    현재_지도교수: str = ""

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
    연구분야_키워드: str = ""
    경력_요약: str = ""

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
            out.append(
                {"표시명": 표시명, "연도": p.연도, "등급": 등급, "국내해외": 국내해외}
            )
        return out

    def 해외논문_제출처(self, registry=None) -> str:
        """해외 학회/저널 1저자 논문만 한 열로 합친다."""
        from .normalize import MULTI_SEP

        items = [
            f"{v['표시명']} {v['연도']}".strip()
            for v in self.papers_view(registry)
            if v["국내해외"] == "해외"
        ]
        return MULTI_SEP.join(items)

    def 등급별_해외논문_수(self, registry=None) -> dict[str, int]:
        counts: dict[str, int] = {}
        for v in self.papers_view(registry):
            if v["국내해외"] == "해외" and v["등급"]:
                counts[v["등급"]] = counts.get(v["등급"], 0) + 1
        return counts

    def to_row(self, registry=None) -> dict[str, str]:
        """표 한 줄(dict)로 변환.

        registry 를 주면 학교·전공·학회 이름이 대표명으로 바뀌고
        등급별 논문 수 열이 붙는다.
        """
        data = self.model_dump()
        data["1저자_해외논문_제출처"] = self.해외논문_제출처(registry)

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
