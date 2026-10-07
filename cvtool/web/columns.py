"""표의 열 — 어떤 열이 있고, 머리글·너비·값을 어떻게 보이나. 칸 편집 모양도.
"""

from __future__ import annotations

import html
import json

from .. import colformula, review
from ..auth import can, User
from ..edit import (
    custom_field_spec, field_spec, MULTILINE_OK, READONLY_FIELDS, REGISTRY_FIELDS,
)
from ..export import build_xlsx
from ..recruit import RECRUIT_COLUMNS, STAGES, STARTED_COLUMN
from ..schemas import columns as table_columns, is_tier_venue, TIER_COLUMN_PREFIX

from .state import auth, mailing, recruit, registry, store


def _cell(cid: str, col: str, 표시: str, 원본: str, spec, *,
          scope: str = "기본", cls: str = "", fx: str = "", 수식값: bool = False) -> str:
    """표 안의 편집 가능한 칸 하나.

    `표시` 는 칸에 그리는 글자, `원본` 은 편집칸이 시작하는 값이자 저장할 때
    보내는 «이전 값» 이다. 둘 다 **화면에 뜨는 값**이어야 한다 — 레코드가 든
    날값을 이전 값으로 보내면(졸업일과 대조하기 전의 학위상태 같은 것) 손도
    안 댄 칸이 저장할 때마다 "다른 사람이 방금 바꿨습니다" 로 튕긴다.
    (`edit.보이는값` 이 그 값을 뽑는다. 부르는 쪽이 그것을 넘긴다.)

    `fx` 는 그 열에 걸린 수식. `수식값` 이면 칸의 글은 수식이 낸 값이고
    `원본`(사람이 적은 값)은 빈칸이다 — 적으면 그 값이 이긴다.
    """
    opts = json.dumps(list(spec.선택지), ensure_ascii=False) if spec.입력 == "select" else "[]"
    # 속성 안의 줄바꿈을 브라우저가 어떻게 다루는지에 기대지 않는다. 값이
    # 한 칸 밀리면 편집칸에 엉뚱한 글이 들어간다.
    담기 = lambda v: html.escape(v).replace("\n", "&#10;")
    if 수식값:
        cls += " fxcol"
    덧 = f" data-fx='{html.escape(fx)}'" if fx else ""
    return (
        f"<td class='edit{cls}'{덧} data-id='{html.escape(cid)}' data-col='{html.escape(col)}'"
        f" data-raw='{담기(원본)}' data-kind='{html.escape(spec.입력)}'"
        f" data-opts='{html.escape(opts)}' data-scope='{scope}'"
        f" data-help='{html.escape(spec.도움말)}' title='{담기(표시)}"
        + (f"&#10;(수식 {담기(fx)} — 칸에 적으면 그 값이 우선, 지우면 다시 수식)"
           if fx else "") + "'>"
        f"{html.escape(표시)}</td>"
    )


def _긴글가능(col: str, 추가열: dict | None, 구분: str = "") -> bool:
    """이 열에 «긴 글» 을 켤 수 있나.

    형식이 정해진 칸(날짜·연월·전화·이메일·선택·숫자)은 안 된다 — 줄바꿈이
    들어가면 그 형식 검사가 무너진다. 명칭 사전 열과 계산 결과 열도 값을 사람이
    직접 쓰는 자리가 아니라 뺀다.
    """
    if 추가열 is not None:
        return custom_field_spec(추가열).입력 == MULTILINE_OK
    if 구분 == "채용 현황":
        return col == "채용_비고"
    if 구분 == "관리 정보":
        return False
    if col in REGISTRY_FIELDS or col in READONLY_FIELDS:
        return False
    if col.startswith(TIER_COLUMN_PREFIX) or is_tier_venue(col):
        return False
    return field_spec(col).입력 == MULTILINE_OK


