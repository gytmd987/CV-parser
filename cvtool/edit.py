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
    석박통합_ENUM,
    학위상태_ENUM,
    현재_신분_ENUM,
)

#: 수정할 수 없는 항목 (시스템이 관리한다)
READONLY_FIELDS = {"지원자_ID", "추출_일시", "원본_파일명", *COUNT_COLUMNS}

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


def field_spec(항목: str) -> FieldSpec:
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
    return FieldSpec(항목, "text", [])


def validate(항목: str, 값: str) -> str:
    """입력값을 검사하고 저장할 형태로 정규화한다.

    형식이 어긋나면 ValidationError 를 낸다. 조용히 고쳐서 넣지 않는다 —
    사람이 잘못 입력한 것을 모르고 지나가면 안 되기 때문이다.
    """
    if 항목 in READONLY_FIELDS:
        raise ValidationError(f"'{항목}' 은 수정할 수 없습니다.")
    if 항목 in REGISTRY_FIELDS:
        raise ValidationError(
            f"'{항목}' 은 여기서 고치지 않습니다. 표기가 잘못됐으면 "
            f"'명칭 관리' 에서 대표명을 고치세요 (표에 바로 반영됩니다)."
        )

    원본 = (값 or "").strip()

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

    return N.text(원본)


def validate_registry(항목: str, 값: str, registry) -> str:
    """소속·전공처럼 명칭 사전이 관리하는 항목의 값을 검사한다.

    **자유 입력을 받지 않는다.** 사전에 이미 있는 이름 중에서만 고를 수 있다.
    자유 입력을 허용하면 표에 보이는 대표명을 그대로 저장해 버리는 일이 생기고,
    그러면 원문 표기가 사라져 나중에 사전을 고쳐도 되돌릴 수 없다.
    """
    종류 = NAME_COLUMNS.get(항목)
    if 종류 is None:
        raise ValidationError(f"명칭 사전이 관리하는 항목이 아닙니다: {항목}")
    원본 = (값 or "").strip()
    if not 원본:
        return ""
    found = registry.lookup(종류, 원본)
    if found is None:
        raise ValidationError(
            f"'{원본}' 은 명칭 사전에 없습니다. '명칭 관리' 화면의 {종류} 목록에서 고르세요."
        )
    return found.표시명


def apply_edit(rec, 항목: str, 새값: str, 기대_이전값: str | None = None,
               registry=None) -> tuple[str, str]:
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
    if 기대_이전값 is not None and 현재값 != str(기대_이전값 or ""):
        raise ConflictError(항목, 현재값, str(기대_이전값 or ""))

    if 항목 in REGISTRY_FIELDS:
        if registry is None:
            raise ValidationError(
                f"'{항목}' 은 표에서 직접 고칠 수 없습니다. 지원자 상세 화면에서 "
                f"명칭 사전에 있는 이름 중 골라 주세요."
            )
        저장값 = validate_registry(항목, 새값, registry)
    else:
        저장값 = validate(항목, 새값)
    setattr(rec, 항목, 저장값)
    return 현재값, 저장값


# ---------------------------------------------------------------------------
# 사용자 정의 열
# ---------------------------------------------------------------------------
def validate_custom(field: dict, 값: str) -> str:
    """관리자가 웹에서 만든 열의 값을 검사한다.

    기본 열과 같은 원칙이다 — 형식이 어긋나면 저장을 거부한다.
    """
    이름 = field.get("이름", "열")
    유형 = field.get("유형", "텍스트")
    원본 = (값 or "").strip()

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

    return N.text(원본)


def custom_field_spec(field: dict) -> FieldSpec:
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
    return FieldSpec(이름, "text", [])
