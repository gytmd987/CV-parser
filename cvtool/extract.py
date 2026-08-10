"""CV 구조화 추출 — 2단계 × 섹션 분할.

CV 는 정해진 양식이 없다. 그래서 본문을 규칙으로 파싱하지 않는다.
이 파일 어디에도 이름·날짜·학교를 정규식으로 뽑는 코드는 없다.

1단계 (읽기): 스키마 없이 자유롭게 읽고 정리하게 둔다.
    guided_json 은 첫 토큰부터 JSON 문법을 강제해서 추론 모델이 생각할 자리를
    없앤다. 읽는 동안에는 문법을 풀어주고, 정리 노트를 받는다.
2단계 (구조화): 정리 노트 + 원문을 주고 섹션별로 guided_json 을 강제한다.

섹션을 4개로 나누면 출력이 짧아져 잘리지 않고, 모델이 한 번에 한 가지에 집중한다.
한 섹션이 실패해도 나머지는 살리고 검토_필요 로 표시한다.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from .clients.llm import LLMClient, LLMError
from .config import settings
from .ingestion.parsers import extract_text
from .schemas import (
    SECTION_BASIC,
    SECTION_CAREER,
    SECTION_EDUCATION,
    SECTION_RESEARCH,
    CVRecord,
    Paper,
)
from .timeutil import now_kst

_SYSTEM = (
    "너는 채용 담당자를 돕는 이력서 분석기다. "
    "이력서는 정해진 양식이 없으므로 형식을 가정하지 말고 내용을 읽고 판단해라. "
    "이력서에 적힌 사실만 사용하고, 없는 정보는 비워 둬라. 지어내지 마라."
)

# --- 1단계: 자유 서술 읽기 ---------------------------------------------------
_READ_PROMPT = """아래 이력서를 읽고, 채용 담당자가 쓸 수 있게 사실을 정리해라.

정해진 양식이 없는 문서다. 표가 깨져 있거나 순서가 뒤섞여 있을 수 있으니
형식을 가정하지 말고 내용으로 판단해라.

다음을 각각 정리해라. 확실하지 않은 것은 "불확실"이라고 명시해라.
1. 인적사항 — 이름(한글/영문 각각 이력서에 실제로 적혀 있는지), 생년월일, 연락처, 이메일
2. 현재 상태 — 지금 무엇을 하는 사람인지(학위과정 재학/박사후연구원/기업 재직 등),
   소속 기관과 부서/연구실, 지도교수
3. 학력 — 박사/석사/학사 각각의 학교, 전공, 지도교수, 시작·졸업 시점, 학위 상태.
   석박사 통합과정이면 그 사실을 명확히 적어라.
4. 논문 — 저자 목록에서 이 지원자가 **몇 번째 저자**인지 주의해서 보고,
   제1저자(공동 제1저자 포함)인 것만 골라 제출처(학회명/저널명)와 연도를 적어라.
   본인 이름이 굵게 표시돼 있었더라도 텍스트에서는 사라졌을 수 있으니,
   위에서 파악한 지원자 이름과 저자 목록을 대조해라.
5. 경력 — 기업 재직 이력(학위과정·인턴·조교는 제외)

원문에 근거가 없는 내용은 쓰지 마라. 표 형식 말고 줄글로 정리해라.

--- 이력서 ---
{cv_text}"""

# --- 2단계: 섹션별 구조화 ----------------------------------------------------
_BASIC_HINT = """앞의 정리 내용과 이력서 원문을 바탕으로 기본 인적사항을 채워라.

- 생년월일은 yyyymmdd 8자리. 없으면 빈 문자열.
- 이름: 이력서에 한글명만 있으면 영문명을 로마자로 추정하고, 영문명만 있으면
  한글명을 추정해라. 추정한 쪽은 출처를 "추정"으로 표시해라.
  이력서에 실제로 적혀 있으면 "원문", 판단 불가면 "없음".
- 현재_신분은 지금 이 사람의 상태다. 이력서 내용으로 확실히 판단되지 않으면
  반드시 "불명"을 선택해라. 추측해서 아무거나 고르지 마라."""

_EDU_HINT = """앞의 정리 내용과 이력서 원문을 바탕으로 학력을 채워라.

