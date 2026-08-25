"""말로 적으면 **블록 초안**을 만들어 준다. 블록 종류를 가리지 않는다.

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
LIST_SCHEMA: dict = {
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

AXIS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "제목": {"type": "string"},
        "행축": {"type": "string"},
        "열축": {"type": "string"},
        "행": {"type": "array", "items": {"type": "string"}},
        "열": {"type": "array", "items": {"type": "string"}},
        "칸수식": {"type": "string"},
        "형식": {"type": "string"},
    },
    "required": ["행축", "열축", "칸수식"],
}

NUM_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "제목": {"type": "string"},
        "수식": {"type": "string"},
        "형식": {"type": "string"},
    },
    "required": ["수식"],
}

PROFILE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "제목": {"type": "string"},
        "대상": {"type": "string"},
        "머리": {"type": "string"},
        "줄": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"라벨": {"type": "string"},
                               "문장": {"type": "string"}},
                "required": ["문장"],
            },
        },
    },
    "required": ["줄"],
}

FREE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "제목": {"type": "string"},
        "행": {"type": "array", "items": {"type": "string"}},
        "열": {"type": "array", "items": {"type": "string"}},
        "칸": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"행": {"type": "string"}, "열": {"type": "string"},
                               "값": {"type": "string"}},
                "required": ["행", "열", "값"],
            },
        },
        "형식": {"type": "string"},
    },
    "required": ["행", "열"],
}

TEXT_SCHEMA: dict = {
    "type": "object",
    "properties": {"제목": {"type": "string"}, "글": {"type": "string"}},
    "required": ["글"],
}

_SYSTEM = (
    "너는 채용 담당자가 말로 설명한 것을 대시보드 블록 '정의' 로 옮기는 도구다. "
    "값을 지어내지 말고, 정의만 낸다."
)

#: 어느 안내문에나 붙는 수식 설명. 한 군데서만 고치면 여섯 종류가 같이 따라온다.
_행수식 = """[행 문맥 수식 — 한 사람의 값. 엑셀과 같다]
- `=` 로 시작한다. 열 이름을 그대로 쓰고, `&` 로 잇는다.
- `=한글_이름`   `=박사_학교 & " " & 박사_전공`
- 날짜: `=TEXT(박사_졸업,"'yy.m")` → '26.8   (m 은 한 자리, mm 은 두 자리)
- 기간: `=TEXT(박사_시작,"'yy.m") & "~" & TEXT(박사_졸업,"'yy.m")`
- 갈라 쓰기: `=IF(석사_학교="","",석사_학교)`
- 빈 값 건너뛰고 잇기: `=TEXTJOIN(" / ", TRUE, 박사_학교, 석사_학교)`
- 줄바꿈: `CHAR(10)`
- 쓸 수 있는 함수: TEXT TEXTJOIN CONCAT LEFT RIGHT MID LEN TRIM SUBSTITUTE
  UPPER LOWER REPT CHAR CODE IF IFS AND OR NOT IFERROR ISBLANK VALUE ROUND
  INT ABS MIN MAX SUM YEAR MONTH DAY TODAY DATEDIF"""

_집계수식 = """[집계 문맥 수식 — 여러 사람을 센다]
- 모양은 하나뿐이다: `=함수(대상, 조건...)`
- 함수: COUNT PCT AVG SUM MIN MAX LIST
- 대상: `지원자`(인재 Pool 전체) 또는 `채용`(채용을 시작한 사람)
- 조건: `열="값"` `열~"패턴*"` `열!~"패턴*"` `열>숫자` `열!="값"` — AND 로만 잇는다
- 예: `=COUNT(채용, 부서="공정", 서류검토="합격")`
      `=PCT(채용, 최종상태~"*합격", 최종상태!~"*불합격")`
      `=AVG(지원자, 저널_주저자_수)`
- **주의**: `최종상태~"*합격"` 은 불합격도 맞는다. 합격만 세려면
  `최종상태!~"*불합격"` 을 같이 걸어라."""

_공통규칙 = """[규칙]
- **아래 '쓸 수 있는 열' 에 있는 이름만 써라.** 없는 이름을 지어내면 그건 버려진다.
- 설명에 없는 것을 굳이 넣지 마라.
- 제목은 짧은 한국어로."""


def _안내(종류: str) -> str:
    if 종류 == "목록":
        return f"""아래 설명을 읽고 **목록 표**의 정의를 만들어라.
한 사람이 한 줄이고, 열은 설명한 대로 만든다.

{_행수식}

{_공통규칙}
- 대상: 인재 Pool 전체면 "지원자", 채용을 시작한 사람만이면 "채용".
- 조건: 행을 고르는 수식. 참/거짓을 낸다. 전부면 빈 문자열.
- 정렬: 기준이 되는 수식 하나. 큰 값부터면 내림차순을 true 로.
- 머리글은 짧게 (이름, 학력, 경력, 논문)."""

    if 종류 == "축표":
        return f"""아래 설명을 읽고 **축 표(피벗)** 의 정의를 만들어라.