def _editable(col: str) -> bool:
    """표에서 직접 고칠 수 있는 열인가.

    명칭 사전이 관리하는 열(소속·학교·전공)도 **고칠 수 있다.** 사전에 있는
    이름을 적으면 그것을 고른 것이 되고, 아닌 값을 적으면 그 사람만 사전을 안
    따라간다(`edit._사전열_고치기`). 원표기는 그대로 남으므로 «사전 따라가기»
    로 언제든 되돌아온다.
    """
    return (
        col not in READONLY_FIELDS
        and not col.startswith(TIER_COLUMN_PREFIX)
        and not is_tier_venue(col)
    )


def _tsv_줄들(tsv: str) -> list[list[str]]:
    """TSV 를 줄·칸으로 가른다. **따옴표로 감싼 칸 안의 줄바꿈을 살린다.**

    여러 줄로 적어 둔 비고 같은 값이 한 칸 안에 들어 있으면 화면이 그 칸을
    `"가\n나"` 로 감싸 보낸다 (엑셀이 붙여넣을 때 쓰는 그 규칙). 그냥
    `split("\n")` 하면 그 한 칸이 두 줄로 쪼개져 표가 통째로 어긋난다.
    """
    글 = (tsv or "").replace("\r\n", "\n").replace("\r", "\n")
    줄들: list[list[str]] = []
    칸들: list[str] = []
    지금: list[str] = []
    따옴표안 = False
    i = 0
    while i < len(글):
        ch = 글[i]
        if 따옴표안:
            if ch == '"':
                if i + 1 < len(글) and 글[i + 1] == '"':   # "" 는 따옴표 한 개
                    지금.append('"')
                    i += 2
                    continue
                따옴표안 = False
            else:
                지금.append(ch)
        elif ch == '"' and not 지금:
            따옴표안 = True                                # 칸 맨 앞의 따옴표만
        elif ch == "\t":
            칸들.append("".join(지금))
            지금 = []
        elif ch == "\n":
            칸들.append("".join(지금))
            줄들.append(칸들)
            칸들, 지금 = [], []
        else:
            지금.append(ch)
        i += 1
    if 지금 or 칸들:
        칸들.append("".join(지금))
        줄들.append(칸들)
    return [줄 for 줄 in 줄들 if any(c.strip() for c in 줄)]


def _tsv_to_xlsx(tsv: str) -> bytes:
    """화면에 보이는 표를 그대로 엑셀로 만든다.

    표마다 서버 라우트를 만들지 않으려고, 화면에서 만든 TSV 를 받아
    같은 xlsx 작성기로 넘긴다. 머리글이 겹치면 뒤에 번호를 붙인다.
    """
    lines = _tsv_줄들(tsv)
    if not lines:
        return build_xlsx([], ["(내용 없음)"])
    header, seen = [], {}
    for name in lines[0]:
        name = name.strip() or "-"
        seen[name] = seen.get(name, 0) + 1
        header.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    rows = []
    for cells in lines[1:]:
        rows.append({h: (cells[i] if i < len(cells) else "") for i, h in enumerate(header)})
    return build_xlsx(rows, header)


#: DB 에는 있지만 CV 에서 뽑은 값이 아닌 열 — 언제 등록했고 원본이 무엇인지.
#: 표에서 고칠 수 없다 (등록년도는 상세 화면에서 고친다).
MANAGE_COLUMNS = ("등록년도", "등록일시", "원본_파일명", "보관_만료일")


