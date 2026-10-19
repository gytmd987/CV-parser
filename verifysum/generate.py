"""2단계 — 후보자 검증 자료 → 1장 결과 + 요약표 한 줄.

한 사람에 LLM 한 번이 기본이다. 답을 받으면 **코드로 검사**한다.

  - 요약 두 칸: 칸 너비·글자 크기로 줄 수를 세서 두 줄을 넘으면 다시 쓰게 한다.
  - 1장 표   : 예시 1장 표의 높이(= 한 쪽에 들어간 양)를 넘으면 줄이게 한다.

두 번 고쳐도 안 되면, 요약 칸은 띄어쓰기 단위로 잘라 **두 줄을 지킨다** (규칙이
'절대' 이므로). 1장은 자르지 않고 '넘칠 수 있음' 경고만 남긴다.

제목 · 소속 · 항목명 · 결과(추천)는 LLM 에 맡기지 않고 원본에서 그대로 가져온다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import docxio as D
from . import fit
from .learn import 예시글

#: 고쳐 쓰게 하는 횟수 (첫 답 포함 최대 1 + 이것)
고쳐쓰기 = 2


@dataclass
class 결과:
    자료: D.검증자료
    항목: list[tuple[str, str]] = field(default_factory=list)
    전문성: str = ""
    역량: str = ""
    작성자: list[str] = field(default_factory=list)
    경고: list[str] = field(default_factory=list)


def _한줄(학습: dict, 칸: str) -> float:
    c = 학습["칸"][칸]
    return c["폭_pt"] / c["크기_pt"]


def 목표글자(자료: D.검증자료, 학습: dict) -> list[int]:
    """항목마다 대략 몇 자로 줄일지. 예시 1장 글자 수를 이 사람 원문 분량에 맞춰 조금 늘리고 줄인다."""
    예원, 예한 = 학습["분량"]["원본글자"], 학습["분량"]["1장글자"]
    평균 = int(sum(예한) / len(예한)) if 예한 else 120
    나온것 = []
    for i, (_a, b) in enumerate(자료.항목):
        기준 = 예한[i] if i < len(예한) else 평균
        원 = 예원[i] if i < len(예원) and 예원[i] else max(len(b), 1)
        비 = min(1.2, max(0.7, len(b) / 원))
        나온것.append(max(20, round(기준 * 비)))
    return 나온것


def _스키마(n: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "검증의견": {"type": "array", "items": {"type": "string"}, "minItems": n, "maxItems": n},
            "전문성_성과": {"type": "string"},
            "연구전문가_역량": {"type": "string"},
            "작성자": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["검증의견", "전문성_성과", "연구전문가_역량", "작성자"],
    }


def 프롬프트(자료: D.검증자료, 학습: dict) -> list[dict]:
    목표 = 목표글자(자료, 학습)
    전문 = fit.줄에_맞는_글자수(_한줄(학습, "전문성"))
    역량 = fit.줄에_맞는_글자수(_한줄(학습, "역량"))
    분량 = "\n".join(f"  {i + 1}) {a} — 약 {t}자" for i, ((a, _b), t) in enumerate(zip(자료.항목, 목표)))
    지시 = (
        "너는 연구자 검증 자료를 정리하는 담당자다. 여러 사람이 각자 쓴 검증 의견을 "
        "하나의 문체로 다듬어 (1) 워드 1장에 들어가게 줄인 검증 의견과 (2) 요약표 두 칸을 만든다.\n\n"
        f"[작성 지침 — 예시에서 정리한 것]\n{학습.get('지침') or ''}\n\n"
        "[규칙]\n"
        "- 원문에 없는 사실·수치·평가를 지어내지 마라. 원문을 줄이고 다듬기만 한다.\n"
        f"- 검증의견은 항목 순서대로 정확히 {len(자료.항목)}개. 항목명은 쓰지 말고 의견만 쓴다.\n"
        "- 항목별 분량(공백 포함, 대략). 예시 1장 결과의 분량과 같은 수준이다:\n"
        f"{분량}\n"
        "- 줄을 나눌 때는 줄바꿈(\\n)을 쓴다. 예시 1장 결과의 줄 나눔·기호를 따른다.\n"
        f"- 전문성_성과: 무엇에 전문성이 있는지·대표 성과. 공백 포함 {전문}자 이내 — 칸에서 반드시 두 줄 안.\n"
        f"- 연구전문가_역량: 연구자로서의 태도·성품. 공백 포함 {역량}자 이내 — 반드시 두 줄 안.\n"
        "- 작성자: 원문 끝에 적힌 작성자 이름만 순서대로 (직함·'작성자' 같은 말은 빼고).\n"
        "- JSON 객체 하나만 출력한다."
    )
    원문 = (
        f"제목: {자료.제목}\n소속: {자료.소속}\n\n"
        + "\n".join(f"[{a}]\n{b}" for a, b in 자료.항목)
        + f"\n\n<표 뒤 끝부분>\n{자료.끝글 or '(없음)'}"
    )
    return [
        {"role": "system", "content": 지시},
        {"role": "user", "content": f"{예시글(학습)}\n\n========\n<이번 후보자 원본>\n{원문}"},
    ]


def 검사(결: 결과, 학습: dict) -> list[str]:
    """고쳐야 할 것들 (LLM 에게 그대로 보낼 말)."""
    문제 = []
    for 칸, 이름, 글 in (("전문성", "전문성_성과", 결.전문성), ("역량", "연구전문가_역량", 결.역량)):
        줄 = fit.줄수(글, _한줄(학습, 칸))
        if 줄 > 2:
            한도 = fit.줄에_맞는_글자수(_한줄(학습, 칸))
            문제.append(f"{이름}이 칸에서 {줄}줄입니다. 공백 포함 {int(한도 * 0.85)}자 안쪽으로 줄이세요.")
        if not 글.strip():
            문제.append(f"{이름}이 비었습니다.")
    칸1, 칸2 = 학습["칸"]["1장_첫열"], 학습["칸"]["1장_둘째열"]
    높이 = fit.표높이(결.항목, 칸1["폭_pt"] / 칸1["크기_pt"], 칸2["폭_pt"] / 칸2["크기_pt"])
    한도 = 학습["분량"]["표높이"]
    if 한도 and 높이 > 한도 * 1.02:
        넘침 = round((높이 / 한도 - 1) * 100)
        긴것 = sorted(range(len(결.항목)), key=lambda i: -len(결.항목[i][1]))[:3]
        문제.append(f"1장 분량을 {넘침}% 넘습니다. 특히 "
                  + ", ".join(f"'{결.항목[i][0]}'" for i in 긴것) + " 을(를) 더 줄이세요.")
    return 문제


def _받기(답: dict, 자료: D.검증자료) -> 결과:
    의견 = [str(x).strip() for x in (답.get("검증의견") or [])]
    의견 += [""] * (len(자료.항목) - len(의견))
    작성자 = [str(x).strip() for x in (답.get("작성자") or []) if str(x).strip()]
    return 결과(
        자료=자료,
        항목=[(a, 의견[i]) for i, (a, _b) in enumerate(자료.항목)],
        전문성=str(답.get("전문성_성과") or "").strip(),
        역량=str(답.get("연구전문가_역량") or "").strip(),
        작성자=작성자 or D.작성자_찾기(자료.끝글)[:3],
    )


def 한사람(자료: D.검증자료, 학습: dict, client) -> 결과:
    메시지 = 프롬프트(자료, 학습)
    스키마 = _스키마(len(자료.항목))
    답 = client.chat_json(메시지, 스키마, schema_name="verify_summary")
    결 = _받기(답, 자료)
    for _ in range(고쳐쓰기):
        문제 = 검사(결, 학습)
        if not 문제:
            break
        메시지 = 메시지 + [
            {"role": "assistant", "content": json.dumps(답, ensure_ascii=False)},
            {"role": "user", "content": "다음을 고쳐 같은 JSON 형식으로 다시 써라. 나머지는 그대로 둔다.\n- "
             + "\n- ".join(문제)},
        ]
        답 = client.chat_json(메시지, 스키마, schema_name="verify_summary")
        결 = _받기(답, 자료)

    # 마지막 안전장치 — 요약 칸은 두 줄을 절대 넘지 않게
    for 칸, 속성, 이름 in (("전문성", "전문성", "전문성/성과"), ("역량", "역량", "연구전문가 역량")):
        글 = getattr(결, 속성)
        if fit.줄수(글, _한줄(학습, 칸)) > 2:
            setattr(결, 속성, fit.자르기(글, _한줄(학습, 칸)))
            결.경고.append(f"{이름}이 길어 두 줄에 맞춰 뒤를 잘랐습니다 — 확인해 주세요.")
    남은 = [m for m in 검사(결, 학습) if "1장 분량" in m]
    if 남은:
        결.경고.append(남은[0].split(" 특히")[0] + " — 워드에서 한 쪽을 넘는지 확인해 주세요.")
    if len(결.작성자) != 3:
        결.경고.append(f"작성자를 {len(결.작성자)}명 찾았습니다 ({', '.join(결.작성자) or '없음'}).")
    if any(not b for _a, b in 결.항목):
        결.경고.append("비어 있는 검증 의견이 있습니다.")
    return 결


# --- 파일 만들기 --------------------------------------------------------------------
def 한장파일(결과들: list[결과], 틀: bytes) -> bytes:
    return D.한장_만들기(틀, [
        D.한장결과(제목=r.자료.제목, 소속=r.자료.소속, 항목=r.항목, 작성자=r.작성자)
        for r in 결과들])


def 요약파일(결과들: list[결과], 틀: bytes, 학습: dict) -> bytes:
    열 = 학습["요약"]["열"]
    칸수 = max(len(학습["요약"]["머리"]), max(열.values()) + 1)
    줄들 = []
    for r in 결과들:
        값 = [""] * 칸수
        값[열["후보자"]] = r.자료.이름
        값[열["전문성"]] = r.전문성
        값[열["역량"]] = r.역량
        값[열["결과"]] = "추천"
        줄들.append(값)
    return D.요약_만들기(틀, 줄들)
