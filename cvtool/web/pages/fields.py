"""표 항목 (열 추가 · 순서 · 숨김 · 선택지).
"""

from __future__ import annotations

import html
import json
import urllib.parse

from ...auth import User
from ...edit import CHOICE_FIELDS, field_spec, REGISTRY_FIELDS
from ...recruit import FIXED_STATUSES, STAGES
from ...schemas import is_tier_venue, NAME_COLUMNS, TIER_COLUMN_PREFIX
from ... import colformula
from ...store import CUSTOM_SCOPES, CUSTOM_TYPES, DEFAULT_LONG_COLUMNS as store_DEFAULT_LONG, 수식정리

from ..state import audit, recruit, store
from ..columns import _긴글가능, 기본숨김, 모든열, 열목록, 표열
from ..layout import _page, _알림, _없는주소, _정적JS, 함수설명자료
from ..router import 라우트


#: 열이 어디에 쓰이는지 — 표 항목 탭에서 한눈에 보이게 적어 둔다.
COLUMN_GROUP_NOTE = {
    "지원자 정보": "인재 Pool · 엑셀 내려받기",
    "관리 정보": "언제 등록했고 원본이 무엇인지. 표에서는 보기만 합니다.",
    "채용 현황": "채용 현황 표. 어떤 열을 쓸지는 [표 열 구성] 에서 고릅니다.",
}


def _선택지편집(col: str, 지금: list[str], action: str, 고정: tuple[str, ...] = (),
             도움말: str = "") -> str:
    """선택지를 그 자리에서 고치는 작은 폼.

    형식 검사·추출 스키마를 건드리지 않는 열에만 붙인다.
    """
    보임 = " | ".join(v for v in 지금 if v)
    잠긴것 = (
        "<br><span class='muted'>못 빼는 값: "
        + html.escape(", ".join(v or "(빈칸)" for v in 고정)) + "</span>"
        if 고정 else ""
    )
    return (
        f"<form method='post' action='{action}' style='display:flex;gap:6px;"
        "align-items:center;flex-wrap:wrap;margin-top:4px'>"
        f"<input type='hidden' name='col' value='{html.escape(col)}'>"
        f"<input type='text' name='choices' value='{html.escape(보임)}'"
        " style='width:230px' placeholder='| 로 구분'>"
        "<button type='submit' class='sec'>선택지 저장</button>"
        + (f"<span class='muted'>{도움말}</span>" if 도움말 else "")
        + f"</form>{잠긴것}"
    )


def _수식편집(col: str, 수식: str) -> str:
    """추가한 열에 거는 수식을 그 자리에서 고치는 작은 폼."""
    return (
        "<form method='post' action='/fields/formula' style='display:flex;gap:6px;"
        "align-items:center;flex-wrap:wrap;margin-top:4px'>"
        f"<input type='hidden' name='col' value='{html.escape(col)}'>"
        f"<input type='text' name='formula' class='fx' data-kind='row' value='{html.escape(수식)}'"
        " autocomplete='off' style='width:230px' placeholder='수식 (없으면 비움) 예: =IF(과제&lt;&gt;&quot;&quot;,&quot;Y&quot;,&quot;N&quot;)'>"
        "<button type='submit' class='sec'>수식 저장</button></form>"
    )


#: 열 수식 칸의 자동완성·인자 안내 (대시보드 편집과 같은 스크립트).
_FXAC_JS = '\n<script>' + _정적JS("formula_autocomplete.js") + '</script>'


def _수식도우미() -> str:
    from ... import expr

    자료 = {"열": sorted(모든열()), "행함수": list(expr.PUBLIC_FUNC_NAMES),
          "집계함수": [], "대상": []}
    자료["설명"] = 함수설명자료(자료["행함수"])
    return ("<script>window.수식목록 = " + json.dumps(자료, ensure_ascii=False)
            + ";</script>" + _FXAC_JS)


