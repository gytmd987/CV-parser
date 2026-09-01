"""엑셀 양식으로 지원자 여러 명 한 번에 등록.

CV 없이 들어온 지원자를 한 명씩 빈 줄로 만들어 상세에서 채우면, 사람 수만큼
같은 짓을 반복해야 한다. 여기서는 **지금 표 열 그대로 만든 빈 양식**을 내주고,
채워서 올린 것을 읽는다.

화면(app.py)에서 떼어 놓은 이유는 시험할 수 있게 하기 위해서다. 이 파일은
DB 를 건드리지 않는다 — 열을 정하고, 엑셀을 만들고, 올라온 것을 줄 목록으로
푸는 데까지만 한다. 실제 등록은 부르는 쪽이 한다.
"""

from __future__ import annotations

from .export import build_xlsx
from .edit import READONLY_FIELDS
from .schemas import COLUMNS, CVRecord
from .xlsx_read import XlsxError, read_sheet

#: 양식에서 뺄 열.
#:
#: - `검토_필요` : 엑셀로 들어온 사람은 **무조건** 검토 필요다 (사람이 손으로
#:   적은 값이라 확인을 거쳐야 한다). 사람이 고를 값이 아니다.
#: - `1저자_해외논문_제출처` · `임팩트_팩터` : 레코드에 없는 열이다. 논문 목록에서
#:   계산해 보여 주는 값이라 여기에 적어도 갈 곳이 없다.
_뺄열 = {"검토_필요", "1저자_해외논문_제출처", "임팩트_팩터"}

#: CV 없이 등록한 사람에게 붙는 검토 사유. `store.create_blank` 과 짝을 맞춘다.
등록사유 = "엑셀로 직접 등록 (내용 확인 필요)"

#: 양식 첫 줄에 넣는 안내. 셀에 넣지 않고 파일 이름으로만 알린다.
파일이름 = "지원자_등록_양식.xlsx"


def 양식열(store) -> list[str]:
    """양식에 낼 열.

    표에 보이는 열 중 **사람이 실제로 채울 수 있는 것**만이다. 계산해서 나오는
    열(논문 수·IF)과 시스템이 관리하는 열(검토 사유·원본 파일명)은 적어 봐야
    갈 곳이 없어서 넣지 않는다. 남길 것인지는 `CVRecord` 에 그 이름의 항목이
    있느냐로 가른다 — `edit.apply_edit` 이 쓰는 것과 같은 기준이다.
    """
    있는것 = set(CVRecord.model_fields)
    열 = [c for c in COLUMNS
         if c in 있는것 and c not in READONLY_FIELDS and c not in _뺄열]
    열.append("등록년도")
    열.extend(store.field_names("지원자 정보"))
    return 열


def 양식(열: list[str], 라벨: dict[str, str] | None = None) -> bytes:
    """머리글만 있는 빈 엑셀."""
    보일이름 = [(라벨 or {}).get(c, c) for c in 열]
    return build_xlsx([], 보일이름)


def 머리풀기(머리글: list[str], 열: list[str],
          라벨: dict[str, str] | None = None) -> tuple[dict[int, str], list[str]]:
    """엑셀 첫 줄을 내부 열 이름으로 옮긴다.

    **보이는 이름과 내부 이름 둘 다 받는다.** 양식은 표 항목 탭에서 정한 이름으로
    나가는데, 사람이 그 머리글을 지우고 내부 이름을 적어 올릴 수도 있다. 둘 중
    무엇으로 적어도 통해야 한다.

    Returns:
        ({칸 번호: 열 이름}, 못 알아본 머리글들)
    """
    라벨 = 라벨 or {}
    거꾸로: dict[str, str] = {}
    for c in 열:
        거꾸로[c.strip().lower()] = c
        거꾸로[라벨.get(c, c).strip().lower()] = c

    자리: dict[int, str] = {}
    모르는것: list[str] = []
    for n, 글 in enumerate(머리글):
        키 = (글 or "").strip()
        if not 키:
            continue
        찾음 = 거꾸로.get(키.lower())
        if 찾음 is None:
            모르는것.append(키)
        elif 찾음 not in 자리.values():      # 같은 열이 두 번이면 앞것을 쓴다
            자리[n] = 찾음
    return 자리, 모르는것


def 읽기(data: bytes, 열: list[str],
       라벨: dict[str, str] | None = None) -> tuple[list[tuple[int, dict]], list[str]]:
    """올라온 엑셀을 (엑셀 행 번호, {열: 값}) 목록으로.

    행 번호를 함께 돌려주는 이유: 어느 줄이 왜 빠졌는지 알려 주려면 사람이
    엑셀에서 찾을 수 있는 번호여야 한다. 머리글이 1행이므로 값은 2행부터다.

    Raises:
        XlsxError: 엑셀로 읽을 수 없거나 머리글을 하나도 못 알아봤을 때.
    """
    표 = read_sheet(data)
    if not 표:
        raise XlsxError("빈 파일입니다. 양식을 받아 채운 뒤 올려 주세요.")
    자리, 모르는것 = 머리풀기(표[0], 열, 라벨)
    if not 자리:
        raise XlsxError(
            "첫 줄에서 아는 열 이름을 하나도 못 찾았습니다. "
            "«엑셀 양식 받기» 로 받은 파일의 첫 줄을 지우지 말고 채워 주세요.")

    줄들: list[tuple[int, dict]] = []
    for i, 행 in enumerate(표[1:], start=2):
        값들 = {이름: (행[n].strip() if n < len(행) else "")
              for n, 이름 in 자리.items()}
        if not any(값들.values()):          # 통째로 빈 줄은 조용히 건너뛴다
            continue
        줄들.append((i, 값들))
    return 줄들, 모르는것
