"""채용 현황.
"""

from __future__ import annotations

import html
import json
import urllib.parse

from ... import normalize as N
from ...auth import can, User
from ...edit import custom_field_spec, validate_custom, ValidationError
from ...export import build_xlsx
from ...recruit import STAGES
from ...timeutil import now_kst

from ..state import audit, auth, recruit, registry, store
from ..columns import MANAGE_COLUMNS, 라벨, 머리글, 열목록, 열폭
from ..layout import _page, _알림, _없는주소, _정적JS
from ..router import 라우트, 로그인만


#: 채용 현황 화면 전용 — 부서를 바꾸면 과제 목록이 따라 바뀐다
_RECRUIT_JS = _정적JS("recruit.js")


def _recruit_rows(me: User, sort: str = ""):
    """채용 현황 표에 나갈 (레코드, 진행, 값 함수) 를 만든다.

    화면과 엑셀이 같은 데이터를 보게 하려고 한 곳에서 만든다.
    """
    보이는과제 = auth.visible_project_ids(me)      # None 이면 전부
    진행맵 = recruit.all()
    부서명 = {d["id"]: d["이름"] for d in auth.departments()}
    과제명 = {p["id"]: p["이름"] for p in auth.projects()}

    # **채용을 시작한 사람만** 채용 현황에 올라온다. 인재 Pool 에 등록만 된
    # 사람까지 여기 있으면, 지금 뽑고 있는 사람이 몇 명인지 알 수가 없다.
    시작한사람 = recruit.started()
    records = [r for r in store.list_all() if r.지원자_ID in 시작한사람]
    if 보이는과제 is not None:
        records = [
            r for r in records
            if (진행맵.get(r.지원자_ID) and 진행맵[r.지원자_ID].project_id in 보이는과제)
        ]

    사용자값맵 = store.custom_map()
    사용자열이름 = set(store.field_names())
    관리값맵 = store.meta_map()

    def 값(rec, col: str) -> str:
        p = 진행맵.get(rec.지원자_ID)
        if col in MANAGE_COLUMNS:
            return 관리값맵.get(rec.지원자_ID, {}).get(col, "")
        if col == "부서":
            return 부서명.get(p.부서_id, "") if p else ""
        if col == "과제":
            return 과제명.get(p.project_id, "") if p else ""
        if col == "최종상태":
            return p.최종상태 if p else "미시작"
        if col == "채용_비고":
            return p.채용_비고 if p else ""
        if col in STAGES:
            return (p.단계상태.get(col, "") if p else "")
        if col in 사용자열이름:
            return 사용자값맵.get(rec.지원자_ID, {}).get(col, "")
        return str(rec.to_row(registry).get(col, "") or "")

    def 정렬키(rec):
        p = 진행맵.get(rec.지원자_ID)
        기본 = p.정렬키() if p else (0, 0, 0)
        if sort:
            return (기본[0], 값(rec, sort).lower())   # 불합격은 어떤 정렬에서도 아래로
        return 기본

    records.sort(key=정렬키)
    return records, 진행맵, 값