#: 열 순서를 **끌어서** 정한다.
#:
#: 예전에는 칸에 숫자를 쳐서 자리를 매겼다. 열이 쉰 개가 넘으면 사람이 할 일이
#: 아니다 — 하나를 앞으로 보내려고 나머지 번호를 전부 다시 세야 했다.
#: 이제 순서는 **줄의 위치**가 정하고, 숨은 칸이 그 위치를 그대로 받아 적는다.
_COLORDER_JS = '\n<script>' + _정적JS("column_order.js") + '</script>'


def _fields_page(me: User, error: str = "", msg: str = "") -> bytes:
    """표에 나갈 열을 관리한다 — **이 시스템이 아는 모든 열을 한 자리에서.**

    예전에는 지원자 정보 열과 직접 추가한 열만 보였다. 그래서 채용 현황 열
    이름을 바꿀 수 없었고, 등록년도·원본 파일명 같은 관리 정보 열은 아예
    표에 올릴 수도 없었다. 이제 세 묶음을 모두 보여주고, 추가한 열도 어느
    묶음에 속하는지 달아서 그 자리에 끼워 넣는다.

    고칠 수 있는 것과 없는 것의 경계는 하나다 — **형식 검사와 추출 스키마.**
    거기 걸려 있지 않은 것(단계 상태 목록, 추가한 열의 선택지·유형·이름)은
    고칠 수 있고, 걸려 있는 것(지원자 정보 열의 선택지)은 못 고친다.
    """
    사용자열 = {f["이름"]: f for f in store.fields()}
    cfg = store.column_config()
    긴글열 = store.긴글열()
    유형옵션 = "".join(f"<option>{t}</option>" for t in CUSTOM_TYPES)
    구분옵션 = "".join(f"<option>{g}</option>" for g in CUSTOM_SCOPES)
    쓰는채용열 = set(recruit.columns())
    지금상태 = recruit.statuses()

    def 설명(구분: str, col: str, 추가열: bool) -> str:
        if 추가열:
            f = 사용자열[col]
            머리 = f"{f['유형']}"
            꼬리 = (f"<br><span class='muted'>{html.escape(f['만든일시'])}"
                  + (f" ({html.escape(f['만든이'])})" if f["만든이"] else "")
                  + "</span>")
            수식칸 = _수식편집(col, f.get("수식") or "")
            if f["유형"] == "선택":
                고를것 = [o.strip() for o in (f["선택지"] or "").split("|") if o.strip()]
                return 머리 + _선택지편집(col, 고를것, "/fields/choices") + 수식칸 + 꼬리
            return 머리 + 수식칸 + 꼬리
        if 구분 == "관리 정보":
            return "<span class='muted'>자동 기록 (고치려면 지원자 상세 화면)</span>"
        if 구분 == "채용 현황":
            if col in STAGES:
                return "선택" + _선택지편집(
                    col, 지금상태, "/recruit/statuses", 고정=FIXED_STATUSES,
                    도움말="네 단계가 같은 목록을 씁니다",
                )
            if col in ("부서", "과제"):
                return "<span class='muted'>조직에서 고른 값 (부서·과제 화면에서 관리)</span>"
            if col == "최종상태":
                return "<span class='muted'>계산 결과 (단계 상태에서 정함)</span>"
            return "텍스트"
        if col in CHOICE_FIELDS:
            return ("선택 · " + html.escape(", ".join(v or "(빈칸)" for v in CHOICE_FIELDS[col]))
                    + "<br><span class='muted'>추출 스키마에 걸려 있어 못 바꿉니다</span>")
        if col in REGISTRY_FIELDS:
            return ("명칭 사전 " + html.escape(NAME_COLUMNS[col])
                    + "<br><span class='muted'>사전에 없는 값을 적으면 "
                    "<b>그 지원자만</b> 사전을 안 따라갑니다 "
                    "(상세에서 «사전 따라가기» 로 되돌립니다)</span>")
        if col.startswith(TIER_COLUMN_PREFIX):
            return "<span class='muted'>계산 결과 (논문 목록에서 셈)</span>"
        if is_tier_venue(col):
            return ("<span class='muted'>계산 결과 (그 등급의 1저자 논문을"
                    " 최근 것부터. 이름·등급·IF 는 명칭 관리에서 정한 값)</span>")
        spec = field_spec(col)
        return html.escape(spec.도움말 or "텍스트")

    전체 = 열목록()
    묶음수: dict[str, int] = {}
    for 구분, _c, _a in 전체:
        묶음수[구분] = 묶음수.get(구분, 0) + 1

    # **지금 표에 나오는 순서 그대로** 늘어놓는다.
    #
    # 예전에는 지원자 정보 / 관리 정보 / 채용 현황으로 묶어서 보여줬다. 그러면
    # 화면에서 실제로 몇 번째에 있는 열인지 알 수가 없고, 순서를 바꾸려면 머릿속
    # 으로 세 묶음을 합쳐 봐야 했다. 보이는 대로 늘어놓으면 그럴 일이 없다.
    구분맵 = {col: (구분, 추가) for 구분, col, 추가 in 전체}
    보이는순서 = [c for c in 표열() if c in 구분맵]
    숨은것 = [c for _g, c, _a in 전체 if c not in set(보이는순서)]
    차례 = [(구분맵[c][0], c, 구분맵[c][1]) for c in 보이는순서 + 숨은것]

    rows = []
    첫숨김 = len(보이는순서)
    for i, (구분, col, 추가열) in enumerate(차례, start=1):
        if i == 첫숨김 + 1:
            rows.append(
                "<tr class='grouphead'><td colspan='8'><b>숨긴 열</b> "
                f"<span class='muted'>{len(숨은것)}개 — 표에 안 나옵니다. "
                "숨김을 풀면 여기 순서대로 뒤에 붙습니다.</span></td></tr>"
            )
        c = cfg.get(col, {})
        숨김중 = c.get("숨김") or 기본숨김(col, cfg)
        꼬리 = ""
        if 구분 == "채용 현황" and col not in 쓰는채용열:
            꼬리 = " <span class='muted'>(지금 표에 없음)</span>"
        이름칸 = (
            # 이름을 입력칸에만 두면 표의 찾기·복사가 못 잡는다. 글자로도 남긴다.
            f"<div class='muted' style='font-size:11px'>{html.escape(col)}</div>"
            f"<input type='text' form='colform' name='rename_{i}'"
            f" value='{html.escape(col)}' style='width:150px'"
            f" data-orig='{html.escape(col)}' oninput='markDirty(this)'>"
            f"<select form='colform' name='scope_{i}' onchange='markDirty(this)'"
            f" data-orig='{html.escape(사용자열[col].get('구분') or '지원자 정보')}'"
            " style='width:110px;margin-top:3px'>"
            + "".join(
                f"<option{' selected' if g == (사용자열[col].get('구분') or '지원자 정보') else ''}>"
                f"{g}</option>" for g in CUSTOM_SCOPES)
            + "</select>"
            if 추가열 else f"{html.escape(col)}{꼬리}"
        )
        rows.append(
            f"<tr draggable='true' data-col='{html.escape(col)}'>"
            # 끌어서 옮기는 손잡이 + 한 칸씩 옮기는 단추. 숫자를 쳐서 자리를
            # 매기는 건 열이 쉰 개가 넘으면 사람이 할 일이 아니다.
            "<td class='ctl grip' title='끌어서 옮기세요'>"
            "<span class='griph'>⠿</span> "
            "<button type='button' class='sec tiny' onclick='colMove(this,-1)'"
            " title='위로'>↑</button>"
            "<button type='button' class='sec tiny' onclick='colMove(this,1)'"
            " title='아래로'>↓</button></td>"
            f"<td>{이름칸}</td>"
            f"<td><span class='pill {'p-완료' if 추가열 else 'p-대기중'}'>"
            f"{'추가한 열' if 추가열 else html.escape(구분)}</span></td>"
            f"<td style='white-space:normal'>{설명(구분, col, 추가열)}</td>"
            f"<td class='ctl'><input type='hidden' form='colform' name='col_{i}'"
            f" value='{html.escape(col)}'>"
            f"<input type='text' form='colform' name='label_{i}'"
            f" value='{html.escape(c.get('표시이름') or '')}'"
            f" placeholder='{html.escape(col)}' style='width:150px'"
            f" data-orig='{html.escape(c.get('표시이름') or '')}' oninput='markDirty(this)'></td>"
            f"<input type='hidden' form='colform' name='order_{i}' value='{i}'"
            " class='ordfield'>"
            f"<td><label><input type='checkbox' form='colform' name='hide_{i}'"
            f"{' checked' if 숨김중 else ''} onchange='markDirty(this)'"
            f" data-orig=''> 숨김</label></td>"
            + ("<td><label><input type='checkbox' form='colform'"
               f" name='long_{i}'{' checked' if col in 긴글열 else ''}"
               " onchange='markDirty(this)' data-orig=''> 긴 글</label></td>"
               if _긴글가능(col, 사용자열.get(col), 구분)
               else "<td class='muted'>-</td>")
            + "<td>" + (
                "<form method='post' action='/fields/delete' style='display:inline'"
                " onsubmit=\"return confirm('이 열과 여기 들어있던 모든 값이 지워집니다.')\">"
                f"<input type='hidden' name='name' value='{html.escape(col)}'>"
                "<button class='danger'>삭제</button></form>" if 추가열
                else "<span class='muted'>-</span>"
            ) + "</td></tr>"
        )

    알림 = _알림(msg=msg)
    오류 = _알림(err=error)
    묶음요약 = " · ".join(f"{k} {v}개" for k, v in 묶음수.items())
    return _page(
        "표 항목",
        알림
        + "<div class='card'><h2>열 추가</h2>" + 오류
        + "<form method='post' action='/fields/add' style='display:flex;gap:8px;flex-wrap:wrap'>"
        "<input type='text' name='name' placeholder='열 이름' required>"
        f"<select name='scope'>{구분옵션}</select>"
        f"<select name='type'>{유형옵션}</select>"
        "<input type='text' name='choices' placeholder=\"선택지 (선택 유형만, | 로 구분)\""
        " style='width:280px'>"
        "<input type='text' name='formula' class='fx' data-kind='row' autocomplete='off'"
        " style='width:260px'"
        " placeholder='수식 (선택) 예: =IF(과제&lt;&gt;\"\",\"Y\",\"N\")'>"
        "<button type='submit'>추가</button></form>"
        "<p class='muted'><b>구분</b>을 고르면 그 표에 붙습니다 — "
        "<b>지원자 정보</b>는 인재 Pool·엑셀에, <b>채용 현황</b>은 채용 현황 표에. "
        "유형에 따라 입력칸이 달라지고 형식이 강제됩니다. "
        "<b>값은 사람이 채웁니다</b> — LLM 이 자동으로 채우지 않습니다.</p>"
        "<p class='muted'><b>수식</b>을 걸면 칸이 비어 있는 사람은 수식 값이 나옵니다 "
        "(표에는 <i>기울인 글씨</i>로). 예: <code>=IF(과제&lt;&gt;\"\",\"Y\",\"N\")</code> · "
        "<code>=IF(저널_수&gt;=3,\"상\",\"하\")</code> · "
        "<code>=TEXT(박사_졸업,\"yyyy.mm\")</code>. "
        "<b>칸에 직접 적으면 그 값이 우선</b>이고, 칸을 지우면 다시 수식 값이 나옵니다. "
        "다른 열 이름을 그대로 씁니다 (대시보드 목록과 같은 문법).</p></div>"

        + "<div class='card'><h2>표에 나갈 열 "
        f"<span class='muted'>{html.escape(묶음요약)}</span></h2>"
        "<form method='post' action='/fields/columns' id='colform' class='mergebar'>"
        "<button type='submit'>고친 내용 저장</button>"
        "<span class='muted'><b>표에 나오는 순서 그대로</b> 늘어놨습니다. "
        "줄을 <b>끌어서</b> 옮기거나 <b>↑ ↓</b> 를 누르고, 이름·숨김을 고친 뒤 "
        "<b>한 번만</b> 누르세요.</span></form>"
        "<div class='scroll'><table data-name='표 항목' id='colorder'>"
        "<tr><th class='ctl' style='width:86px'>순서</th>"
        "<th>열 이름</th><th>구분</th><th>입력 형식</th>"
        "<th class='ctl'>표에 보일 이름</th><th>숨김</th>"
        "<th title='켜면 그 칸에 여러 줄을 넣을 수 있습니다'>긴 글</th>"
        "<th></th></tr>"
        + "".join(rows) + "</table></div>"
        + _COLORDER_JS +
        "<p class='muted'>여기서 정한 이름·순서·숨김은 <b>화면과 엑셀에 함께</b> 적용됩니다. "
        "<b>긴 글</b>을 켠 열은 표에서 칸을 눌렀을 때 여러 줄 상자가 "
        "뜹니다 (Enter 는 줄바꿈, Ctrl+Enter 가 저장). 형식이 정해진 열은 "
        "켤 수 없습니다. "
        "고칠 수 있는 것과 없는 것의 경계는 하나입니다 — <b>형식 검사와 추출 스키마</b>. "
        "단계 상태 목록과 추가한 열의 선택지·유형·이름은 고칠 수 있고, 지원자 정보 열의 "
        "선택지는 추출 스키마에 걸려 있어 못 고칩니다. 안 쓰는 열은 <b>숨김</b>으로 두세요.</p>"
        "</div>" + _수식도우미(),
        me=me,
    )


