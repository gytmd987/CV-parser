"""지원자 ↔ 연구 과제 매칭.

CV 에서 뽑아둔 정보로 **어느 과제와 잘 맞는지**를 점수와 **판단 사유**까지 낸다.

두 단계로 나눈다.
  1. 과제가 많으면 임베딩(TEI)으로 **후보를 좁힌다.** 수십 개를 통째로 LLM 에
     넣으면 프롬프트가 길어져 잘리고 느리다.
  2. 좁힌 후보만 LLM 에 넣어 점수와 사유를 받는다.

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

#: LLM 에 한 번에 넣을 과제 수 상한. 넘으면 임베딩으로 먼저 좁힌다.
DEFAULT_TOP = 8

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

    @property
    def 등급(self) -> str:
        if self.점수 >= 80:
            return "매우 적합"
        if self.점수 >= 60:
            return "적합"
        if self.점수 >= 40:
            return "보통"
        return "낮음"


def candidate_profile(rec, registry=None) -> str:
    """매칭 판단에 쓸 지원자 요약.

    이름·연락처는 **넣지 않는다.** 판단에 필요 없고, 넣으면 사람 이름을 보고
    엉뚱한 판단을 할 여지만 생긴다.
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
    for 단계 in ("박사", "석사", "학사"):
        학교, 전공 = row.get(f"{단계}_학교"), row.get(f"{단계}_전공")
        if 학교 or 전공:
            상태 = row.get(f"{단계}_학위상태", "")
            넣기(f"{단계}", " ".join(x for x in (학교, 전공, 상태) if x))
    넣기("연구분야 키워드", row.get("연구분야_키워드"))
    넣기("1저자 해외논문", row.get("1저자_해외논문_제출처"))
    넣기("경력", row.get("경력_요약"))

    논문 = getattr(rec, "논문", []) or []
    제목들 = [p.제목 for p in 논문 if getattr(p, "제목", "")][:12]
    if 제목들:
        줄.append("논문 제목: " + " / ".join(제목들))
    경력 = getattr(rec, "경력", []) or []
    상세 = [
        " ".join(x for x in (getattr(c, "회사", ""), getattr(c, "직무", ""),
                             getattr(c, "설명", "")) if x)
        for c in 경력
    ]
    상세 = [x for x in 상세 if x.strip()][:8]
    if 상세:
        줄.append("경력 상세: " + " / ".join(상세))
    return "\n".join(줄)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    점 = sum(x * y for x, y in zip(a, b))
    갑 = math.sqrt(sum(x * x for x in a))
    을 = math.sqrt(sum(y * y for y in b))
    return 점 / (갑 * 을) if 갑 and 을 else 0.0


def _글자겹침(profile: str, p: Project) -> float:
    """임베딩을 못 쓸 때의 대비책. 정교하진 않지만 없는 것보단 낫다."""
    def 조각(글: str) -> set[str]:
        return {x for x in "".join(
            c if c.isalnum() else " " for c in 글.lower()
        ).split() if len(x) >= 2}

    가 = 조각(profile)
    나 = 조각(p.요약())
    return len(가 & 나) / len(나) if 나 else 0.0


def shortlist(profile: str, projects: list[Project], *, top: int = DEFAULT_TOP,
              embed_client=None) -> list[Project]:
    """LLM 에 넣을 후보를 좁힌다. 과제가 적으면 그대로 둔다."""
    if len(projects) <= top:
        return list(projects)

    if embed_client is not None:
        try:
            벡터 = embed_client.embed([profile] + [p.요약(600) for p in projects])
            기준, 나머지 = 벡터[0], 벡터[1:]
            점수 = [(_cosine(기준, v), p) for v, p in zip(나머지, projects)]
            점수.sort(key=lambda x: -x[0])
            return [p for _, p in 점수[:top]]
        except Exception:  # noqa: BLE001 - 임베딩이 죽어도 매칭은 되어야 한다
            pass

    점수 = [(_글자겹침(profile, p), p) for p in projects]
    점수.sort(key=lambda x: -x[0])
    return [p for _, p in 점수[:top]]


def _prompt(profile: str, 후보: list[Project]) -> list[dict]:
    과제글 = "\n\n".join(
        f"[과제키: {p.키}]\n{p.요약()}" for p in 후보
    )
    지시 = (
        "너는 연구개발 채용 담당자다. 지원자 정보와 회사 연구 과제 목록을 보고 "
        "**각 과제와 얼마나 맞는지** 0~100 점으로 매기고 이유를 쓴다.\n\n"
        "규칙:\n"
        "- 점수는 전공·연구 주제·사용 기술·경력이 과제와 겹치는 정도로 매긴다.\n"
        "- **지원자 정보에 없는 내용을 지어내지 마라.** 근거는 지원자 정보에 적힌 "
        "표현을 그대로 인용한다.\n"
        "- 맞는 구석이 없으면 낮은 점수를 주고 그 이유를 쓴다. 억지로 맞추지 마라.\n"
        "- 사유는 한국어 두세 문장. 무엇이 겹치고 무엇이 부족한지 함께 쓴다.\n"
        "- 준 과제키만 쓴다. 목록에 없는 과제를 만들지 마라.\n"
        "- 아래 형태의 JSON 객체 하나만 출력한다.\n"
        '{"결과": [{"과제키": "...", "점수": 0, "사유": "...", '
        '"근거": ["지원자 정보에서 인용"]}]}'
    )
    본문 = (
        f"[지원자 정보]\n{profile or '(정보 없음)'}\n\n"
        f"[연구 과제 목록]\n{과제글}"
    )
    return [
        {"role": "system", "content": 지시},
        {"role": "user", "content": 본문},
    ]


def match(profile: str, projects: list[Project], *, client: LLMClient | None = None,
          embed_client=None, top: int = DEFAULT_TOP) -> list[Match]:
    """지원자 요약 -> 과제별 점수·사유 (점수 높은 순)."""
    if not projects:
        return []
    if not (profile or "").strip():
        raise LLMError("매칭할 지원자 정보가 비어 있습니다. CV 추출 결과를 확인하세요.")

    후보 = shortlist(profile, projects, top=top, embed_client=embed_client)
    이름표 = {p.키: p.이름 for p in 후보}

    llm = client or LLMClient()
    닫아야 = client is None
    try:
        데이터 = llm.chat_json(
            _prompt(profile, 후보), MATCH_SCHEMA,
            temperature=settings.llm_temperature, schema_name="project_match",
        )
    finally:
        if 닫아야:
            llm.close()

    나온것: list[Match] = []
    for 항목 in 데이터.get("결과") or []:
        if not isinstance(항목, dict):
            continue
        키 = str(항목.get("과제키") or "").strip()
        if 키 not in 이름표:          # 목록에 없는 과제는 버린다 (지어낸 것)
            continue
        try:
            점수 = int(항목.get("점수") or 0)
        except (TypeError, ValueError):
            점수 = 0
        근거 = 항목.get("근거") or []
        if isinstance(근거, str):
            근거 = [근거]
        나온것.append(Match(
            과제키=키, 과제명=이름표[키], 점수=max(0, min(100, 점수)),
            사유=str(항목.get("사유") or "").strip(),
            근거=[str(x).strip() for x in 근거 if str(x).strip()][:5],
        ))
    나온것.sort(key=lambda m: -m.점수)
    return 나온것