def _recruit_page(me: User, sort: str = "", error: str = "", msg: str = "") -> bytes:
    """채용 현황 관리.

    현업은 배정된 과제의 지원자만 보인다. 기본 정렬은 불합격을 맨 아래로 내린다.

    화면 규칙:
      - **저장 버튼은 맨 위 하나뿐이다.** 여러 사람 상태를 바꾸고 한 번에 저장한다.
      - **지원자 정보 열은 여기서 못 고친다.** 채용 상태를 보는 화면이라
        지원자 정보까지 고칠 수 있으면 실수로 덮어쓰기 쉽다.
        고칠 일이 있으면 인재 Pool 이나 상세 화면에서 한다.
    """
    records, 진행맵, 값 = _recruit_rows(me, sort)
    보이는과제 = auth.visible_project_ids(me)
    depts = auth.departments()
    projects = auth.projects()

    표열 = store.arrange(recruit.columns())
    이름표 = 라벨(표열)
    고를수있는상태 = recruit.statuses()
    메일가능 = can(me, "메일_발송")
    # '채용 현황' 으로 만든 추가 열은 **여기서** 고친다. 그 열의 자리가 여기니까.
    채용사용자열 = {f["이름"]: f for f in store.fields()
                if (f.get("구분") or "지원자 정보") == "채용 현황"}
    열번호 = {c: n for n, c in enumerate(표열) if c in 채용사용자열}
    수정가능 = can(me, "채용현황_수정")
    담당자 = can(me, "지원자_수정")
    긴글열 = store.긴글열()

    부서옵션전체 = "".join(
        f"<option value='{d['id']}'>{html.escape(d['이름'])}</option>" for d in depts
    )
    과제_by_부서: dict[int, list] = {}
    for pr in projects:
        과제_by_부서.setdefault(pr["부서_id"], []).append(pr)
    # 부서를 바꾸면 과제 목록이 따라 바뀌어야 한다 (페이지를 새로 그리지 않고)
    과제표 = json.dumps(
        {str(k): [[pr["id"], pr["이름"]] for pr in v] for k, v in 과제_by_부서.items()},
        ensure_ascii=False,
    )

    rows = []
    for rec in records:
        p = 진행맵.get(rec.지원자_ID)
        cid = rec.지원자_ID
        cells = []
        for col in 표열:
            v = html.escape(값(rec, col))
            if col in STAGES and 수정가능:
                opts = "".join(
                    f"<option value='{html.escape(st)}'"
                    f"{' selected' if st == 값(rec, col) else ''}>{html.escape(st) or '-'}</option>"
                    for st in 고를수있는상태
                )
                cells.append(
                    f"<td class='ctl'><select form='recruitform'"
                    f" name='단계_{html.escape(cid)}_{html.escape(col)}'"
                    f" data-orig='{v}' onchange='markDirty(this)'>{opts}</select></td>"
                )
            elif col == "부서" and 담당자:
                현재부서 = p.부서_id if p else None
                옵션 = "".join(
                    f"<option value='{d['id']}'{' selected' if d['id'] == 현재부서 else ''}>"
                    f"{html.escape(d['이름'])}</option>" for d in depts
                )
                cells.append(
                    f"<td class='ctl'><select form='recruitform'"
                    f" name='부서_{html.escape(cid)}' data-orig='{v}'"
                    f" onchange=\"markDirty(this);syncProjects(this)\""
                    f" data-cid='{html.escape(cid)}'>"
                    f"<option value=''>-</option>{옵션}</select></td>"
                )
            elif col == "과제" and 담당자:
                현재부서 = p.부서_id if p else None
                현재과제 = p.project_id if p else None
                과제옵션 = "".join(
                    f"<option value='{pr['id']}'{' selected' if pr['id'] == 현재과제 else ''}>"
                    f"{html.escape(pr['이름'])}</option>"
                    for pr in 과제_by_부서.get(현재부서, [])
                )
                cells.append(
                    f"<td class='ctl'><select form='recruitform'"
                    f" name='과제_{html.escape(cid)}' id='proj-{html.escape(cid)}'"
                    f" data-orig='{v}' onchange='markDirty(this)'>"
                    f"<option value=''>-</option>{과제옵션}</select></td>"
                )
            elif col == "채용_비고" and 수정가능:
                칸 = (
                    f"<textarea form='recruitform' rows='2'"
                    f" name='채용비고_{html.escape(cid)}'"
                    f" style='width:180px;resize:vertical'"
                    f" data-orig='{v}' oninput='markDirty(this)'>{v}</textarea>"
                    if "채용_비고" in 긴글열 else
                    f"<input type='text' form='recruitform'"
                    f" name='채용비고_{html.escape(cid)}' value='{v}' style='width:180px'"
                    f" data-orig='{v}' oninput='markDirty(this)'>"
                )
                cells.append(f"<td class='ctl'>{칸}</td>")
            elif col in 채용사용자열 and 수정가능:
                spec = custom_field_spec(채용사용자열[col], col in 긴글열)
                이름 = f"사용자_{열번호[col]}_{html.escape(cid)}"
                if spec.입력 == "select":
                    옵션 = "".join(
                        f"<option{' selected' if o == 값(rec, col) else ''}>"
                        f"{html.escape(o)}</option>" for o in spec.선택지
                    )
                    칸 = (f"<select form='recruitform' name='{이름}' data-orig='{v}'"
                         f" onchange='markDirty(this)'>{옵션}</select>")
                elif spec.입력 == "긴글":
                    칸 = (f"<textarea form='recruitform' name='{이름}' rows='2'"
                         f" style='width:140px;resize:vertical' data-orig='{v}'"
                         f" oninput='markDirty(this)'"
                         f" title='{html.escape(spec.도움말)}'>{v}</textarea>")
                else:
                    칸 = (f"<input type='text' form='recruitform' name='{이름}'"
                         f" value='{v}' style='width:140px' data-orig='{v}'"
                         f" oninput='markDirty(this)'"
                         f" title='{html.escape(spec.도움말)}'>")
                cells.append(f"<td class='ctl'>{칸}</td>")
            elif col == "최종상태":
                cls = " class='flag'" if p and p.탈락 else ""
                cells.append(f"<td{cls}>{v}</td>")
            else:
                # 지원자 정보 열은 보기만 한다 (고치려면 인재 Pool/상세에서)
                cells.append(f"<td class='{열폭(col)}' title='{v}'>{v}</td>")
        체크 = (f"<td><input type='checkbox' form='mailform' name='ids'"
              f" value='{html.escape(cid)}'></td>" if 메일가능 else "")
        링크 = f"<td><a href='/candidate?id={urllib.parse.quote(cid)}'>상세</a></td>"
        묶음 = " class='dup'" if p and p.탈락 else ""
        rows.append(f"<tr{묶음}>{체크}{링크}{''.join(cells)}</tr>")

    체크머리 = ("<th><input type='checkbox' onclick='selectVisible(this)'"
             " title='보이는 줄만 선택합니다'></th>" if 메일가능 else "")
    머리 = 체크머리 + "<th class='w-xs'></th>" + "".join(
        f"<th class='{열폭(c)}'>{머리글(이름표[c])}</th>" for c in 표열)
    알림 = _알림(msg=msg)
    오류 = _알림(err=error)
    안내 = (
        "배정된 과제의 지원자만 보입니다."
        if 보이는과제 is not None
        else "열 제목을 눌러 정렬하고, 표 위 칸으로 걸러 봅니다. 불합격자는 항상 아래로 갑니다."
    )
    if not (수정가능 or 담당자):
        안내 += " 보기 전용입니다."
    else:
        안내 += (" 지원자 정보 열은 여기서 고칠 수 없습니다 (인재 Pool 에서 고치세요)."
              " <b>채용_비고</b>는 이번 채용에 대한 메모입니다 — 그 사람 자체에 대한"
              " 메모는 인재 Pool·상세의 <b>비고</b>에 적으세요.")
    열이름칸 = "".join(
        f"<input type='hidden' form='recruitform' name='사용자열_{n}'"
        f" value='{html.escape(c)}'>" for c, n in 열번호.items()
    )
    # 파란 띠를 두 개 쌓으면 화면이 무겁다. 띠는 하나로 두고 그 안에 단추를
    # 나란히 놓는다. 폼은 표 밖에 두고 form= 로 잇는다 (폼 중첩 금지).
    메일폼 = (
        "<form method='post' action='/mail/compose' id='mailform'>"
        "<input type='hidden' name='back' value='/recruit'></form>"
        if rows and 메일가능 else ""
    )
    저장폼 = (
        "<form method='post' action='/recruit/save' id='recruitform'>"
        + 열이름칸 + "</form>"
        if rows and (수정가능 or 담당자) else ""
    )
    단추들 = []
    if 저장폼:
        단추들.append("<button type='submit' form='recruitform'>고친 내용 저장</button>")
    if 메일폼:
        단추들.append("<button type='submit' form='mailform' class='sec'>"
                    "선택한 사람에게 메일</button>")
    저장바 = (
        메일폼 + 저장폼
        + "<div class='mergebar'>" + " ".join(단추들)
        + "<span class='muted'>"
        + ("여러 줄을 고친 뒤 <b>한 번만</b> 누르세요. 고친 칸은 노랗게 표시됩니다. "
           if 저장폼 else "")
        + ("메일은 체크한 사람에게만 갑니다 — 여기서는 <b>누가 어느 단계인지 "
           "보면서</b> 고를 수 있습니다." if 메일폼 else "")
        + "</span></div>"
        if 단추들 else ""
    )
    메일바 = ""
    열구성 = (
        "<a class='btn sec' href='/recruit/columns'>표 열 구성</a> "
        if can(me, "열_구성") else ""
    )
    빈화면 = (
        "<p class='muted'>여기에는 <b>채용을 시작한 사람만</b> 나옵니다. "
        + ("<a href='/'>인재 Pool</a> 에서 `채용 시작` 을 누르세요.</p>"
           if can(me, "지원자_목록") else "채용담당자가 시작하면 보입니다.</p>")
    )
    표 = (
        "<div class='scroll'><table data-name='채용현황'"
        " data-export='/recruit/export.xlsx'>"
        f"<tr>{머리}</tr>{''.join(rows)}</table></div>"
        if rows else 빈화면
    )
    # 표가 비어 있을 때는 빈 화면 안내가 같은 말을 하므로 두 번 쓰지 않는다.
    출처 = (
        " <a href='/'>인재 Pool</a> 에서 <b>채용 시작</b>을 누른 사람만 여기 올라옵니다."
        if rows and can(me, "지원자_목록") else ""
    )
    return _page(
        "채용 현황",
        f"""{알림}{오류}<div class='card'><h2>채용 현황 <span class='muted'>{len(records)}명</span></h2>
        <p class='muted'>{안내}{출처}</p>
        <p>{열구성}</p>
        {메일바}{저장바}{표}</div>
        <script>var 과제표 = {과제표};{_RECRUIT_JS}</script>""",
        me=me,
    )