- 시작/졸업은 YYYYMM 6자리. 연도만 알면 YYYY00. 전혀 없으면 빈 문자열.
- 박사_학위상태: 졸업/수료/재학/예정 중 하나. 아무 정보 없으면 빈 문자열.
- 석박사 통합과정인 경우:
  * 석박통합_여부를 true 로 설정
  * 통합과정 정보는 전부 박사_* 에 넣어라 (박사_시작 = 통합과정 입학년월)
  * 석사_* 는 전부 빈 문자열. 통합과정은 석사 학위를 따로 받지 않는다.
  * 단 통합과정 중 석사만 취득하고 나온 경우에는 석사_* 를 채워라."""

_RESEARCH_HINT = """앞의 정리 내용과 이력서 원문에서 제1저자 논문만 채워라.

- 공저자 논문은 제외한다. 제1저자·공동 제1저자만.
- 지원자 이름: {이름}
  저자 목록에서 이 이름이 맨 앞(또는 공동 1저자)인 것만 골라라.
  저자 순서를 알 수 없으면 그 논문은 넣지 마라.
- 제출처는 학회명 또는 저널명 그대로. 약어가 있으면 약어 그대로.
- 국내해외: 그 학회/저널 자체가 국내 것인지 국제(해외) 것인지 판단해라.
  조금이라도 확실하지 않으면 "불명"으로 둬라. 추측하지 마라.
  (분류는 담당자가 따로 검토하므로, 모르면 불명이 정답이다.)
- 연도는 4자리."""

_CAREER_HINT = """앞의 정리 내용과 이력서 원문에서 기업 재직 경력만 채워라.

