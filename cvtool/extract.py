"""CV 구조화 추출 — 섹션 분할 방식.

한 번에 전부 뽑으면 출력이 길어져 잘리고 정확도도 떨어진다. 4개 섹션으로 나눠
각각 guided_json 으로 강제한 뒤 하나의 CVRecord 로 합친다.

섹션 하나가 실패해도 나머지는 살린다. 실패한 섹션은 검토_필요=Y 로 표시된다.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from .clients.llm import LLMClient, LLMError
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
    "너는 채용 담당자를 돕는 이력서 정보 추출기다. "
    "이력서에 적힌 사실만 추출하고, 없는 정보는 빈 문자열로 두어라. "
    "추측하라고 명시한 항목만 추측해도 된다."
)

_BASIC_HINT = """다음 이력서에서 기본 인적사항을 추출해라.

규칙:
- 생년월일은 yyyymmdd 8자리 숫자로. 없으면 빈 문자열.
- 이름: 이력서에 한글명만 있으면 영문명을 로마자로 추정하고, 영문명만 있으면
  한글명을 추정해라. 단 추정한 쪽은 반드시 출처를 "추정"으로 표시해라.
  이력서에 실제로 적혀 있으면 "원문", 아예 판단 불가면 "없음".
- 현재_신분: 포닥/박사/석박통합/석사/학사/타사재직/기타 중 하나.
  학위 과정 재학 중이면 그 과정을, 학위 취득 후 연구원이면 포닥,
  기업 재직 중이면 타사재직.
- 석박사 통합과정이면 현재_신분을 "석박통합"으로.
"""

_EDU_HINT = """다음 이력서에서 학력 정보를 추출해라.

규칙:
- 시작/졸업은 YYYYMM 6자리 숫자로. 예: 201903. 연도만 알면 YYYY00.
  전혀 없으면 빈 문자열.
- 박사_학위상태: 졸업/수료/재학/예정 중 하나.
  졸업일이 확정돼 있으면 "졸업", 아직 재학 중이면 "재학",
  졸업 예정만 적혀 있으면 "예정", 아무 정보 없으면 빈 문자열.
- 석박사 통합과정(석박통합)인 경우:
  * 석박통합_여부를 true 로 설정
  * 통합과정 정보는 전부 박사_* 항목에 넣어라 (박사_시작 = 통합과정 입학년월)
  * 석사_* 항목은 전부 빈 문자열로 둬라. 통합과정은 석사 학위를 따로 받지 않는다.
  * 단 통합과정 중 석사만 취득하고 나온 경우에는 석사_* 를 채워라.
"""

_RESEARCH_HINT = """다음 이력서에서 1저자(제1저자, first author) 논문만 추출해라.

규칙:
- 공저자 논문은 제외한다. 1저자/제1저자/공동1저자만.
- 제출처는 학회명 또는 저널명. 약어가 있으면 약어 그대로 (예: NeurIPS, ICML).
- 국내해외: 국내 학회/저널이면 "국내", 국제(해외)면 "해외", 판단 불가면 "불명".
  한국정보과학회, 대한전자공학회, KCC, KSC 등은 국내.
  NeurIPS, ICML, CVPR, IEEE, ACM, Nature, Science 등은 해외.
- 연도는 4자리.
"""

_CAREER_HINT = """다음 이력서에서 기업 재직 경력만 추출해라.

규칙:
- 학위 과정, 인턴, 조교는 제외한다. 정규 재직 경력만.
- 시작/종료는 YYYYMM 6자리. 재직 중이면 종료를 "재직중"으로.
"""


def _ask(llm: LLMClient, hint: str, schema: dict, cv_text: str, name: str) -> dict:
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"{hint}\n\n--- 이력서 ---\n{cv_text}"},
    ]
    return llm.chat_json(messages, schema, temperature=0.0, schema_name=name)


def extract_cv_from_text(
    cv_text: str,
    *,
    client: LLMClient | None = None,
    지원자_ID: str | None = None,
    원본_파일명: str = "",
) -> CVRecord:
    """이력서 텍스트 -> CVRecord. 섹션별로 나눠 호출한다."""
    if not cv_text or not cv_text.strip():
        raise ValueError("빈 이력서 텍스트입니다.")

    llm = client or LLMClient()
    owns = client is None
    사유: list[str] = []
    data: dict = {}

    sections = [
        ("기본정보", _BASIC_HINT, SECTION_BASIC, "basic"),
        ("학력", _EDU_HINT, SECTION_EDUCATION, "education"),
        ("연구", _RESEARCH_HINT, SECTION_RESEARCH, "research"),
        ("경력", _CAREER_HINT, SECTION_CAREER, "career"),
    ]

    try:
        for label, hint, schema, name in sections:
            try:
                data[name] = _ask(llm, hint, schema, cv_text, name)
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

    # 이름 추정 여부 -> 사람이 읽는 한 문자열로
    추정: list[str] = []
    if basic.get("한글_이름_출처") == "추정":
        추정.append("한글추정")
    if basic.get("영문_이름_출처") == "추정":
        추정.append("영문추정")
    if 추정:
        사유.append("이름 " + "/".join(추정) + " (원문 대조 필요)")

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

    # 석박통합: 석사 항목이 채워져 있으면 통합과정과 모순이므로 표시
    if edu.get("석박통합_여부") and edu.get("석사_학교"):
        사유.append("석박통합인데 석사 학력이 있음 (중도 석사 취득 여부 확인)")

    rec = CVRecord(
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
    return rec


def extract_cv_from_file(
    path: str | Path, *, client: LLMClient | None = None, 지원자_ID: str | None = None
) -> CVRecord:
    """CV 파일(PDF/docx/txt) -> CVRecord."""
    p = Path(path)
    return extract_cv_from_text(
        extract_text(p), client=client, 지원자_ID=지원자_ID, 원본_파일명=p.name
    )