행 축과 열 축을 정하면 그 값들이 저절로 줄과 칸이 되고, **칸 수식 하나**를
모든 칸에 되풀이한다.

{_집계수식}

[축]
- 행축·열축은 다음 중에서 고른다: {{축목록}}
- 칸 수식에서 `{{{{행}}}}` `{{{{열}}}}` 이 그 축의 값으로 바뀐다.
  예: `=COUNT(채용, 부서="{{{{행}}}}", 최종상태="{{{{열}}}}")`
- 축을 `직접 입력` 으로 두면 행·열 목록을 직접 적는다 (그때만 행·열 배열을 채워라).

{_공통규칙}
- 형식: 그대로 / 정수 / 소수1 / 퍼센트 / 쉼표 / 명 중 하나."""

    if 종류 == "숫자":
        return f"""아래 설명을 읽고 **큰 숫자 하나**를 내는 정의를 만들어라.

{_집계수식}

{_공통규칙}
- 형식: 그대로 / 정수 / 소수1 / 퍼센트 / 쉼표 / 명 중 하나."""

    if 종류 == "프로필":
        return f"""아래 설명을 읽고 **인별 프로필 양식**의 정의를 만들어라.
조건에 맞는 사람마다 카드가 한 장씩 나온다.

{_행수식}

[대상]
- 누구를 보여줄지는 집계 문맥이다: `=LIST(채용, 부서="공정")` 처럼 적는다.
  전부면 `=LIST(지원자)`.

{_공통규칙}
- 머리: 카드 맨 위 한 줄. 예 `=한글_이름 & " (" & 현재_신분 & ")"`
- 줄: 라벨(학력·경력·실적 같은 짧은 말)과 문장 수식의 짝."""

    if 종류 == "표":
        return f"""아래 설명을 읽고 **자유 표**의 정의를 만들어라.
행 이름과 열 이름을 직접 정하고, **칸마다** 값을 따로 적는다.

{_집계수식}

{_공통규칙}
- 칸의 값은 집계 수식이거나 그냥 글자다.
- 설명에 없는 칸은 비워 둬도 된다 (칸 배열에 안 넣으면 빈칸)."""

    return f"""아래 설명을 읽고 대시보드에 넣을 **설명 글**을 지어라.
표가 아니라 사람이 읽는 글이다. 짧고 분명하게, 세 줄을 넘기지 마라.