def 열목록(registry_=None) -> list[tuple[str, str, bool]]:
    """이 시스템이 아는 **모든 열**을 (구분, 열이름, 추가한열인가) 로 돌려준다.

    표 항목 탭이 이걸 그대로 보여준다. 지원자 정보 열만 관리할 수 있으면
    채용 현황 열 이름을 못 바꾸고, 관리 정보 열은 아예 표에 못 올린다.

    추가한 열도 **어느 표에 속하는지**를 달고 그 묶음 안에 들어간다.
    직접 만든 '면접 평점' 이 지원자 정보인지 채용 현황인지 모르면, 어느 표에서
    찾아야 하는지도 알 수 없다.
    """
    reg = registry_ or registry
    out: list[tuple[str, str, bool]] = []
    out += [("지원자 정보", c, False) for c in table_columns(reg)]
    out += [("지원자 정보", c, True) for c in 추가열("지원자 정보")]
    # 표에 나오는데 여기 없으면 이름을 바꾸거나 숨길 방법이 없다.
    # 지원자_ID 는 내부 열쇠지만 표에 올릴 수 있어야 한다 (엑셀에서 대조할 때 쓴다).
    out += [("관리 정보", c, False) for c in ("지원자_ID", *MANAGE_COLUMNS, MAIL_COLUMN)]
    out += [("채용 현황", c, False) for c in RECRUIT_COLUMNS]
    out += [("채용 현황", c, True) for c in 추가열("채용 현황")]
    return out


def 추가열(구분: str) -> list[str]:
    """그 묶음에 속하는 추가한 열 이름."""
    return store.field_names(구분)


#: 지원자마다 어떤 메일을 보냈는지 한 열로. 세어 나오는 값이라 못 고친다.
MAIL_COLUMN = "메일_발송이력"


def 지원자열(registry_=None) -> list[str]:
    """인재 Pool·엑셀에 나갈 수 있는 열 **전부.**

    지원자 정보뿐 아니라 채용 현황 열과 메일 발송이력까지 한 표에서 본다.
    한 사람에 대해 아는 것을 보려고 화면을 옮겨 다니지 않아도 되게.
    채용 열은 여기서 **보기만** 한다 (고치는 건 채용 현황 화면 몫).
    """
    return (list(table_columns(registry_ or registry))
            + ["지원자_ID"] + list(MANAGE_COLUMNS)
            + 추가열("지원자 정보") + list(RECRUIT_COLUMNS)
            + 추가열("채용 현황") + [MAIL_COLUMN])


#: 관리 정보 중 처음에는 접어 두는 열. 표가 넓어지기만 하고 평소엔 안 본다.
#: 표 항목 탭에서 숨김을 풀면(설정이 생기면) 그때부터 보인다.
MANAGE_HIDDEN_BY_DEFAULT = ("지원자_ID", "등록일시", "원본_파일명", "보관_만료일")


#: 처음 볼 때 **화면 표에서만** 숨겨 두는 열. 대부분 비어 있거나 가끔만 보는 값이라,
#: 다 펼쳐 두면 정작 봐야 할 열이 화면 밖으로 밀렸다. 엑셀 내려받기에는 그대로
#: 나간다(`엑셀열`). 표 항목에서 숨김을 풀면 화면에도 나온다.
SCREEN_HIDDEN_BY_DEFAULT = (
    "영문_이름", "생년월일", "전화번호", "현재_소속_상세", "현재_지도교수",
    "박사_석박통합", "박사_지도교수", "박사_시작", "석사_지도교수", "석사_시작", "석사_졸업",
    "학사_학교", "학사_전공", "학사_시작", "학사_졸업",
    "경력_시작", "경력_종료", "구글_스칼라_링크", "검토_필요",
)


def 기본숨김(col: str, cfg: dict) -> bool:
    """설정을 한 번도 안 건드린 열 중 처음부터 숨겨 두는 열인가."""
    return (col in MANAGE_HIDDEN_BY_DEFAULT or col in SCREEN_HIDDEN_BY_DEFAULT) \
        and col not in cfg


def 표열(registry_=None) -> list[str]:
    """지원자 표에 실제로 나갈 열 (숨김·순서 설정 반영)."""
    cfg = store.column_config()
    return store.arrange([c for c in 지원자열(registry_) if not 기본숨김(c, cfg)])


