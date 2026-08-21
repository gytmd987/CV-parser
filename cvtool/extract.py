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

import json
import uuid
from pathlib import Path

from .clients.llm import LLMClient, LLMError
from . import normalize as N
from .config import settings
from .ingestion.parsers import extract_text
from .schemas import (
    학위상태_ENUM,
    현재_신분_ENUM,
    SECTION_BASIC,
    SECTION_CAREER,
    SECTION_EDUCATION,
    SECTION_RESEARCH,
    Career,
    CVRecord,
    Paper,
    Patent,
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
- 이메일·전화번호가 여러 개면 전부 적어라. 구분은 쉼표로 한다(형식은 나중에 통일된다).
- 이름: 이력서에 한글명만 있으면 영문명을 로마자로 추정하고, 영문명만 있으면
  한글명을 추정해라. 추정한 쪽은 출처를 "추정"으로 표시해라.
  이력서에 실제로 적혀 있으면 "원문", 판단 불가면 "없음".
- 현재_신분은 지금 이 사람의 상태다. 이력서 내용으로 확실히 판단되지 않으면
  반드시 "불명"을 선택해라. 추측해서 아무거나 고르지 마라."""

_EDU_HINT = """앞의 정리 내용과 이력서 원문을 바탕으로 학력을 채워라.

- 시작/졸업은 YYYYMM 6자리. 연도만 알면 YYYY00. 전혀 없으면 빈 문자열.
- 박사_학위상태: 졸업/수료/재학/예정 중 하나. 아무 정보 없으면 빈 문자열.
  졸업일이 이미 지난 시점이면 '졸업' 이다. 이력서에 '재학' 이라 적혀 있어도
  졸업 연월이 과거면 졸업으로 판단해라.
- 전공은 학문 이름만 적어라. '전공', '과정' 같은 꼬리표는 빼라.
  예: '전기공학전공' -> '전기공학'. 여러 전공이면 각각 적어라.
- 석박사 통합과정인 경우:
  * 석박통합_여부를 true 로 설정
  * 통합과정 정보는 전부 박사_* 에 넣어라 (박사_시작 = 통합과정 입학년월)
  * 석사_* 는 전부 빈 문자열. 통합과정은 석사 학위를 따로 받지 않는다.
  * 단 통합과정 중 석사만 취득하고 나온 경우에는 석사_* 를 채워라."""

_RESEARCH_HINT = """앞의 정리 내용과 이력서 원문에서 논문과 특허를 채워라.

[논문] — **공저자 논문까지 전부** 넣어라. 빠뜨리지 마라.
- 지원자 이름: {이름}
- 저자구분: 저자 목록에서 이 이름이 **맨 앞(제1저자·공동 제1저자)이거나
  교신저자(corresponding)** 이면 "주저자", 그 밖이면 "공저자".
  저자 순서를 알 수 없으면 "공저자" 로 둬라. 주저자로 올리지 마라.
- 제출처는 학회명 또는 저널명 그대로. 약어가 있으면 약어 그대로.
- 유형: 저널이면 "저널", 학회/워크숍이면 "학회". 애매하면 "기타".
- 국내해외: 그 학회/저널 자체가 국내 것인지 국제(해외) 것인지 판단해라.
  조금이라도 확실하지 않으면 "불명"으로 둬라. 추측하지 마라.
  (분류는 담당자가 따로 검토하므로, 모르면 불명이 정답이다.)
- 연도는 4자리.
- 같은 논문이 여러 번 적혀 있으면 한 번만 넣어라.

[특허]
- 상태: 등록(registered/granted)이면 "등록", 출원(filed/pending)이면 "출원".
  이력서에 그런 말이 없으면 "불명". 추측하지 마라.
- 특허가 없으면 빈 배열."""

_CAREER_HINT = """앞의 정리 내용과 이력서 원문에서 **일한 경력**을 채워라.

- 넣을 것: 기업 재직 경력, **박사후연구원(포닥/Post-doc)**, 연구소·대학의
  연구원·전임연구원처럼 학위 취득 후 급여를 받고 일한 자리.
  지금 포닥 중이면 그 포닥을 반드시 넣어라 — 이 사람의 현재 직장이다.
- 뺄 것: 학위 과정(석사·박사 재학), 조교(TA/RA), 학부 연구생.
- 인턴은 **넣되 인턴여부를 true 로** 표시해라 (걸러내는 건 우리가 한다).
- 회사: 기업이면 회사명, 포닥·연구원이면 **소속 대학·연구소 이름**.
- 직무: 직책 그대로 (예: "선임연구원", "박사후연구원", "Staff Engineer").
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


def json_directive(schema: dict) -> str:
    """출력 형식을 프롬프트로도 지시한다.

    guided_json 에만 의존하면 안 된다. 서버가 그 필드를 400 없이 조용히
    무시하면 모델은 JSON 을 만들 이유가 없어 산문으로 답한다. 실제로
    모든 섹션이 "응답에 JSON 객체가 없습니다"로 실패했다.
    """
    return (
        "\n\n[출력 형식 — 반드시 지켜라]\n"
        "아래 JSON 스키마에 맞는 **JSON 객체 하나만** 출력해라.\n"
        "- 설명·인사말·요약을 앞뒤에 붙이지 마라.\n"
        "- 코드펜스(```)로 감싸지 마라.\n"
        '- 모르는 값은 빈 문자열 "" 로 둬라. 항목을 생략하지 마라.\n'
        "- 첫 글자는 { 이고 마지막 글자는 } 여야 한다.\n\n"
        "[스키마]\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
    )


def _ask(
    llm: LLMClient, hint: str, schema: dict, cv_text: str, digest: str, name: str
) -> dict:
    본문 = f"--- 이력서 원문 ---\n{cv_text}"
    if digest:
        본문 = f"--- 이력서에서 정리한 내용 ---\n{digest}\n\n{본문}"
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"{hint}\n\n{본문}{json_directive(schema)}"},
    ]
    return llm.chat_json(
        messages, schema, temperature=settings.llm_temperature, schema_name=name
    )