{_공통규칙}"""


#: 블록 종류 -> (스키마, 이름)
_모양 = {
    "목록": (LIST_SCHEMA, "dash_list"),
    "축표": (AXIS_SCHEMA, "dash_axis"),
    "숫자": (NUM_SCHEMA, "dash_num"),
    "프로필": (PROFILE_SCHEMA, "dash_profile"),
    "표": (FREE_SCHEMA, "dash_free"),
    "글": (TEXT_SCHEMA, "dash_text"),
}

_형식들 = ("그대로", "정수", "소수1", "퍼센트", "쉼표", "명")


def draft(설명: str, 아는열, llm: LLMClient | None = None, *,
          종류: str = "목록", 축목록=None) -> tuple[dict, list[str]]:
    """말 → (설정 초안, 메모). 메모는 사람에게 보여줄 알림이다.

    실패해도 예외를 던지지 않는다. 초안 만들기가 안 됐다고 블록을 못 만들면
    안 된다 — **손으로 만드는 길이 언제나 살아 있어야 한다.**
    """
    설명 = (설명 or "").strip()
    if not 설명:
        return {}, ["무엇을 만들지 적어 주세요."]
    if 종류 not in _모양:
        return {}, [f"'{종류}' 블록은 아직 말로 못 만듭니다."]

    스키마, 이름 = _모양[종류]
    안내 = _안내(종류)
    if "{축목록}" in 안내:
        안내 = 안내.replace("{축목록}", ", ".join(축목록 or []))
    쓰는것 = llm or LLMClient()
    가진것 = llm is None
    try:
        답 = 쓰는것.chat_json(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": (
                    f"{안내}\n\n[쓸 수 있는 열]\n{', '.join(sorted(아는열))}"
                    f"\n\n[설명]\n{설명}")},
            ],
            스키마,
            schema_name=이름,
        )
    except LLMError as exc:
        return {}, [f"초안을 만들지 못했습니다: {exc}"]
    finally:
        if 가진것:
            쓰는것.close()

    메모: list[str] = []
    설정 = _옮기기(종류, 답, 아는열, 메모, 축목록 or [])
    제목 = str(답.get("제목") or "").strip()
    if 제목:
        설정["_제목"] = 제목               # 부르는 쪽이 블록 제목으로 쓴다
    return 설정, 메모


def _옮기기(종류: str, 답: dict, 아는열, 메모: list[str], 축목록) -> dict:
    """LLM 이 낸 것을 블록 설정으로. **검사에서 떨어진 것은 버린다.**"""
    from . import expr
    from . import formula as F

    def 행수식(식: str, 자리: str) -> str:
        """행 문맥 수식. 모르는 열을 쓴 것은 버린다 — ? 만 뜨는 초안은 못 쓴다."""
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

    def 집계수식(식: str, 자리: str, 견본: str = "") -> str:
        """집계 문맥 수식. {행}{열} 이 든 것은 견본 값을 넣고 검사한다."""
        식 = (식 or "").strip()
        if not 식:
            return ""
        볼것 = 식.replace("{행}", 견본 or "가").replace("{열}", 견본 or "가")
        try:
            F.validate(볼것, set(아는열))
        except F.FormulaError as exc:
            메모.append(f"{자리} 는 빼놨습니다 — {exc}")
            return ""
        return 식

    def 형식(v) -> str:
        v = str(v or "").strip()
        return v if v in _형식들 else "그대로"

    if 종류 == "목록":
        설정 = {
            "목록대상": 답.get("대상") if 답.get("대상") in ("지원자", "채용") else "지원자",
            "목록내림차순": bool(답.get("내림차순")),
            "목록조건": 행수식(답.get("조건", ""), "행 고르기"),
            "목록정렬": 행수식(답.get("정렬", ""), "정렬"),
        }
        열 = []
        for i, c in enumerate(답.get("열") or []):
            if not isinstance(c, dict):
                continue
            머리 = str(c.get("머리글") or "").strip()
            식 = 행수식(str(c.get("수식") or ""), f"'{머리 or i + 1}' 열")
            if 식:
                열.append([머리, 식, ""])
        설정["목록열"] = 열
        _센다(메모, len(열), "열")
        return 설정

    if 종류 == "축표":
        고를수있는것 = list(축목록) + ["직접 입력"]
        행축 = str(답.get("행축") or "").strip()
        열축 = str(답.get("열축") or "").strip()
        설정 = {
            "행축": 행축 if 행축 in 고를수있는것 else "직접 입력",
            "열축": 열축 if 열축 in 고를수있는것 else "직접 입력",
            "행": [str(x).strip() for x in (답.get("행") or []) if str(x).strip()],
            "열": [str(x).strip() for x in (답.get("열") or []) if str(x).strip()],
            "형식": 형식(답.get("형식")),
        }
        설정["칸수식"] = 집계수식(답.get("칸수식", ""), "칸 수식")
        if not 설정["칸수식"]:
            메모.append("칸 수식이 안 나왔습니다. 손으로 적어 주세요.")
        else:
            메모.insert(0, "축과 칸 수식을 만들었습니다. **보고 고친 뒤 저장하세요.**")
        return 설정

    if 종류 == "숫자":
        설정 = {"수식": 집계수식(답.get("수식", ""), "수식"),
              "형식": 형식(답.get("형식"))}
        if not 설정["수식"]:
            메모.append("수식이 안 나왔습니다. 손으로 적어 주세요.")
        else:
            메모.insert(0, "수식을 만들었습니다. **보고 고친 뒤 저장하세요.**")
        return 설정

    if 종류 == "프로필":
        대상 = 집계수식(답.get("대상", ""), "누구를") or "=LIST(지원자, 열=지원자_ID)"
        줄 = []
        for i, r in enumerate(답.get("줄") or []):
            if not isinstance(r, dict):
                continue
            라벨 = str(r.get("라벨") or "").strip()
            문장 = 행수식(str(r.get("문장") or ""), f"'{라벨 or i + 1}' 줄")
            if 문장:
                줄.append([라벨, 문장])
        설정 = {"대상": 대상, "머리": 행수식(답.get("머리", ""), "머리"), "줄": 줄}
        _센다(메모, len(줄), "줄")
        return 설정

    if 종류 == "표":
        행 = [str(x).strip() for x in (답.get("행") or []) if str(x).strip()]
        열 = [str(x).strip() for x in (답.get("열") or []) if str(x).strip()]
        칸 = {}
        for c in (답.get("칸") or []):
            if not isinstance(c, dict):
                continue
            r, k, v = (str(c.get("행") or "").strip(), str(c.get("열") or "").strip(),
                       str(c.get("값") or "").strip())
            if r not in 행 or k not in 열 or not v:
                continue
            # 수식이면 검사하고, 그냥 글자면 그대로 둔다
            칸[f"{r}\t{k}"] = 집계수식(v, f"'{r}×{k}' 칸") if v.startswith("=") else v
        설정 = {"행": 행, "열": 열, "칸": {k: v for k, v in 칸.items() if v},
              "형식": 형식(답.get("형식"))}
        _센다(메모, len(행) * len(열), "칸")
        return 설정

    return {"글": str(답.get("글") or "").strip()}


def _센다(메모: list[str], 개수: int, 단위: str) -> None:
    if 개수:
        메모.insert(0, f"{단위} {개수}개를 만들었습니다. **보고 고친 뒤 저장하세요.**")
    else:
        메모.append(f"쓸 수 있는 {단위}이 하나도 안 나왔습니다. 손으로 만들어 주세요.")