- 학위 과정, 인턴, 조교는 제외한다. 정규 재직 경력만.
- 시작/종료는 YYYYMM 6자리. 재직 중이면 종료를 "재직중"으로."""


def guard_length(cv_text: str, limit: int | None = None) -> tuple[str, str]:
    """CV 본문이 모델 컨텍스트를 넘지 않게 자른다.

    조용히 자르면 뒤쪽(보통 논문 목록)이 통째로 사라져도 아무도 모른다.
    그래서 잘린 사실을 경고 문자열로 돌려주고 검토_필요 로 표시한다.
    """
    cap = limit if limit is not None else settings.max_input_chars
    if cap <= 0 or len(cv_text) <= cap:
        return cv_text, ""
    잘림 = len(cv_text) - cap
    경고 = (
        f"CV 본문이 길어 {잘림:,}자를 잘랐습니다(전체 {len(cv_text):,}자 중 {cap:,}자만 사용). "
        f"뒤쪽 내용이 누락됐을 수 있습니다. CVTOOL_MAX_INPUT_CHARS 를 늘리세요."
    )
    return cv_text[:cap], 경고


def _ask(
    llm: LLMClient, hint: str, schema: dict, cv_text: str, digest: str, name: str
) -> dict:
    본문 = f"--- 이력서 원문 ---\n{cv_text}"
    if digest:
        본문 = f"--- 이력서에서 정리한 내용 ---\n{digest}\n\n{본문}"
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"{hint}\n\n{본문}"},
    ]
    return llm.chat_json(messages, schema, temperature=0.0, schema_name=name)


def _read_pass(llm: LLMClient, cv_text: str) -> str:
    """1단계: 스키마 없이 읽고 정리하게 둔다."""
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _READ_PROMPT.format(cv_text=cv_text)},
    ]
    return llm.chat_text(messages, temperature=0.0)


def extract_cv_from_text(
    cv_text: str,
    *,
    client: LLMClient | None = None,
    지원자_ID: str | None = None,
    원본_파일명: str = "",
    two_stage: bool | None = None,
) -> CVRecord:
    """이력서 텍스트 -> CVRecord."""
    if not cv_text or not cv_text.strip():
        raise ValueError("빈 이력서 텍스트입니다.")

    사유: list[str] = []
    text, 길이경고 = guard_length(cv_text)
    if 길이경고:
        사유.append(길이경고)

    llm = client or LLMClient()
    owns = client is None
    use_two_stage = settings.two_stage if two_stage is None else two_stage
    data: dict = {}

    try:
        digest = ""
        if use_two_stage:
            try:
                digest = _read_pass(llm, text)
            except LLMError as exc:
                사유.append(f"1단계 읽기 실패(원문만으로 진행): {exc}")

        # 기본정보를 먼저 뽑아야 논문 단계에서 지원자 이름을 대조할 수 있다
        order = [
            ("기본정보", _BASIC_HINT, SECTION_BASIC, "basic"),
            ("학력", _EDU_HINT, SECTION_EDUCATION, "education"),
            ("연구", _RESEARCH_HINT, SECTION_RESEARCH, "research"),
            ("경력", _CAREER_HINT, SECTION_CAREER, "career"),
        ]
        for label, hint, schema, name in order:
            if name == "research":
                basic = data.get("basic", {})
                이름 = " / ".join(
                    v for v in (basic.get("한글_이름"), basic.get("영문_이름")) if v
                ) or "(파악 실패)"
                hint = hint.format(이름=이름)
            try:
                data[name] = _ask(llm, hint, schema, text, digest, name)
            except LLMError as exc:
                data[name] = {}
                사유.append(f"{label} 추출 실패: {exc}")
    finally:
        if owns:
            llm.close()

    return _assemble(
        data,
        사유,
        지원자_ID=지원자_ID or f"CV-{uuid.uuid4().hex[:8].upper()}",
        원본_파일명=원본_파일명,
    )


def _assemble(
    data: dict, 사유: list[str], *, 지원자_ID: str, 원본_파일명: str
) -> CVRecord:
    basic = data.get("basic", {})
    edu = data.get("education", {})
    research = data.get("research", {})
    career = data.get("career", {})

    추정: list[str] = []
    if basic.get("한글_이름_출처") == "추정":
        추정.append("한글추정")
    if basic.get("영문_이름_출처") == "추정":
        추정.append("영문추정")
    if 추정:
        사유.append("이름 " + "/".join(추정) + " (원문 대조 필요)")

    if basic.get("현재_신분") == "불명":
        사유.append("현재_신분을 판단하지 못함")

    논문 = [Paper.model_validate(p) for p in research.get("1저자_논문", []) or []]
    불명 = [p.제출처 for p in 논문 if p.국내해외 == "불명"]
    if 불명:
        사유.append("국내/해외 판별 불가: " + ", ".join(불명))

    경력_목록 = career.get("경력", []) or []
    경력_요약 = " | ".join(
        f"{c.get('회사','')}/{c.get('직무','')}({c.get('시작','')}-{c.get('종료','')})"
        for c in 경력_목록
    )

    키워드 = research.get("연구분야_키워드", []) or []

    if edu.get("석박통합_여부") and edu.get("석사_학교"):
        사유.append("석박통합인데 석사 학력이 있음 (중도 석사 취득 여부 확인)")

    return CVRecord(
        지원자_ID=지원자_ID,
        한글_이름=basic.get("한글_이름", ""),
        영문_이름=basic.get("영문_이름", ""),
        이름_추정여부="/".join(추정),
        생년월일=basic.get("생년월일", ""),
        전화번호=basic.get("전화번호", ""),
        이메일=basic.get("이메일", ""),
        현재_신분=basic.get("현재_신분", ""),
        현재_소속=basic.get("현재_소속", ""),
        현재_소속_상세=basic.get("현재_소속_상세", ""),
        현재_지도교수=basic.get("현재_지도교수", ""),
        박사_학교=edu.get("박사_학교", ""),
        박사_전공=edu.get("박사_전공", ""),
        박사_지도교수=edu.get("박사_지도교수", ""),
        박사_시작=edu.get("박사_시작", ""),
        박사_졸업=edu.get("박사_졸업", ""),
        박사_학위상태=edu.get("박사_학위상태", ""),
        석사_학교=edu.get("석사_학교", ""),
        석사_전공=edu.get("석사_전공", ""),
        석사_지도교수=edu.get("석사_지도교수", ""),
        석사_시작=edu.get("석사_시작", ""),
        석사_졸업=edu.get("석사_졸업", ""),
        학사_학교=edu.get("학사_학교", ""),
        학사_전공=edu.get("학사_전공", ""),
        학사_시작=edu.get("학사_시작", ""),
        학사_졸업=edu.get("학사_졸업", ""),
        논문=논문,
        연구분야_키워드=", ".join(키워드) if isinstance(키워드, list) else str(키워드),
        경력_요약=경력_요약,
        검토_필요="Y" if 사유 else "",
        검토_사유=" / ".join(사유),
        원본_파일명=원본_파일명,
        추출_일시=now_kst().strftime("%Y-%m-%d %H:%M:%S"),
    )


def extract_cv_from_file(
    path: str | Path,
    *,
    client: LLMClient | None = None,
    지원자_ID: str | None = None,
    two_stage: bool | None = None,
) -> CVRecord:
    """CV 파일(PDF/docx/txt) -> CVRecord."""
    p = Path(path)
    return extract_cv_from_text(
        extract_text(p),
        client=client,
        지원자_ID=지원자_ID,
        원본_파일명=p.name,
        two_stage=two_stage,
    )
