"""지원자 정보 수동 수정.

세 가지를 함께 지킨다.

1. **형식 강제** — 정해진 형식에 맞지 않으면 저장을 거부한다. 드롭다운이 있는
   항목은 목록 밖의 값을 받지 않는다. 잘못된 값이 표에 들어가면 정렬·필터가
   전부 망가지기 때문이다.

2. **필드 단위 저장** — 행 전체를 덮어쓰지 않고 바꾼 칸만 고친다.
   두 사람이 서로 다른 칸을 고치면 충돌 자체가 생기지 않는다.

3. **낙관적 잠금** — 같은 칸을 동시에 고치면, 나중 사람에게 "다른 사람이 방금
   이 값을 바꿨다" 고 알린다. 조용히 덮어쓰지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import normalize as N
from .schemas import (
    COUNT_COLUMNS,
    NAME_COLUMNS,
    게재상태_ENUM,
    석박통합_ENUM,
    저자구분_ENUM,
    학위상태_ENUM,
    현재_신분_ENUM,
    Paper,
)

#: 논문 한 줄에서 목록 안의 값만 받는 칸들 (칸 이름 -> 고를 수 있는 값)
PAPER_CHOICES: dict[str, list[str]] = {
    "유형": ["학회", "저널", "기타"],
    "국내해외": ["국내", "해외", "불명"],
    "저자구분": list(저자구분_ENUM),
    "게재상태": list(게재상태_ENUM),
}

#: 수정할 수 없는 항목 (시스템이 관리한다)
#: 검토_사유는 상세 화면의 **검토 카드**가 관리한다. 그 카드는 사유 글자를
#: 열쇠 삼아 '확인함' 을 기록하므로, 글을 고치면 확인 기록과 짝이 어긋나
#: 되돌릴 수 없다. 값을 고치는 건 사유가 가리키는 **그 열**에서 한다.
READONLY_FIELDS = {"지원자_ID", "추출_일시", "원본_파일명", "검토_사유",
                   *COUNT_COLUMNS}

#: 명칭 사전이 대표명을 붙이는 항목. 여기서 직접 고치면 사전과 어긋난다.
#: (표에 보이는 값은 사전을 거친 대표명이라, 그 값을 그대로 저장하면
#:  원문 표기가 사라지고 나중에 사전을 고쳐도 되돌릴 수 없다.)
REGISTRY_FIELDS = set(NAME_COLUMNS)

#: 드롭다운으로만 고를 수 있는 항목
CHOICE_FIELDS: dict[str, list[str]] = {
    "현재_신분": 현재_신분_ENUM,
    "박사_학위상태": 학위상태_ENUM,
    "박사_석박통합": 석박통합_ENUM,
    "검토_필요": ["", "Y"],
}

#: 형식이 정해진 항목
_YYYYMM_FIELDS = {
    "박사_시작", "박사_졸업", "석사_시작", "석사_졸업", "학사_시작", "학사_졸업",
    "경력_시작",
}
#: 경력_종료는 '재직중' 도 들어갈 수 있어 YYYYMM 강제를 걸지 않는다
_YYYYMMDD_FIELDS = {"생년월일"}
_EMAIL_FIELDS = {"이메일"}
_PHONE_FIELDS = {"전화번호"}
_MAJOR_FIELDS = {"박사_전공", "석사_전공", "학사_전공"}
#: 여러 값을 넣을 수 있는 항목 (구분자 통일 대상)
_MULTI_FIELDS = {"연구분야_키워드"}


class ValidationError(ValueError):
    """형식이 맞지 않아 저장을 거부한다."""


class ConflictError(RuntimeError):
    """다른 사람이 먼저 바꿨다. 덮어쓰지 않고 알린다."""

    def __init__(self, 항목: str, 현재값: str, 기대값: str) -> None:
        super().__init__(
            f"'{항목}' 을 다른 사람이 방금 바꿨습니다. "
            f"화면에 있던 값은 '{기대값 or '(빈칸)'}' 인데 지금은 '{현재값 or '(빈칸)'}' 입니다."
        )
        self.항목, self.현재값, self.기대값 = 항목, 현재값, 기대값


@dataclass
class FieldSpec:
    이름: str
    입력: str          # text | select | yyyymm | yyyymmdd | email | phone
    선택지: list[str]
    도움말: str = ""


#: 여러 줄을 켤 수 있는 입력 종류. 형식이 정해진 칸(날짜·연월·전화·이메일·
#: 선택·숫자)은 켤 수 없다 — 줄바꿈이 들어가면 그 형식이 무너진다.
MULTILINE_OK = "text"

#: 형식은 자유인데 **무엇을 적는 자리인지** 헷갈리는 열의 안내.
#: 비고는 이름이 같은 짝(`채용_비고`)이 채용 현황에 따로 있어서 특히 그렇다.
_안내 = {
    "비고": "이 사람에 대한 메모 (채용 이야기는 채용 현황의 채용_비고 에)",
}


def field_spec(항목: str, 긴글: bool = False) -> FieldSpec:
    """화면에서 어떤 입력칸을 그릴지 정한다."""
    if 항목 in CHOICE_FIELDS:
        return FieldSpec(항목, "select", CHOICE_FIELDS[항목], "목록에서 고르세요")
    if 항목 in _YYYYMMDD_FIELDS:
        return FieldSpec(항목, "yyyymmdd", [], "YYYYMMDD 8자리 (예: 19920315)")
    if 항목 in _YYYYMM_FIELDS:
        return FieldSpec(항목, "yyyymm", [], "YYYYMM 6자리 (예: 201903)")
    if 항목 in _EMAIL_FIELDS:
        return FieldSpec(항목, "email", [], "여러 개면 쉼표로 구분")
    if 항목 in _PHONE_FIELDS:
        return FieldSpec(항목, "phone", [], "010-1234-5678")
    도움 = _안내.get(항목, "여러 줄을 쓸 수 있습니다" if 긴글 else "")
    return FieldSpec(항목, "긴글" if 긴글 else "text", [], 도움)


def validate(항목: str, 값: str, 긴글: bool = False) -> str:
    """입력값을 검사하고 저장할 형태로 정규화한다.

    형식이 어긋나면 ValidationError 를 낸다. 조용히 고쳐서 넣지 않는다 —
    사람이 잘못 입력한 것을 모르고 지나가면 안 되기 때문이다.

    `긴글` 이면 줄바꿈을 살린다. 어느 열이 그런지는 관리자가 «표 항목» 에서
    정하고, 부르는 쪽이 그 값을 넘긴다. 형식이 정해진 열은 그 검사가 먼저
    걸리므로 여기까지 오지 않는다.
    """
    if 항목 in READONLY_FIELDS:
        raise ValidationError(f"'{항목}' 은 수정할 수 없습니다.")
    if 항목 in REGISTRY_FIELDS:
        # 여기까지 오면 안 된다 — `apply_edit` 이 사전 갈래에서 먼저 처리한다.
        # 사전 없이 부른 자리를 막는 그물이다.
        raise ValidationError(
            f"'{항목}' 은 명칭 사전이 함께 있어야 고칠 수 있습니다."
        )

    원본 = N.paragraph(값) if 긴글 else (값 or "").strip()

    if 항목 in CHOICE_FIELDS:
        허용 = CHOICE_FIELDS[항목]
        if 원본 not in 허용:
            보기 = ", ".join(v or "(빈칸)" for v in 허용)
            raise ValidationError(f"'{항목}' 은 다음 중 하나여야 합니다: {보기}")
        return 원본

    if not 원본:
        return ""

    if 항목 in _YYYYMMDD_FIELDS:
        결과 = N.yyyymmdd(원본)
        if not 결과:
            raise ValidationError(
                f"'{항목}' 은 YYYYMMDD 8자리여야 합니다. 입력값: {원본!r}"
            )
        return 결과

    if 항목 in _YYYYMM_FIELDS:
        결과 = N.yyyymm(원본)
        if not 결과:
            raise ValidationError(f"'{항목}' 은 YYYYMM 6자리여야 합니다. 입력값: {원본!r}")
        return 결과

    if 항목 in _EMAIL_FIELDS:
        결과 = N.emails(원본)
        for part in 결과.split(N.MULTI_SEP):
            if "@" not in part or part.startswith("@") or part.endswith("@"):
                raise ValidationError(f"이메일 형식이 아닙니다: {part!r}")
        return 결과

    if 항목 in _PHONE_FIELDS:
        return N.phones(원본)

    if 항목 in _MAJOR_FIELDS:
        return N.major(원본)

    if 항목 in _MULTI_FIELDS:
        return N.multi(원본)

    return N.paragraph(원본) if 긴글 else N.text(원본)


def registry_entry(항목: str, 값: str, registry):
    """그 값이 가리키는 사전 항목. 없으면 None.

    화면의 소속·학교 칸은 **표시명**으로 고르게 돼 있고(사람이 정한 이름),
    레코드에는 **원표기**가 들어 있다(CV 에 적힌 그대로). 둘 다 같은 항목을
    가리킬 수 있어야 해서 두 갈래로 찾는다.
    """
    종류 = NAME_COLUMNS.get(항목)
    if 종류 is None or not (값 or "").strip():
        return None
    표기 = 값.strip()
    return registry.lookup(종류, 표기) or registry.by_display(종류, 표기)


def registry_display(항목: str, 값: str, registry) -> str:
    """그 값이 화면에 어떻게 보이나. 사전에 없으면 값 그대로."""
    found = registry_entry(항목, 값, registry)
    return found.표시명 if found else (값 or "").strip()


#: 레코드에 든 날값과 **화면에 뜨는 값이 다른** 열.
#:
#: 저장은 이력서에 적힌 그대로 하고, 보여줄 때마다 다시 계산한다 — 명칭 사전
#: 열과 같은 구조다. 다른 점은 다시 읽는 것이 사전이 아니라는 것뿐이다.
#:   박사_학위상태 — **오늘 날짜**. 졸업일이 지났으면 졸업이다.
#:   경력_요약    — **사전**. 경력 목록의 회사 이름을 사전 이름으로 다시 만든다.
CALCULATED_FIELDS = ("박사_학위상태", "경력_요약")


def 보이는값(rec, 항목: str, registry=None) -> str:
    """이 칸이 화면에 뜨는 값. 레코드가 든 날값과 다를 수 있다.

    화면을 그리는 쪽과 고친 값을 받는 쪽이 **같은 함수**를 봐야 한다. 갈라지면
    손도 안 댄 칸이 매번 "다른 사람이 방금 바꿨습니다" 가 된다.
    """
    현재값 = str(getattr(rec, 항목, "") or "")
    if 항목 in getattr(rec, "직접입력", {}):
        # 사람이 정한 값이다. 사전이 어떻게 바뀌든 안 움직인다.
        return rec.직접입력[항목]
    if 항목 in REGISTRY_FIELDS and registry is not None:
        return registry_display(항목, 현재값, registry)
    if 항목 == "박사_학위상태":
        return rec.학위상태_보기()
    if 항목 == "경력_요약":
        return rec.경력_요약_보기(registry)
    return 현재값


def validate_registry(항목: str, 값: str, registry, 현재값: str = "") -> str:
    """사전에 있는 이름을 고른 것을 **원표기**로 바꾼다.

    사전에 없는 값은 받지 않는다(ValidationError). 사람이 손으로 적은 값은
    여기로 오지 않는다 — `_사전열_고치기` 가 먼저 갈라서 `직접입력` 으로
    보낸다. 여기는 «사전에서 골랐다» 는 뜻이 확실한 값만 온다.

    «있다» 의 뜻이 둘이다 — CV 에 적힌 **원표기**로 있을 수도 있고,
    사람이 정한 **표시명**으로 있을 수도 있다. 화면은 표시명으로 고르게 하므로
    표시명도 받아야 한다. (표시명을 안 받던 동안에는 `서울대학교` 의 이름을
    `서울대` 로 바꿔 두면 그 목록에서 고른 값이 "명칭 사전에 없습니다" 로
    되돌아왔다.)

    **돌려주는 것은 표시명이 아니라 원표기다.** 표에 보이는 이름은 볼 때마다
    사전에서 다시 읽으므로(`CVRecord.to_row`), 저장은 원표기로 둬야 나중에
    사전에서 이름을 고쳤을 때 따라온다. 표시명을 저장해 버리면 원문 표기가
    사라져 되돌릴 수 없다.
    """
    종류 = NAME_COLUMNS.get(항목)
    if 종류 is None:
        raise ValidationError(f"명칭 사전이 관리하는 항목이 아닙니다: {항목}")
    원본 = (값 or "").strip()
    if not 원본:
        return ""
    found = registry_entry(항목, 원본, registry)
    if found is None:
        raise ValidationError(
            f"'{원본}' 은 명칭 사전에 없습니다. '명칭 관리' 화면의 {종류} 목록에서 고르세요."
        )
    # 지금 값이 이미 같은 항목을 가리키면 **아무것도 바꾸지 않는다.**
    # 화면이 표시명을 되돌려 보냈다는 이유로 원표기를 갈아치우면 안 된다.
    지금 = registry_entry(항목, 현재값, registry)
    if 지금 is not None and 지금.표시명 == found.표시명:
        return (현재값 or "").strip()
    return found.원표기


def _사전에서_고른것(항목: str, 값: str, registry):
    """적은 글자가 **사전에 있는 이름 그대로**인가. 아니면 None.

    `registry.lookup` 은 정규화키로도 찾아서 `서울대학교(본교)` 가
    `서울대학교` 로 걸린다. 그 느슨함은 CV 표기를 묶을 때는 맞지만 여기서는
    아니다 — 고정하려고 적은 값이 조용히 풀려 버린다. **똑같을 때만** 본다.
    """
    found = registry_entry(항목, 값, registry)
    if found is None:
        return None
    글 = (값 or "").strip()
    return found if (found.원표기 == 글 or found.표시명 == 글) else None


def _사전열_고치기(rec, 항목: str, 새값: str, registry, 현재값: str) -> str:
    """소속·학교·전공 칸에 적은 값을 받는다. 돌려주는 것은 **원표기**다.

    사전에 있는 이름을 그대로 적었으면 «그것을 고른» 것이므로 사전을 따라가고,
    아니면 **이 지원자만의 값**으로 고정한다. 고정해도 원표기는 안 덮는다 —
    되돌리기가 그 칸을 지우는 것만으로 끝나야 하기 때문이다.
    """
    적은값 = (새값 or "").strip()
    if not 적은값:
        # 비운 것은 «빈칸» 이지 «이 사람은 예외» 가 아니다.
        rec.직접입력.pop(항목, None)
        return ""
    if _사전에서_고른것(항목, 적은값, registry) is not None:
        rec.직접입력.pop(항목, None)
        return validate_registry(항목, 적은값, registry, 현재값=현재값)
    rec.직접입력[항목] = 적은값
    return 현재값


def edit_field(rec, 항목: str, 새값: str, *, 기대_이전값: str | None = None,
               registry=None, 긴글: bool = False) -> tuple[str, str]:
    """한 칸 고치기. 돌려주는 것은 **화면에 뜨던 값과 뜨게 될 값**이다.

    `apply_edit` 은 «저장된 날값» 을 돌려주는데, 그것만으로는 바뀌었는지 알 수
    없는 경우가 있다 — 사전 열을 고정하거나 풀면 원표기는 그대로이고 `직접입력`
    만 바뀐다. 화면 기준으로 견주면 그 경우까지 잡힌다. 변경 이력에 남길 값도
    사람이 화면에서 본 것이어야 읽힌다.
    """
    전 = 보이는값(rec, 항목, registry)
    apply_edit(rec, 항목, 새값, 기대_이전값=기대_이전값,
               registry=registry, 긴글=긴글)
    return 전, 보이는값(rec, 항목, registry)


def 사전_따라가기(rec, 항목: str, registry=None) -> tuple[str, str]:
    """손으로 정해 둔 값을 버리고 다시 사전을 따라가게 한다. (전, 후)"""
    전 = 보이는값(rec, 항목, registry)
    rec.직접입력.pop(항목, None)
    return 전, 보이는값(rec, 항목, registry)


def apply_edit(rec, 항목: str, 새값: str, 기대_이전값: str | None = None,
               registry=None, 긴글: bool = False) -> tuple[str, str]:
    """레코드의 한 항목만 고친다.

    Args:
        기대_이전값: 화면에 보이던 값. 지금 값과 다르면 다른 사람이 먼저
            고친 것이므로 ConflictError 를 낸다. None 이면 검사하지 않는다.
        registry: 명칭 사전. 소속·전공 항목은 이것을 넘겨야만 고칠 수 있고,
            사전에 있는 이름 중에서만 고를 수 있다. 안 넘기면 거부된다.
    Returns:
        (이전값, 저장된 값)
    """
    if not hasattr(rec, 항목):
        raise ValidationError(f"없는 항목입니다: {항목}")

    현재값 = str(getattr(rec, 항목) or "")
    # 화면에 **보이던 값끼리** 견준다. 날값끼리 견주면 계산 열과 사전 열은
    # 손도 안 댄 칸이 매번 "다른 사람이 방금 바꿨습니다" 가 된다.
    비교값 = 보이는값(rec, 항목, registry)
    # 줄 끝은 맞춰 놓고 견준다 — 브라우저가 폼을 보낼 때 줄바꿈을 CRLF 로
    # 바꿔 놓아서, 안 그러면 여러 줄 칸이 저장할 때마다 충돌로 잡힌다.
    if 기대_이전값 is not None and N.lines(비교값) != N.lines(기대_이전값):
        raise ConflictError(항목, 비교값, str(기대_이전값 or ""))

    if 항목 in REGISTRY_FIELDS:
        if registry is None:
            # 부르는 쪽이 사전을 안 넘긴 자리. 사전에 있는 이름인지 가릴 수가
            # 없어 고정인지 아닌지도 정할 수 없다.
            raise ValidationError(
                f"'{항목}' 은 명칭 사전이 함께 있어야 고칠 수 있습니다."
            )
        저장값 = _사전열_고치기(rec, 항목, 새값, registry, 현재값)
    elif 항목 in CALCULATED_FIELDS and N.lines(새값) == N.lines(비교값):
        # 화면이 **보이던 값을 그대로 되돌려 보냈다.** 안 고친 것이므로 날값을
        # 건드리지 않는다. 계산 결과를 저장해 버리면 거기서 얼어붙는다 —
        # 졸업으로 보이던 것을 저장하면 졸업일을 고쳐도 안 따라오고, 사전
        # 이름으로 만든 요약을 저장하면 «사람이 고친 요약» 으로 오해받는다.
        저장값 = 현재값
    else:
        저장값 = validate(항목, 새값, 긴글=긴글)
    setattr(rec, 항목, 저장값)
    return 현재값, 저장값


def validate_paper(값들: dict) -> Paper | None:
    """논문 한 줄을 검사해 `Paper` 로. 제출처가 비었으면 None.

    제출처가 빈 줄을 버리는 까닭: 화면 맨 아래에 **추가용 빈 줄**이 늘 하나
    있다. 그것을 그대로 저장하면 저장할 때마다 빈 논문이 하나씩 쌓인다.
    제출처는 논문을 가리키는 최소 정보라(명칭 사전도 이걸로 찾는다) 이 칸을
    기준으로 삼는다.
    """
    제출처 = (값들.get("제출처") or "").strip()
    if not 제출처:
        return None

    연도 = (값들.get("연도") or "").strip()
    if 연도 and not (len(연도) == 4 and 연도.isdigit()):
        raise ValidationError(f"논문 연도는 4자리여야 합니다: {연도!r}")

    고른것 = {}
    for 칸, 고를수있는것 in PAPER_CHOICES.items():
        값 = (값들.get(칸) or "").strip()
        if 값 and 값 not in 고를수있는것:
            raise ValidationError(
                f"논문 '{칸}' 은 다음 중 하나여야 합니다: {', '.join(고를수있는것)}")
        # 빈 값은 넘기지 않는다 — Paper 의 기본값이 서야 한다
        # (게재상태 기본이 «게재» 인 것이 여기 걸린다).
        if 값:
            고른것[칸] = 값

    return Paper(제목=(값들.get("제목") or "").strip(), 제출처=제출처,
                 연도=연도, **고른것)


# ---------------------------------------------------------------------------
# 사용자 정의 열
# ---------------------------------------------------------------------------
def validate_custom(field: dict, 값: str, 긴글: bool = False) -> str:
    """관리자가 웹에서 만든 열의 값을 검사한다.

    기본 열과 같은 원칙이다 — 형식이 어긋나면 저장을 거부한다.
    """
    이름 = field.get("이름", "열")
    유형 = field.get("유형", "텍스트")
    원본 = N.paragraph(값) if 긴글 else (값 or "").strip()

    if 유형 == "선택":
        허용 = [o.strip() for o in (field.get("선택지") or "").split("|") if o.strip()]
        if 원본 and 원본 not in 허용:
            raise ValidationError(
                f"'{이름}' 은 다음 중 하나여야 합니다: {', '.join(허용)}"
            )
        return 원본

    if not 원본:
        return ""

    if 유형 == "연월":
        결과 = N.yyyymm(원본)
        if not 결과:
            raise ValidationError(f"'{이름}' 은 YYYYMM 6자리여야 합니다. 입력값: {원본!r}")
        return 결과

    if 유형 == "숫자":
        정리 = 원본.replace(",", "")
        try:
            float(정리)
        except ValueError:
            raise ValidationError(f"'{이름}' 은 숫자여야 합니다. 입력값: {원본!r}") from None
        return 정리

    return N.paragraph(원본) if 긴글 else N.text(원본)


def custom_field_spec(field: dict, 긴글: bool = False) -> FieldSpec:
    """사용자 정의 열의 입력칸 모양."""
    유형 = field.get("유형", "텍스트")
    이름 = field.get("이름", "")
    if 유형 == "선택":
        선택지 = [""] + [
            o.strip() for o in (field.get("선택지") or "").split("|") if o.strip()
        ]
        return FieldSpec(이름, "select", 선택지, "목록에서 고르세요")
    if 유형 == "연월":
        return FieldSpec(이름, "yyyymm", [], "YYYYMM 6자리 (예: 202603)")
    if 유형 == "숫자":
        return FieldSpec(이름, "number", [], "숫자만")
    if 긴글:
        return FieldSpec(이름, "긴글", [], "여러 줄을 쓸 수 있습니다")
    return FieldSpec(이름, "text", [])
