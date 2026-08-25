"""말로 적으면 목록 표의 **초안**을 만들어 준다.

    "채용 중인 사람들, 이름이랑 학력이랑 논문 수. 주저자 논문 많은 순으로"
      →  대상 채용 / 정렬 =저널_주저자_수 (큰 값부터)
         이름   =한글_이름
         학력   =박사_학교 & " " & 박사_전공
         논문   =저널_주저자_수 & "/" & 저널_수

빈 화면에 `=한글_이름` 부터 쳐 넣는 건 아무리 문법이 쉬워도 부담스럽다.
초안이 있으면 **고치는 일**이 되고, 고치는 건 훨씬 쉽다.

## 지키는 것 두 가지

1. **LLM 이 값을 만들지 않는다.** 만드는 건 *열 정의*뿐이고, 실제 표는 언제나
   우리 계산기(`expr`)가 그린다. 그래야 같은 표가 늘 같은 값을 내고, 사람이
   눈으로 확인할 것이 한 번만 생긴다.
2. **나온 걸 그대로 쓰지 않는다.** 뽑은 수식을 여기서 다시 검사해서(`expr.validate`)
   모르는 열을 쓴 것은 **버린다.** 화면에 `?` 만 뜨는 초안은 없느니만 못하다.
"""

from __future__ import annotations

from .clients.llm import LLMClient, LLMError

#: 뽑아낼 모양. guided_json 이 이 틀을 강제한다.
SCHEMA: dict = {
    "type": "object",
    "properties": {
        "제목": {"type": "string"},
        "대상": {"type": "string", "enum": ["지원자", "채용"]},
        "조건": {"type": "string"},
        "정렬": {"type": "string"},
        "내림차순": {"type": "boolean"},
        "열": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "머리글": {"type": "string"},
                    "수식": {"type": "string"},
                },
                "required": ["머리글", "수식"],
            },
        },
    },
    "required": ["대상", "열"],
}

_SYSTEM = (
    "너는 채용 담당자가 말로 설명한 표를 '열 정의'로 옮기는 도구다. "
    "값을 지어내지 말고, 정의만 낸다."
)

_PROMPT = """아래 설명을 읽고 **목록 표**의 정의를 만들어라.
한 사람이 한 줄이고, 열은 설명한 대로 만든다.

[수식 문법 — 엑셀과 같다]
- `=` 로 시작한다. 열 이름을 그대로 쓰고, `&` 로 잇는다.
- `=한글_이름`
- `=박사_학교 & " " & 박사_전공`
- 날짜: `=TEXT(박사_졸업,"'yy.m")` → '26.8   (m 은 한 자리, mm 은 두 자리)
- 기간: `=TEXT(박사_시작,"'yy.m") & "~" & TEXT(박사_졸업,"'yy.m")`
- 갈라 쓰기: `=IF(석사_학교="","",석사_학교)`
- 빈 값 건너뛰고 잇기: `=TEXTJOIN(" / ", TRUE, 박사_학교, 석사_학교)`
- 쓸 수 있는 함수: TEXT TEXTJOIN CONCAT LEFT RIGHT MID LEN TRIM SUBSTITUTE
  UPPER LOWER REPT IF IFS AND OR NOT IFERROR ISBLANK VALUE ROUND INT ABS
  MIN MAX SUM YEAR MONTH DAY TODAY DATEDIF

[규칙]
- **아래 '쓸 수 있는 열' 에 있는 이름만 써라.** 없는 이름을 지어내면 그 열은 버려진다.
- 대상: 인재 Pool 전체면 "지원자", 채용을 시작한 사람만이면 "채용".
- 조건: 행을 고르는 수식. 참/거짓을 낸다. 예 `=최종상태="최종 합격"`.
  전부 보여줄 것이면 빈 문자열.
- 정렬: 기준이 되는 수식 하나. 없으면 빈 문자열. 큰 값부터면 내림차순을 true 로.
- 머리글은 짧은 한국어로 (예: 이름, 학력, 경력, 논문).
- 설명에 없는 열을 굳이 넣지 마라.

[쓸 수 있는 열]
{열목록}

[설명]
{설명}"""


def draft(설명: str, 아는열, llm: LLMClient | None = None) -> tuple[dict, list[str]]:
    """말 → (설정 초안, 메모). 메모는 사람에게 보여줄 알림이다.

    실패해도 예외를 던지지 않는다. 초안 만들기가 안 됐다고 표를 못 만들면
    안 된다 — 손으로 만드는 길이 언제나 살아 있어야 한다.
    """
    from . import expr

    설명 = (설명 or "").strip()
    if not 설명:
        return {}, ["무엇을 만들지 적어 주세요."]

    열목록 = ", ".join(sorted(아는열))
    쓰는것 = llm or LLMClient()
    가진것 = llm is None
    try:
        답 = 쓰는것.chat_json(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user",
                 "content": _PROMPT.format(열목록=열목록, 설명=설명)},
            ],
            SCHEMA,
            schema_name="dash_list",
        )
    except LLMError as exc:
        return {}, [f"초안을 만들지 못했습니다: {exc}"]
    finally:
        if 가진것:
            쓰는것.close()

    메모: list[str] = []
    설정: dict = {
        "목록대상": 답.get("대상") if 답.get("대상") in ("지원자", "채용") else "지원자",
        "목록내림차순": bool(답.get("내림차순")),
    }

    def 쓸만한가(식: str, 자리: str) -> str:
        """모르는 열을 쓴 수식은 **버린다.** ? 만 뜨는 초안은 없느니만 못하다."""
        식 = (식 or "").strip()
        if not 식:
            return ""
        if not expr.is_formula(식):
            식 = "=" + 식
        try:
            expr.validate(식, 아는열)
        except expr.ExprError as exc:
            메모.append(f"{자리} 는 빼놨습니다 — {exc}")
            return ""
        return 식

    설정["목록조건"] = 쓸만한가(답.get("조건", ""), "행 고르기")
    설정["목록정렬"] = 쓸만한가(답.get("정렬", ""), "정렬")

    열 = []
    for i, c in enumerate(답.get("열") or []):
        if not isinstance(c, dict):
            continue
        머리 = str(c.get("머리글") or "").strip()
        식 = 쓸만한가(str(c.get("수식") or ""), f"'{머리 or i + 1}' 열")
        if 식:
            열.append([머리, 식])
    설정["목록열"] = 열
    if not 열:
        메모.append("쓸 수 있는 열이 하나도 안 나왔습니다. 손으로 만들어 주세요.")
    else:
        메모.insert(0, f"열 {len(열)}개를 만들었습니다. **보고 고친 뒤 저장하세요.**")
    return 설정, 메모