def _recruit_columns_page(me: User) -> bytes:
    """관리자가 채용 현황 표에 보일 열과 순서를 정한다."""
    전체 = [c for _, c, _ in 열목록()]
    현재 = recruit.columns()
    항목 = "".join(
        f"<label style='display:block;padding:3px 0'>"
        f"<input type='checkbox' name='col' value='{html.escape(c)}'"
        f"{' checked' if c in 현재 else ''}> {html.escape(c)}</label>"
        for c in 전체
    )
    순서 = ", ".join(현재)
    return _page(
        "표 열 구성",
        f"""<div class='card'><h2>채용 현황 표에 보일 열</h2>
        <p class='muted'>체크한 열만 보입니다. 순서는 아래 칸에 쉼표로 적은 순서를 따릅니다.</p>
        <form method='post' action='/recruit/columns'>
          <div style='columns:3'>{항목}</div>
          <p>순서(쉼표 구분, 비우면 체크 순서대로):<br>
          <input type='text' name='order' value='{html.escape(순서)}' style='width:100%'></p>
          <button type='submit'>저장</button>
          <a class='btn sec' href='/recruit'>취소</a>
        </form></div>""",
        me=me,
    )


@라우트("GET", '/recruit', 권한=로그인만)
def get_recruit(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return self._send(
        _recruit_page(me, (params.get("sort") or [""])[0],
                      (params.get("err") or [""])[0],
                      (params.get("msg") or [""])[0])
    )
    return _없는주소(self)


@라우트("GET", '/recruit/export.xlsx', 권한=('채용현황_수정', '지원자_조회'))
def get_recruit_export_xlsx(self, me, path):
    if not (can(me, "채용현황_수정") or can(me, "지원자_조회")):
        return self._deny()
    # 이름을 표열 로 두면 이 함수 안에서 모듈 함수 표열() 을 가린다
    # (파이썬은 함수 어디서든 대입이 있으면 그 이름을 지역으로 본다).
    채용열 = store.arrange(recruit.columns())
    이름표 = 라벨(채용열)
    records, _진행, 값 = _recruit_rows(me, (urllib.parse.parse_qs(
        urllib.parse.urlparse(self.path).query).get("sort") or [""])[0])
    rows = [{이름표[c]: 값(rec, c) for c in 채용열} for rec in records]
    stamp = now_kst().strftime("%Y%m%d_%H%M")
    return self._send(
        build_xlsx(rows, [이름표[c] for c in 채용열]),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        extra={"Content-Disposition": f'attachment; filename="recruit_{stamp}.xlsx"'},
    )
    return _없는주소(self)


@라우트("GET", '/recruit/columns', 권한='열_구성', 거부말='표 열 구성은 관리자만 바꿀 수 있습니다.')
def get_recruit_columns(self, me, path):
    return self._send(_recruit_columns_page(me))
    return _없는주소(self)


@라우트("POST", '/recruit/save', 권한=('채용현황_수정', '지원자_수정'))
def post_recruit_save(self, me, path):
    # 표 전체가 한 번에 온다. 실제로 값이 달라진 것만 저장한다.
    data = urllib.parse.parse_qs(
        self._read_body().decode("utf-8", "replace"), keep_blank_values=True
    )
    보이는 = auth.visible_project_ids(me)
    바뀐것: list[str] = []
    긴글열 = store.긴글열()
    이름맵 = {r.지원자_ID: (r.한글_이름 or r.영문_이름 or r.지원자_ID)
             for r in store.list_all()}

    def 볼수있나(cid: str) -> bool:
        return 보이는 is None or recruit.get(cid).project_id in 보이는

    # 1) 단계 상태
    if can(me, "채용현황_수정"):
        for key, 값들 in data.items():
            if not key.startswith("단계_"):
                continue
            몸통 = key[len("단계_"):]
            for 단계 in STAGES:
                if 몸통.endswith("_" + 단계):
                    cid = 몸통[: -(len(단계) + 1)]
                    break
            else:
                continue
            if not 볼수있나(cid):
                continue
            try:
                이전 = recruit.set_stage(cid, 단계, 값들[0], me.아이디)
            except ValueError as exc:
                return self._redirect("/recruit?err=" + urllib.parse.quote(str(exc)))
            if 이전 != 값들[0]:
                audit.record(me.아이디, "채용현황", cid, 항목=단계,
                             이전값=이전, 새값=값들[0])
                바뀐것.append(f"{이름맵.get(cid, cid)} {단계} {값들[0] or '(빈칸)'}")

        # 2) 채용 비고 (지원자 쪽 '비고' 와 다른 값이다)
        for key, 값들 in data.items():
            if not key.startswith("채용비고_"):
                continue
            cid = key[len("채용비고_"):]
            if not 볼수있나(cid):
                continue
            새비고 = (N.paragraph(값들[0]) if "채용_비고" in 긴글열
                    else N.text(값들[0]))
            이전 = recruit.set_note(cid, 새비고, me.아이디)
            if 이전 != 새비고:
                audit.record(me.아이디, "채용현황", cid, 항목="채용_비고",
                             이전값=이전, 새값=새비고)
                바뀐것.append(f"{이름맵.get(cid, cid)} 채용_비고")

        # 3) '채용 현황' 으로 만든 추가 열
        for 키, 열이름들 in data.items():
            if not 키.startswith("사용자열_"):
                continue
            n = 키[len("사용자열_"):]
            열이름 = 열이름들[0]
            정의 = store.field(열이름)
            if 정의 is None or (정의.get("구분") or "지원자 정보") != "채용 현황":
                continue
            앞머리 = f"사용자_{n}_"
            for k2, 값들2 in data.items():
                if not k2.startswith(앞머리):
                    continue
                cid = k2[len(앞머리):]
                if not 볼수있나(cid):
                    continue
                try:
                    새값 = validate_custom(정의, 값들2[0], 열이름 in 긴글열)
                except ValidationError as exc:
                    return self._redirect(
                        "/recruit?err=" + urllib.parse.quote(str(exc)))
                이전 = store.set_custom(cid, 열이름, 새값)
                if 이전 != 새값:
                    audit.record(me.아이디, "채용현황", cid, 항목=열이름,
                                 이전값=이전, 새값=새값)
                    바뀐것.append(f"{이름맵.get(cid, cid)} {열이름}")

    # 4) 부서 / 과제 (지원자 수정 권한이 있어야 배정할 수 있다)
    if can(me, "지원자_수정"):
        for key, 값들 in data.items():
            if not key.startswith("부서_"):
                continue
            cid = key[len("부서_"):]
            dept = 값들[0]
            proj = (data.get(f"과제_{cid}") or [""])[0]
            부서_id = int(dept) if dept.isdigit() else None
            project_id = int(proj) if proj.isdigit() else None
            # 부서를 바꾸면 그 부서에 속하지 않는 과제는 떨어뜨린다
            if project_id is not None:
                소속 = {pr["id"] for pr in auth.projects(부서_id)} if 부서_id else set()
                if project_id not in 소속:
                    project_id = None
            옛부서, 옛과제 = recruit.set_assignment(cid, 부서_id, project_id, me.아이디)
            if (옛부서, 옛과제) != (부서_id, project_id):
                audit.record(me.아이디, "채용현황", cid, 항목="부서/과제",
                             이전값=f"{옛부서}/{옛과제}", 새값=f"{부서_id}/{project_id}")
                바뀐것.append(f"{이름맵.get(cid, cid)} 부서/과제")

    if not 바뀐것:
        return self._redirect("/recruit?msg=" + urllib.parse.quote("바뀐 내용이 없습니다."))
    보임 = ", ".join(바뀐것[:5]) + (" 외" if len(바뀐것) > 5 else "")
    return self._redirect("/recruit?msg=" + urllib.parse.quote(
        f"{len(바뀐것)}건 저장했습니다 — {보임}"))
    return _없는주소(self)


@라우트("POST", '/recruit/statuses', 권한='열_구성', 거부말='표 항목 설정은 관리자만 바꿀 수 있습니다.')
def post_recruit_statuses(self, me, path):
    # 단계에서 고를 수 있는 상태 목록. 네 단계가 같은 목록을 쓴다.
    data = urllib.parse.parse_qs(
        self._read_body().decode("utf-8", "replace"), keep_blank_values=True
    )
    목록 = [v.strip() for v in (data.get("choices") or [""])[0].split("|")]
    try:
        이전 = recruit.set_statuses(목록)
    except ValueError as exc:
        return self._redirect("/fields?err=" + urllib.parse.quote(str(exc)))
    지금 = recruit.statuses()
    if 지금 != 이전:
        audit.record(me.아이디, "표항목", "단계 상태", 항목="선택지",
                     이전값=" | ".join(x for x in 이전 if x),
                     새값=" | ".join(x for x in 지금 if x))
    return self._redirect("/fields?msg=" + urllib.parse.quote(
        "단계 상태 목록을 바꿨습니다: "
        + ", ".join(x or "(빈칸)" for x in 지금)))
    return _없는주소(self)


@라우트("POST", '/recruit/columns', 권한='열_구성', 거부말='표 열 구성은 관리자만 바꿀 수 있습니다.')
def post_recruit_columns(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    고른것 = data.get("col") or []
    순서문 = (data.get("order") or [""])[0]
    if 순서문.strip():
        원하는 = [c.strip() for c in 순서문.split(",") if c.strip()]
        최종 = [c for c in 원하는 if c in 고른것] + [c for c in 고른것 if c not in 원하는]
    else:
        최종 = 고른것
    recruit.set_columns(최종)
    audit.record(me.아이디, "채용현황", "(표 열)", 비고=f"열 구성 변경: {', '.join(최종)}")
    return self._redirect("/recruit")
    return _없는주소(self)