def _read_pass(llm: LLMClient, cv_text: str) -> str:
    """1단계: 스키마 없이 읽고 정리하게 둔다."""
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _READ_PROMPT.format(cv_text=cv_text)},
    ]
    return llm.chat_text(messages, temperature=settings.llm_temperature)


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


def _재직중(종료: str) -> bool:
    """'재직중', '현재', 'present' 처럼 아직 다니고 있다는 표시인가."""
    표시 = (종료 or "").strip().lower()
    return 표시 in ("재직중", "재직 중", "현재", "present", "current", "now", "-")


def _대표_경력(경력들: list[Career]) -> tuple[Career, list[str]]:
    """열로 뽑을 **가장 최근 경력 하나**를 고른다.

    거르는 기준은 둘이다.
      - **인턴은 뺀다.** 채용 판단에서 인턴을 경력으로 보지 않는다.
      - **6개월 미만은 뺀다.** 짧게 스친 자리가 대표 경력으로 올라오면
        이 사람이 뭘 하는 사람인지 표에서 잘못 읽힌다.

    기간을 알 수 없는 경력은 **버리지 않는다.** 날짜를 못 읽은 것과 실제로
    짧은 것은 다르다. 남기되 검토 사유에 적는다.
    """
    오늘 = now_kst().strftime("%Y%m")
    메모: list[str] = []
    쓸것: list[tuple[str, Career]] = []
    for c in 경력들:
        직무 = (c.직무 or "").lower()
        if c.인턴여부 or "인턴" in 직무 or "intern" in 직무:
            continue
        개월 = N.months_between(c.시작, c.종료, 오늘)
        # 6개월 규칙은 **끝난 자리**에만 건다. 지금 다니는 자리는 짧아도
        # 그 사람의 현재 직장이다 — 이제 막 시작한 포닥이 여기서 걸리면
        # "현재 포닥 중" 이라는 사실 자체가 표에서 사라진다.
        if not _재직중(c.종료) and 개월 is not None and 개월 < N.MIN_CAREER_MONTHS:
            continue
        if 개월 is None and (c.시작 or c.종료):
            메모.append(f"경력 기간을 못 읽음: {c.회사} {c.시작!r}~{c.종료!r}")
        # 재직 중이면 가장 최근이다. 아니면 종료 → 시작 순으로 비교한다.
        키 = "999999" if _재직중(c.종료) else (N.yyyymm(c.종료) or N.yyyymm(c.시작) or "")
        쓸것.append((키, c))

    if not 쓸것:
        return Career(), 메모
    쓸것.sort(key=lambda x: x[0], reverse=True)
    return 쓸것[0][1], 메모


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

    # 새 키는 "논문"(전부), 옛 키는 "1저자_논문". 둘 다 받아준다.
    논문원본 = research.get("논문") or research.get("1저자_논문") or []
    논문 = [Paper.model_validate(p) for p in 논문원본]
    불명 = [p.제출처 for p in 논문 if p.국내해외 == "불명" and p.주저자]
    if 불명:
        사유.append("국내/해외 판별 불가: " + ", ".join(불명))
    특허 = [Patent.model_validate(pt) for pt in research.get("특허") or []]
    상태불명 = sum(1 for pt in 특허 if pt.상태 not in ("등록", "출원"))
    if 상태불명:
        사유.append(f"특허 {상태불명}건의 등록/출원 여부를 판단하지 못함")

    경력_목록 = career.get("경력", []) or []
    경력들 = [Career.model_validate(c) for c in 경력_목록]
    경력_요약 = " | ".join(
        f"{c.회사}/{c.직무}({c.시작}-{c.종료})" for c in 경력들
    )
    대표경력, 경력메모 = _대표_경력(경력들)
    사유.extend(경력메모)

    키워드 = research.get("연구분야_키워드", []) or []

    # 석박통합은 학력에서 뽑은 값이 먼저다. 신분이 '석박통합' 이면 그것도 근거다.
    석박통합 = "석박통합" if (edu.get("석박통합_여부")
                          or basic.get("현재_신분") == "석박통합") else ""
    if 석박통합 and edu.get("석사_학교"):
        사유.append("석박통합인데 석사 학력이 있음 (중도 석사 취득 여부 확인)")

    # LLM 이 형식을 어긴 값을 약속한 형태로 맞춘다. 고쳐야 했던 항목은 기록해
    # 사람이 확인할 수 있게 한다 (모델이 형식을 자주 어기면 프롬프트 문제다).
    def date6(key: str, raw: str) -> str:
        fixed = N.yyyymm(raw)
        if raw and not fixed:
            형식오류.append(f"{key}={raw!r}")
        elif raw and fixed != str(raw).strip():
            형식오류.append(f"{key}: {raw!r}->{fixed}")
        return fixed

    형식오류: list[str] = []
    생년월일 = N.yyyymmdd(basic.get("생년월일", ""))
    if basic.get("생년월일") and not 생년월일:
        형식오류.append(f"생년월일={basic.get('생년월일')!r}")

    신분 = N.enum(basic.get("현재_신분", ""), 현재_신분_ENUM, "불명")
    학위상태 = N.enum(edu.get("박사_학위상태", ""), 학위상태_ENUM, "")

    # 날짜는 CVRecord 를 만들기 전에 전부 계산한다.
    # 생성자 인자 안에서 계산하면 아래 형식오류 검사가 먼저 돌아 놓친다.
    날짜 = {
        key: date6(key, edu.get(key, ""))
        for key in (
            "박사_시작", "박사_졸업", "석사_시작", "석사_졸업", "학사_시작", "학사_졸업",
        )
    }

    # 졸업일이 이미 지났는데 '재학' 으로 나오는 모순을 바로잡는다
    학위상태, 학위보정 = N.degree_status(
        학위상태, 날짜["박사_졸업"], now_kst().strftime("%Y%m")
    )
    if 학위보정:
        사유.append(학위보정)

    if 형식오류:
        사유.append("형식 보정: " + ", ".join(형식오류))
    if 신분 == "불명" and basic.get("현재_신분") not in ("불명", None, ""):
        사유.append(f"현재_신분 값이 목록 밖: {basic.get('현재_신분')!r}")

    return CVRecord(
        지원자_ID=지원자_ID,
        한글_이름=N.text(basic.get("한글_이름", "")),
        영문_이름=N.text(basic.get("영문_이름", "")),
        이름_추정여부="/".join(추정),
        생년월일=생년월일,
        전화번호=N.phones(basic.get("전화번호", "")),
        이메일=N.emails(basic.get("이메일", "")),
        현재_신분=신분,
        현재_소속=N.text(basic.get("현재_소속", "")),
        현재_소속_상세=N.text(basic.get("현재_소속_상세", "")),
        현재_지도교수=N.text(basic.get("현재_지도교수", "")),
        박사_석박통합=석박통합,
        박사_학교=N.text(edu.get("박사_학교", "")),
        박사_전공=N.major(edu.get("박사_전공", "")),
        박사_지도교수=N.text(edu.get("박사_지도교수", "")),
        박사_시작=날짜["박사_시작"],
        박사_졸업=날짜["박사_졸업"],
        박사_학위상태=학위상태,
        석사_학교=N.text(edu.get("석사_학교", "")),
        석사_전공=N.major(edu.get("석사_전공", "")),
        석사_지도교수=N.text(edu.get("석사_지도교수", "")),
        석사_시작=날짜["석사_시작"],
        석사_졸업=날짜["석사_졸업"],
        학사_학교=N.text(edu.get("학사_학교", "")),
        학사_전공=N.major(edu.get("학사_전공", "")),
        학사_시작=날짜["학사_시작"],
        학사_졸업=날짜["학사_졸업"],
        논문=논문,
        특허=특허,
        연구분야_키워드=N.multi(키워드),
        경력_요약=N.text(경력_요약),
        경력_회사=N.text(대표경력.회사),
        직책=N.text(대표경력.직무),
        경력_시작=N.yyyymm(대표경력.시작),
        경력_종료=("재직중" if _재직중(대표경력.종료)
                else N.yyyymm(대표경력.종료)),
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