def 엑셀열(registry_=None) -> list[str]:
    """엑셀 내려받기 열 — 화면에서만 처음부터 숨긴 열(`SCREEN_HIDDEN_BY_DEFAULT`)도 담는다.

    사람이 표 항목에서 직접 숨긴 열은 엑셀에도 안 나간다 (예전과 같다).
    """
    cfg = store.column_config()
    return store.arrange([c for c in 지원자열(registry_)
                          if not (c in MANAGE_HIDDEN_BY_DEFAULT and c not in cfg)])


#: 열 이름별 너비 등급. 값이 짧은 열에 넓은 자리를 주면 정작 긴 글이 잘린다.
_넓은열 = {
    "경력_요약", "검토_사유", "연구분야_키워드", "보유기술", "수상실적", "1저자_해외논문_제출처",
    "메일_발송이력", "비고", "채용_비고", "현재_소속_상세", "중복_메모", "원본_파일명",
}
_중간열 = {
    "한글_이름", "영문_이름", "이메일", "전화번호", "현재_소속", "부서", "과제",
    "경력_회사", "직책", "최종상태", "박사_학교", "석사_학교", "학사_학교",
    "박사_전공", "석사_전공", "학사_전공", "박사_지도교수", "석사_지도교수",
    "현재_지도교수", "등록일시", "보관_만료일",
}
_짧은열 = {
    "검토_필요", "박사_석박통합", "등록년도", "현재_신분",
    "박사_학위상태", "생년월일",
}


def 열이름도움(col: str) -> str:
    """머리글에 마우스를 올리면 뜨는 글 — 수식·메일에 쓰는 **내부 이름**."""
    return f"수식·메일 자리표시자에서는 {col}"


def 머리글(이름: str) -> str:
    """표 머리글 HTML. 밑줄 뒤에서 줄바꿈해도 된다고 알려준다.

    `저널_주저자_수` 처럼 공백 없는 긴 이름은 한 낱말로 취급돼 줄바꿈이 안
    되고, 값은 한 글자뿐인 열을 통째로 넓혀 버린다. <wbr> 로 끊을 자리를
    준다 (글자를 넣는 게 아니라 '여기서 끊어도 된다' 는 표시다).
    """
    return html.escape(이름).replace("_", "_<wbr>")


def 열폭(col: str) -> str:
    """이 열에 줄 너비 등급 (CSS 클래스 이름)."""
    if col in _넓은열:
        return "w-xl"
    if col in _중간열:
        return "w-lg"
    if col in _짧은열:
        return "w-sm"
    if is_tier_venue(col):
        return "w-xl"
    if col.endswith("_수") or col.startswith(TIER_COLUMN_PREFIX):
        return "w-xs"
    if col.endswith(("_시작", "_졸업", "_종료")):
        return "w-sm"
    if col in STAGES:
        return "w-sm"
    return "w-md"


def 라벨(열들: list[str]) -> dict[str, str]:
    return store.labels(열들)


#: 수식에서 늘 쓸 수 있는 덧열 — 표에는 없지만 줄마다 계산해 넣는다.
덧열 = ("지원자_ID", "매칭_과제", "매칭_점수", "채용중")


def 모든열() -> set[str]:
    """수식(대시보드·열 수식)에서 쓸 수 있는 열 이름 전부."""
    return set(지원자열()) | set(RECRUIT_COLUMNS) | set(store.field_names()) | set(덧열)


def 덧값(행: dict, cid: str, 시작한사람, 상위매칭: dict,
       채용중열: bool | None = None) -> None:
    """`채용중` · `매칭_과제` · `매칭_점수` 를 줄에 넣는다.

    `채용중` 이라는 **추가 열**을 만들어 두었으면 그쪽이 이긴다 (사람이 수식을
    걸어 두려고 만든 것이다).
    """
    m = 상위매칭.get(cid)
    행["매칭_과제"] = (m or {}).get("과제명", "") if isinstance(m, dict) else ""
    행["매칭_점수"] = str((m or {}).get("점수", "") or "") if isinstance(m, dict) else ""
    if 채용중열 is None:
        채용중열 = store.field("채용중") is not None
    if not 채용중열:
        행["채용중"] = "Y" if cid in 시작한사람 else ""