@라우트("GET", '/fields', 권한='열_구성', 거부말='표 항목 추가는 관리자만 할 수 있습니다.')
def get_fields(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return self._send(_fields_page(me, (params.get("err") or [""])[0],
                                   (params.get("msg") or [""])[0]))
    return _없는주소(self)


@라우트("POST", '/fields/add', 권한='열_구성', 거부말='표 항목 추가는 관리자만 할 수 있습니다.')
def post_fields_add(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    이름 = (data.get("name") or [""])[0]
    try:
        구분 = (data.get("scope") or ["지원자 정보"])[0]
        수식 = 수식정리((data.get("formula") or [""])[0])
        colformula.검사(이름.strip(), 수식, 모든열(), store.field_formulas())
        store.add_field(
            이름,
            (data.get("type") or ["텍스트"])[0],
            (data.get("choices") or [""])[0],
            만든이=me.아이디,
            구분=구분,
            수식=수식,
        )
    except ValueError as exc:
        return self._redirect("/fields?err=" + urllib.parse.quote(str(exc)))
    audit.record(me.아이디, "표항목", 이름, 항목="구분", 새값=구분, 비고="열 추가")
    return self._redirect("/fields")
    return _없는주소(self)


@라우트("POST", '/fields/choices', 권한='열_구성', 거부말='표 항목 설정은 관리자만 바꿀 수 있습니다.')
def post_fields_choices(self, me, path):
    # 추가한 열의 선택지 고치기. 형식 검사·추출 스키마와 무관한 열이라
    # 고칠 수 있다. 이미 쓰고 있는 값을 빼면 store 가 거부한다.
    data = urllib.parse.parse_qs(
        self._read_body().decode("utf-8", "replace"), keep_blank_values=True
    )
    col = (data.get("col") or [""])[0]
    새선택지 = (data.get("choices") or [""])[0]
    옛것 = store.field(col)
    try:
        store.update_field(col, 선택지=새선택지)
    except ValueError as exc:
        return self._redirect("/fields?err=" + urllib.parse.quote(str(exc)))
    audit.record(me.아이디, "표항목", col, 항목="선택지",
                 이전값=(옛것 or {}).get("선택지", ""), 새값=새선택지.strip())
    return self._redirect("/fields?msg=" + urllib.parse.quote(
        f"'{col}' 선택지를 바꿨습니다."))
    return _없는주소(self)


@라우트("POST", '/fields/formula', 권한='열_구성', 거부말='표 항목 설정은 관리자만 바꿀 수 있습니다.')
def post_fields_formula(self, me, path):
    # 추가한 열의 수식. 칸이 빈 사람만 이 수식 값이 나온다 (사람이 적은 값이 이긴다).
    data = urllib.parse.parse_qs(
        self._read_body().decode("utf-8", "replace"), keep_blank_values=True
    )
    col = (data.get("col") or [""])[0]
    수식 = 수식정리((data.get("formula") or [""])[0])
    옛것 = store.field(col)
    if 옛것 is None:
        return self._redirect("/fields?err=" + urllib.parse.quote(f"없는 열입니다: {col}"))
    try:
        다른것 = {k: v for k, v in store.field_formulas().items() if k != col}
        colformula.검사(col, 수식, 모든열(), 다른것)
        store.update_field(col, 수식=수식)
    except ValueError as exc:
        return self._redirect("/fields?err=" + urllib.parse.quote(str(exc)))
    audit.record(me.아이디, "표항목", col, 항목="수식",
                 이전값=옛것.get("수식") or "", 새값=수식)
    return self._redirect("/fields?msg=" + urllib.parse.quote(
        f"'{col}' 수식을 {'바꿨습니다' if 수식 else '뺐습니다'}."))
    return _없는주소(self)


@라우트("POST", '/fields/columns', 권한='열_구성', 거부말='표 열 설정은 관리자만 바꿀 수 있습니다.')
def post_fields_columns(self, me, path):
    data = urllib.parse.parse_qs(
        self._read_body().decode("utf-8", "replace"), keep_blank_values=True
    )
    # 줄을 끌어 옮기면 폼 칸이 오는 **차례가 바뀐다.** 차례로 짝을 맞추면
    # 5번 줄의 순서 값이 1번 줄에 붙는다 (실제로 그랬다). 그래서 열 이름도
    # 번호를 달아 보내고, 번호로만 짝을 맞춘다.
    열들 = [(int(k.split("_")[1]), v[0])
          for k, v in data.items()
          if k.startswith("col_") and k.split("_")[1].isdigit() and v]
    열들.sort()
    이전 = store.column_config()
    구분맵 = {c: g for g, c, _a in 열목록()}
    바뀐것: list[str] = []
    for i, col in 열들:
        # 추가한 열은 이름과 구분(어느 표에 속하는지)까지 고칠 수 있다.
        # 기본 열은 이 칸을 아예 안 그리므로 여기 걸리지 않는다.
        옛필드 = store.field(col)
        if 옛필드 is not None:
            새이름 = (data.get(f"rename_{i}") or [col])[0].strip()
            새구분 = (data.get(f"scope_{i}")
                    or [옛필드.get("구분") or "지원자 정보"])[0]
            옛구분 = 옛필드.get("구분") or "지원자 정보"
            if 새이름 != col or 새구분 != 옛구분:
                try:
                    store.update_field(col, 새이름=새이름, 구분=새구분)
                except ValueError as exc:
                    return self._redirect(
                        "/fields?err=" + urllib.parse.quote(str(exc)))
                if 새이름 != col:
                    audit.record(me.아이디, "표항목", 새이름, 항목="열 이름",
                                 이전값=col, 새값=새이름)
                    바뀐것.append(f"{col}→{새이름}")
                if 새구분 != 옛구분:
                    audit.record(me.아이디, "표항목", 새이름, 항목="구분",
                                 이전값=옛구분, 새값=새구분)
                    바뀐것.append(f"{새이름}(구분 {새구분})")
                col = 새이름
        새라벨 = (data.get(f"label_{i}") or [""])[0].strip()
        순서값 = (data.get(f"order_{i}") or [""])[0].strip()
        숨김 = f"hide_{i}" in data
        # 켤 수 없는 열은 체크박스를 아예 안 그린다. 그런 열까지
        # "체크 없음 = 끔" 으로 읽으면 기본값이 조용히 꺼진다.
        긴글가능 = _긴글가능(col, store.field(col), 구분맵.get(col, ""))
        긴글 = (f"long_{i}" in data) if 긴글가능 else None
        # 관리 정보 열은 설정이 없으면 "숨김" 이 기본이다. 그 상태에서
        # 체크를 풀었으면 바뀐 것으로 봐야 설정이 저장된다.
        옛 = 이전.get(
            col,
            {"표시이름": "", "숨김": 기본숨김(col, 이전), "순서": 0,
             "긴글": col in store_DEFAULT_LONG},
        )
        try:
            새순서 = int(순서값) if 순서값 else 0
        except ValueError:
            새순서 = 옛["순서"]
        옛긴글 = bool(옛.get("긴글"))
        if (새라벨, 숨김, 새순서, 긴글 if 긴글 is not None else 옛긴글) == (
                옛["표시이름"], 옛["숨김"], 옛["순서"], 옛긴글):
            continue
        store.set_column(col, 표시이름=새라벨, 숨김=숨김, 순서=새순서,
                         긴글=긴글)
        조각 = []
        if 새라벨 != 옛["표시이름"]:
            조각.append(f"이름 {새라벨 or '(원래대로)'}")
            audit.record(me.아이디, "표항목", col, 항목="표에 보일 이름",
                         이전값=옛["표시이름"], 새값=새라벨)
        if 숨김 != 옛["숨김"]:
            조각.append("숨김" if 숨김 else "다시 보임")
            audit.record(me.아이디, "표항목", col, 항목="숨김",
                         이전값="Y" if 옛["숨김"] else "", 새값="Y" if 숨김 else "")
        if 새순서 != 옛["순서"]:
            조각.append(f"순서 {새순서 or '원래대로'}")
            audit.record(me.아이디, "표항목", col, 항목="순서",
                         이전값=str(옛["순서"] or ""), 새값=str(새순서 or ""))
        if 긴글 is not None and 긴글 != 옛긴글:
            조각.append("긴 글" if 긴글 else "긴 글 끔")
            audit.record(me.아이디, "표항목", col, 항목="긴 글",
                         이전값="Y" if 옛긴글 else "", 새값="Y" if 긴글 else "")
        바뀐것.append(f"{col}({', '.join(조각)})")
    if not 바뀐것:
        return self._redirect("/fields?msg=" + urllib.parse.quote("바뀐 내용이 없습니다."))
    보임 = ", ".join(바뀐것[:5]) + (" 외" if len(바뀐것) > 5 else "")
    return self._redirect("/fields?msg=" + urllib.parse.quote(
        f"{len(바뀐것)}건 저장했습니다 — {보임}"))
    return _없는주소(self)


@라우트("POST", '/fields/delete', 권한='열_구성', 거부말='표 항목 삭제는 관리자만 할 수 있습니다.')
def post_fields_delete(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    이름 = (data.get("name") or [""])[0]
    store.delete_field(이름)
    audit.record(me.아이디, "표항목", 이름, 비고="열 삭제 (값도 함께 삭제)")
    return self._redirect("/fields")
    return _없는주소(self)


@라우트("POST", '/fields', 권한='열_구성', 거부말='표 항목 추가는 관리자만 할 수 있습니다.')
def post_fields(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return self._send(_fields_page(me, (params.get("err") or [""])[0]))
    return _없는주소(self)
