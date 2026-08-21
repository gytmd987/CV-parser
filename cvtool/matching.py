"""지원자 ↔ 연구 과제 매칭.

CV 에서 뽑아둔 정보로 **어느 과제와 잘 맞는지**를 점수와 **판단 사유**까지 낸다.

## 모든 과제와 비교한다

예전에는 과제가 많으면 임베딩으로 후보를 좁혀 **그 안에서만** 점수를 매겼다.
그래서 좁히기가 잘못되면(임베딩이 죽어 있으면 파일 순서대로 앞에서 잘랐다)
정작 잘 맞는 과제가 LLM 에 가보지도 못했다.

지금은 **과제를 전부 비교한다.** 다만 한 번에 다 넣으면 답이 길어져 잘리므로
몇 개씩 나눠 묻고 합친다. 임베딩은 **묻는 순서를 정하는 데만** 쓰고, 없어도
결과는 같다.

## 점수는 눈금을 정해놓고 매긴다

`SCORE_RUBRIC` 을 프롬프트에 그대로 넣어, 90점이 무슨 뜻인지 모델과 사람이 같은
기준을 보게 한다. 그래도 **LLM 의 판단이지 측정값이 아니다.** 그래서 화면에는
등급(매우 적합/적합/…)을 함께 보여주고, 재현 가능한 임베딩 유사도를 따로 적어
교차 확인할 수 있게 한다.

⚠️ 모델을 새로 올리지 않는다. 이미 떠 있는 vLLM·TEI 를 **부르기만** 한다.
⚠️ 사유는 **CV 에 있는 내용에 근거**해야 한다. 없는 경력을 지어내면 사람이
   그대로 믿고 면접에 들어간다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .clients.llm import LLMClient, LLMError
from .config import settings
from .projects import Project

#: 한 번에 물어볼 과제 수. 답이 길어지면 잘리므로 작게 나눠 묻는다.
DEFAULT_BATCH = 5

#: 점수의 뜻. 프롬프트에 그대로 넣어 모델과 사람이 같은 눈금을 본다.
SCORE_RUBRIC = (
    "90~100: 과제의 핵심 기술·연구 주제가 지원자 경력과 그대로 일치. 바로 투입 가능\n"
    "70~89 : 주요 기술이 상당 부분 겹침. 일부만 익히면 됨\n"
    "50~69 : 인접 분야. 기반은 있으나 과제 핵심 기술 경험은 없음\n"
    "30~49 : 전공·기초 소양만 겹침. 실제 경험 없음\n"
    "0~29  : 접점이 거의 없음"
)

MATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "결과": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "과제키": {"type": "string"},
                    "점수": {"type": "integer", "minimum": 0, "maximum": 100},
                    "사유": {"type": "string"},
                    "근거": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["과제키", "점수", "사유"],
            },
        }
    },
    "required": ["결과"],
}


@dataclass
class Match:
    과제키: str
    과제명: str
    점수: int
    사유: str
    근거: list[str] = field(default_factory=list)
    유사도: float | None = None      # 임베딩 코사인. 재현 가능한 참고값
    평가됨: bool = True              # 모델이 답하지 않은 과제는 False

    @property
    def 등급(self) -> str:
        if not self.평가됨:
            return "미평가"
        if self.점수 >= 90:
            return "매우 적합"
        if self.점수 >= 70:
            return "적합"
        if self.점수 >= 50:
            return "인접 분야"
        if self.점수 >= 30:
            return "기초만 겹침"
        return "접점 없음"


#: 매칭 판단에 넣지 않는 학력 단계.
#:
#: 학사 전공은 **10년쯤 전의 이야기**다. 학사가 광학이고 석·박사가 다른
#: 분야인 사람에게 광학 과제를 1순위로 붙여 준 일이 있었다. 모델은 프로필에
#: 있는 단어를 과제 키워드와 맞추려 하므로, 판단에 쓰면 안 되는 정보는
#: 애초에 **넣지 않는 것**이 유일하게 확실한 방법이다.
#: (학사 정보 자체는 표·엑셀에 그대로 남는다. 매칭에만 안 쓴다.)
MATCH_SKIP_DEGREES = ("학사",)
MATCH_DEGREES = ("박사", "석사")


def candidate_profile(rec, registry=None) -> str:
    """매칭 판단에 쓸 지원자 요약.

    이름·연락처는 **넣지 않는다.** 판단에 필요 없고, 넣으면 사람 이름을 보고
    엉뚱한 판단을 할 여지만 생긴다.

    학사 학력도 **넣지 않는다** (MATCH_SKIP_DEGREES 참고).
    """
    row = rec.to_row(registry) if registry is not None else rec.to_row()
    줄 = []

    def 넣기(라벨: str, 값: str) -> None:
        값 = str(값 or "").strip()
        if 값:
            줄.append(f"{라벨}: {값}")

    넣기("현재 신분", row.get("현재_신분"))
    넣기("현재 소속", row.get("현재_소속"))
    넣기("현재 소속 상세", row.get("현재_소속_상세"))
    for 단계 in MATCH_DEGREES:
        학교, 전공 = row.get(f"{단계}_학교"), row.get(f"{단계}_전공")
        if 학교 or 전공:
            상태 = row.get(f"{단계}_학위상태", "")
            넣기(f"{단계}", " ".join(x for x in (학교, 전공, 상태) if x))
    넣기("연구분야 키워드", row.get("연구분야_키워드"))
    넣기("1저자 해외논문", row.get("1저자_해외논문_제출처"))

    # 최근 경력을 앞세운다. 요약은 전부 이어 붙인 것이라 뒤에 둔다.
    최근 = " ".join(x for x in (
        str(row.get("경력_회사") or ""), str(row.get("직책") or ""),
    ) if x).strip()
    기간 = " ".join(x for x in (
        str(row.get("경력_시작") or ""), str(row.get("경력_종료") or ""),
    ) if x).strip()
    if 최근:
        넣기("최근 경력", f"{최근} ({기간})" if 기간 else 최근)
    넣기("경력 전체", row.get("경력_요약"))

    논문 = getattr(rec, "논문", []) or []
    제출처 = [p.제출처 for p in 논문 if getattr(p, "제출처", "") and p.주저자][:15]
    if 제출처:
        줄.append("주저자 논문 제출처: " + " / ".join(제출처))
    특허 = getattr(rec, "특허", []) or []
    제목들 = [pt.제목 for pt in 특허 if getattr(pt, "제목", "")][:8]
    if 제목들:
        줄.append("특허: " + " / ".join(제목들))
    return "\n".join(줄)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    점 = sum(x * y for x, y in zip(a, b))
    갑 = math.sqrt(sum(x * x for x in a))
    을 = math.sqrt(sum(y * y for y in b))
    return 점 / (갑 * 을) if 갑 and 을 else 0.0


def similarities(profile: str, projects: list[Project],
                 embed_client=None) -> dict[str, float]:
    """과제별 임베딩 유사도. 임베딩이 없으면 빈 사전.

    **재현 가능한 값**이라 LLM 점수를 교차 확인하는 데 쓴다.
    실패해도 매칭 자체는 그대로 진행한다 — 순서 정하기와 참고용일 뿐이다.
    """
    if embed_client is None or not projects:
        return {}
    try:
        벡터 = embed_client.embed([profile] + [p.요약(600) for p in projects])
    except Exception:  # noqa: BLE001 - 임베딩이 죽어도 매칭은 되어야 한다
        return {}
    기준, 나머지 = 벡터[0], 벡터[1:]
    return {p.키: round(_cosine(기준, v), 4) for p, v in zip(projects, 나머지)}


def _prompt(profile: str, 후보: list[Project]) -> list[dict]:
    과제글 = "\n\n".join(f"[과제키: {p.키}]\n{p.요약()}" for p in 후보)
    키목록 = ", ".join(p.키 for p in 후보)
    지시 = (
        "너는 연구개발 채용 담당자다. 지원자 정보와 회사 연구 과제를 보고 "
        "**과제마다** 얼마나 맞는지 0~100 점으로 매기고 이유를 쓴다.\n\n"
        "[점수 기준] 이 눈금을 그대로 따른다. 느낌으로 매기지 마라.\n"
        f"{SCORE_RUBRIC}\n\n"
        "[규칙]\n"
        f"- 준 과제 {len(후보)}개를 **하나도 빠짐없이** 채점한다. 과제키: {키목록}\n"
        "- **지원자 정보에 없는 내용을 지어내지 마라.** 근거는 지원자 정보에 적힌 "
        "표현을 그대로 인용한다.\n"
        "- 맞는 구석이 없으면 낮은 점수를 주고 그 이유를 쓴다. 억지로 맞추지 마라.\n"
        "- **박사 과정과 최근 연구·경력을 중심으로** 판단해라. 오래된 이력에서 "
        "단어 하나가 겹친다고 점수를 올리지 마라.\n"
        "- 사유는 한국어 두세 문장. **무엇이 겹치고 무엇이 부족한지 함께** 쓰고, "
        "왜 그 점수 구간인지 밝힌다.\n"
        "- 준 과제키만 쓴다. 목록에 없는 과제를 만들지 마라.\n"
        "- 아래 형태의 JSON 객체 하나만 출력한다.\n"
        '{"결과": [{"과제키": "...", "점수": 0, "사유": "...", '
        '"근거": ["지원자 정보에서 인용"]}]}'
    )
    본문 = (
        f"[지원자 정보]\n{profile or '(정보 없음)'}\n\n"
        f"[연구 과제 {len(후보)}개]\n{과제글}"
    )
    return [
        {"role": "system", "content": 지시},
        {"role": "user", "content": 본문},
    ]


def _한묶음(profile: str, 후보: list[Project], llm: LLMClient) -> dict[str, dict]:
    데이터 = llm.chat_json(
        _prompt(profile, 후보), MATCH_SCHEMA,
        temperature=settings.llm_temperature, schema_name="project_match",
    )
    이름표 = {p.키 for p in 후보}
    나온것: dict[str, dict] = {}
    for 항목 in 데이터.get("결과") or []:
        if not isinstance(항목, dict):
            continue
        키 = str(항목.get("과제키") or "").strip()
        if 키 not in 이름표:          # 목록에 없는 과제는 버린다 (지어낸 것)
            continue
        나온것[키] = 항목
    return 나온것


def match(profile: str, projects: list[Project], *, client: LLMClient | None = None,
          embed_client=None, batch: int | None = None) -> list[Match]:
    """지원자 요약 -> **모든 과제**의 점수·사유 (점수 높은 순).

    한 번에 다 묻지 않고 몇 개씩 나눠 묻는다. 답이 길어져 잘리면 결과가 통째로
    사라지기 때문이다. 답하지 않은 과제는 한 번 더 묻고, 그래도 없으면
    '미평가' 로 남긴다 — 조용히 빼면 사람이 비교된 줄 안다.
    """
    if not projects:
        return []
    if not (profile or "").strip():
        raise LLMError("매칭할 지원자 정보가 비어 있습니다. CV 추출 결과를 확인하세요.")

    유사도 = similarities(profile, projects, embed_client)
    # 비슷해 보이는 것부터 묻는다 (결과는 같지만 앞쪽 묶음이 더 유용하다)
    순서 = sorted(projects, key=lambda p: -유사도.get(p.키, 0.0)) if 유사도 else list(projects)
    크기 = max(1, batch or settings.match_batch)

    llm = client or LLMClient()
    닫아야 = client is None
    답: dict[str, dict] = {}
    try:
        for i in range(0, len(순서), 크기):
            묶음 = 순서[i:i + 크기]
            답.update(_한묶음(profile, 묶음, llm))
            빠진것 = [p for p in 묶음 if p.키 not in 답]
            if 빠진것:                       # 한 번만 다시 묻는다
                답.update(_한묶음(profile, 빠진것, llm))
    finally:
        if 닫아야:
            llm.close()

    나온것: list[Match] = []
    for p in projects:
        항목 = 답.get(p.키)
        if 항목 is None:
            나온것.append(Match(과제키=p.키, 과제명=p.이름, 점수=0,
                              사유="모델이 이 과제를 채점하지 않았습니다. 다시 돌려보세요.",
                              유사도=유사도.get(p.키), 평가됨=False))
            continue
        try:
            점수 = int(항목.get("점수") or 0)
        except (TypeError, ValueError):
            점수 = 0
        근거 = 항목.get("근거") or []
        if isinstance(근거, str):
            근거 = [근거]
        나온것.append(Match(
            과제키=p.키, 과제명=p.이름, 점수=max(0, min(100, 점수)),
            사유=str(항목.get("사유") or "").strip(),
            근거=[str(x).strip() for x in 근거 if str(x).strip()][:5],
            유사도=유사도.get(p.키),
        ))
    나온것.sort(key=lambda m: (m.평가됨, m.점수), reverse=True)
    return 나온것