def 수식열채우기(행: dict, 적은값: dict, 수식들: dict | None = None,
            빈칸: dict | None = None) -> set[str]:
    """추가 열에 걸린 수식을 **사람이 안 적은 칸만** 계산해 넣는다."""
    수식들 = store.field_formulas() if 수식들 is None else 수식들
    if not 수식들:
        return set()
    return colformula.채우기(행, 수식들, 적은값,
                          빈칸 if 빈칸 is not None else {c: "" for c in 모든열()})


def _표값맵() -> dict[str, dict[str, str]]:
    """추출 결과에 없는 열의 값. {지원자_ID: {열: 값}}

    관리 정보 · 추가한 열 · 채용 현황 · 메일 발송이력을 한 번에 모은다.
    화면과 엑셀이 **같은 함수**를 쓰므로 둘이 어긋날 수 없다.
    """
    합침 = store.meta_map()
    for cid, 값들 in store.custom_map().items():
        합침.setdefault(cid, {}).update(값들)

    부서명 = {d["id"]: d["이름"] for d in auth.departments()}
    과제명 = {p["id"]: p["이름"] for p in auth.projects()}
    for cid, p in recruit.all().items():
        칸 = 합침.setdefault(cid, {})
        칸["부서"] = 부서명.get(p.부서_id, "")
        칸["과제"] = 과제명.get(p.project_id, "")
        칸["최종상태"] = p.최종상태
        칸["채용_비고"] = p.채용_비고
        for 단계 in STAGES:
            칸[단계] = p.단계상태.get(단계, "")
    for cid, 보낸것 in mailing.sent_summary().items():
        합침.setdefault(cid, {})[MAIL_COLUMN] = 보낸것

    # 검토 사유는 **남은 것만** 보여준다. 사람이 '확인함' 을 눌렀는데도 원문이
    # 표에 그대로 남아 있으면 아직 볼 게 있는 것처럼 보인다. DB 원문은 그대로
    # 두고 (LLM 이 무엇을 확신 못 했는지의 기록이다) 보이는 글만 줄인다.
    끝낸것 = store.review_done_map()
    시작한사람 = recruit.started()
    수식들 = store.field_formulas()
    사용자값 = store.custom_map() if 수식들 else {}
    상위매칭 = store.top_matches() if 수식들 else {}
    빈칸 = {c: "" for c in 모든열()} if 수식들 else {}
    채용중열 = store.field("채용중") is not None
    for rec in store.list_all():
        cid = rec.지원자_ID
        칸 = 합침.setdefault(cid, {})
        # 표에 나오는 값은 전부 여기서 나와야 표 항목 탭에서 관리할 수 있다.
        칸["지원자_ID"] = cid
        칸[STARTED_COLUMN] = "채용 중" if cid in 시작한사람 else "인재 Pool"
        if (rec.검토_사유 or "").strip():
            칸["검토_사유"] = review.display(rec.검토_사유, 끝낸것.get(cid, set()))
        if 수식들:
            # 수식은 한 사람의 **모든** 값을 본다 — 추출 값 + 여기서 모은 값.
            행 = {**rec.to_row(registry), **칸}
            덧값(행, cid, 시작한사람, 상위매칭, 채용중열)
            for 열 in 수식열채우기(행, 사용자값.get(cid, {}), 수식들, 빈칸):
                칸[열] = 행[열]
    return 합침


def _볼수있나(me: User, 지원자_ID: str) -> bool:
    """현업은 **배정된 과제의 지원자만** 볼 수 있다.

    화면에서 감추는 것으로는 부족하다. 주소를 직접 쳐도 막혀야 한다.
    """
    if not can(me, "지원자_조회"):
        return False
    보이는 = auth.visible_project_ids(me)
    if 보이는 is None:
        return True
    return recruit.get(지원자_ID).project_id in 보이는
