"""계정 · 부서·과제 · 변경 이력.
"""

from __future__ import annotations

import html
import urllib.parse

from ...auth import can, ROLES, User

from ..state import audit, auth, recruit
from ..jobs import 과제목록

from ..layout import _page, _알림, _없는주소
from ..router import 라우트


def _org_hub_page(me: User) -> bytes:
    """부서·과제 탭의 첫 화면. 무엇을 할지 고른다."""
    목록, _오류 = 과제목록()
    return _page(
        "부서·과제",
        "<div class='card'><h2>부서·과제</h2>"
        "<p class='muted'>둘 중 무엇을 할지 고르세요.</p></div>"
        "<div class='card'><h2><a href='/org/edit'>부서·과제 편집</a></h2>"
        f"<p class='muted'>부서 {len(auth.departments())}개 · "
        f"과제 {len(auth.projects())}개 — 이름을 고치고, 새로 만들고, "
        "과제 초대암호를 겁니다. 현업 계정이 배정되는 그 과제입니다.</p>"
        "<p><a class='btn' href='/org/edit'>열기</a></p></div>"
        "<div class='card'><h2><a href='/match'>과제 정보 관리</a></h2>"
        f"<p class='muted'>연구 과제 파일 {len(목록)}개 — 매칭에 쓸 과제 파일을 "
        "확인하고 다듬습니다. 지원자와 맞춰보는 것도 여기서 돌립니다.</p>"
        "<p><a class='btn' href='/match'>열기</a></p></div>",
        me=me,
    )


def _users_page(me: User, error: str = "") -> bytes:
    """계정 관리. 관리자는 전원, 채용담당자는 현업만 추가할 수 있다."""
    users = auth.list_users()
    projects = auth.projects()
    추가가능 = ROLES if me.is_admin else ("현업",)

    rows = []
    for u in users:
        배정 = auth.project_ids_of(u.아이디) if u.역할 == "현업" else set()
        과제표시 = ", ".join(
            f"{p['부서명']}/{p['이름']}" for p in projects if p["id"] in 배정
        ) or ("-" if u.역할 == "현업" else "")
        수정가능 = me.is_admin or u.역할 == "현업"
        조작 = ""
        if 수정가능 and u.아이디 != me.아이디:
            라벨 = "비활성화" if u.활성 else "활성화"
            조작 = (
                "<form method='post' action='/users/toggle' style='display:inline'>"
                f"<input type='hidden' name='id' value='{html.escape(u.아이디)}'>"
                f"<button class='sec'>{라벨}</button></form> "
                "<form method='post' action='/users/delete' style='display:inline'"
                " onsubmit=\"return confirm('계정을 삭제합니다.')\">"
                f"<input type='hidden' name='id' value='{html.escape(u.아이디)}'>"
                "<button class='danger'>삭제</button></form>"
            )
        상태 = "활성" if u.활성 else "<span class='flag'>비활성</span>"
        rows.append(
            f"<tr><td>{html.escape(u.아이디)}</td><td>{html.escape(u.이름)}</td>"
            f"<td>{u.역할}</td><td>{상태}</td><td>{html.escape(과제표시)}</td>"
            f"<td class='muted'>{html.escape(u.생성일시)} ({html.escape(u.생성자 or '-')})</td>"
            f"<td>{조작}</td></tr>"
        )

    역할옵션 = "".join(f"<option>{r}</option>" for r in 추가가능)
    과제옵션 = "".join(
        f"<option value='{p['id']}'>{html.escape(p['부서명'])} / {html.escape(p['이름'])}</option>"
        for p in projects
    )
    오류 = _알림(err=error)
    안내 = (
        "관리자는 모든 역할을 만들 수 있습니다."
        if me.is_admin
        else "채용담당자는 <b>현업 계정만</b> 만들 수 있습니다."
    )
    return _page(
        "계정 관리",
        "<div class='card'><h2>계정 추가</h2>" + 오류
        + "<form method='post' action='/users/add' style='display:flex;gap:8px;flex-wrap:wrap'>"
        "<input type='text' name='userid' placeholder='아이디' required>"
        "<input type='text' name='name' placeholder='이름'>"
        "<input type='password' name='password' placeholder='비밀번호(4자 이상)' required>"
        f"<select name='role'>{역할옵션}</select>"
        f"<select name='project'><option value=''>과제 배정(현업만)</option>{과제옵션}</select>"
        "<button type='submit'>추가</button></form>"
        f"<p class='muted'>{안내} 현업은 배정된 과제의 지원자만 볼 수 있습니다.</p></div>"
        f"<div class='card'><h2>계정 {len(users)}개</h2><div class='scroll'>"
        "<table><tr><th>아이디</th><th>이름</th><th>역할</th><th>상태</th>"
        "<th>배정 과제</th><th>생성</th><th></th></tr>"
        + "".join(rows) + "</table></div></div>",
        me=me,
    )


def _org_page(me: User, error: str = "") -> bytes:
    """부서 · 과제 편집. 과제는 부서에 속한다.

    예전에는 과제마다 입력칸과 단추가 글머리표 목록으로 늘어서서, 단추가
    줄바꿈되고 무엇이 무엇에 딸린 것인지 알기 어려웠다. 부서 하나를 카드
    하나로 두고, 그 안은 **표**로 정리했다.
    """
    depts = auth.departments()
    projects = auth.projects()

    # 배정된 지원자 수. 지우기 전에 무엇이 딸려 있는지 보이면 실수가 준다.
    배정수: dict[int, int] = {}
    for p in recruit.all().values():
        if p.project_id:
            배정수[p.project_id] = 배정수.get(p.project_id, 0) + 1

    카드 = []
    for d in depts:
        소속 = [p for p in projects if p["부서_id"] == d["id"]]
        사람 = sum(배정수.get(p["id"], 0) for p in 소속)
        줄 = "".join(
            "<tr>"
            f"<td class='ctl'><input type='text' name='name' form='pf{p['id']}'"
            f" value='{html.escape(p['이름'])}' style='width:240px'></td>"
            f"<td class='ctl'><input type='password' name='invite' form='pf{p['id']}'"
            " placeholder='바꿀 때만 입력' style='width:170px'"
            " autocomplete='new-password'>"
            + ("<br><span class='muted'>지금 걸려 있음</span>" if p["초대암호"]
               else "<br><span class='muted'>없음</span>")
            + "</td>"
            f"<td class='w-sm'>{배정수.get(p['id'], 0)}명</td>"
            "<td class='ctl' style='white-space:nowrap'>"
            f"<form method='post' action='/org/project/rename' id='pf{p['id']}'"
            " style='display:inline'>"
            f"<input type='hidden' name='id' value='{p['id']}'>"
            "<button type='submit'>저장</button></form> "
            "<form method='post' action='/org/project/delete' style='display:inline'"
            f" onsubmit=\"return confirm('과제 \\'{html.escape(p['이름'])}\\' 를 "
            f"삭제합니다. 배정된 지원자 {배정수.get(p['id'], 0)}명의 배정도 함께 "
            "풀립니다.')\">"
            f"<input type='hidden' name='id' value='{p['id']}'>"
            "<button class='danger'>삭제</button></form></td></tr>"
            for p in 소속
        ) or ("<tr><td colspan='4' class='muted'>아직 과제가 없습니다. "
              "아래에서 추가하세요.</td></tr>")

        카드.append(
            "<div class='card'>"
            # 폼 안에 폼을 넣으면 브라우저가 안쪽을 버린다. 나란히 둔다.
            "<div style='display:flex;gap:8px;align-items:center;flex-wrap:wrap;"
            "margin-bottom:10px'>"
            "<form method='post' action='/org/dept/rename'"
            " style='display:flex;gap:6px;align-items:center'>"
            f"<input type='hidden' name='id' value='{d['id']}'>"
            f"<input type='text' name='name' value='{html.escape(d['이름'])}'"
            " style='width:220px;font-weight:700'>"
            "<button type='submit'>부서명 저장</button></form>"
            "<form method='post' action='/org/dept/delete' style='display:inline'"
            f" onsubmit=\"return confirm('부서 \\'{html.escape(d['이름'])}\\' 와 "
            f"그 아래 과제 {len(소속)}개를 삭제합니다. 배정도 함께 풀립니다.')\">"
            f"<input type='hidden' name='id' value='{d['id']}'>"
            "<button class='danger'>부서 삭제</button></form>"
            f"<span class='muted'>과제 {len(소속)}개 · 배정된 지원자 {사람}명</span>"
            "</div>"
            "<table style='width:auto'><tr><th>과제 이름</th>"
            "<th>초대암호</th><th class='w-sm'>배정</th>"
            "<th></th></tr>" + 줄 + "</table>"
            "<form method='post' action='/org/project/add'"
            " style='display:flex;gap:8px;margin-top:10px;flex-wrap:wrap'>"
            f"<input type='hidden' name='dept' value='{d['id']}'>"
            "<input type='text' name='name' placeholder='새 과제 이름' required"
            " style='width:240px'>"
            "<input type='password' name='invite' placeholder='초대암호 (선택)'"
            " autocomplete='new-password' style='width:200px'>"
            "<button type='submit'>과제 추가</button></form>"
            "</div>"
        )

    오류 = _알림(err=error)
    본문 = (
        "<div class='card'><h2>부서 추가 "
        "<span class='muted'>과제는 부서에 속합니다</span></h2>" + 오류
        + "<form method='post' action='/org/dept/add'"
        " style='display:flex;gap:8px;flex-wrap:wrap'>"
        "<input type='text' name='name' placeholder='부서 이름' required"
        " style='width:240px'>"
        "<button type='submit'>추가</button>"
        "<a class='btn sec' href='/org'>부서·과제로</a></form>"
        "<p class='muted'>현업 계정은 <b>과제</b>에 배정됩니다. 초대암호를 걸면 "
        "그 암호를 아는 사람만 그 과제로 계정을 만들 수 있습니다.</p></div>"
        + ("".join(카드) or "<div class='card muted'>부서를 먼저 추가하세요.</div>")
    )
    return _page("부서·과제 편집", 본문, me=me)


def _history_page(me: User, 대상종류: str = "", limit: int = 300) -> bytes:
    entries = audit.recent(limit, 대상종류=대상종류)
    rows = "".join(
        f"<tr><td>{html.escape(e.일시)}</td><td>{html.escape(e.사용자)}</td>"
        f"<td>{html.escape(e.대상종류)}</td><td>{html.escape(e.대상)}</td>"
        f"<td title='{html.escape(e.summary())}'>{html.escape(e.summary())}</td></tr>"
        for e in entries
    ) or "<tr><td colspan='5' class='muted'>이력이 없습니다.</td></tr>"
    탭 = " ".join(
        f"<a class='btn {'' if k == 대상종류 else 'sec'}'"
        f" href='/history?kind={urllib.parse.quote(k)}'>{k or '전체'}</a>"
        for k in ("", "지원자", "채용현황", "계정", "명칭", "과제", "로그인")
    )
    return _page(
        "변경 이력",
        f"<div class='card'><h2>변경 이력 <span class='muted'>총 {audit.count()}건</span></h2>"
        f"<p>{탭}</p><div class='scroll'><table>"
        "<tr><th>일시</th><th>사용자</th><th>종류</th><th>대상</th><th>내용</th></tr>"
        f"{rows}</table></div></div>",
        me=me,
    )


@라우트("GET", '/users', 권한='계정_현업추가')
def get_users(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return self._send(_users_page(me, (params.get("err") or [""])[0]))
    return _없는주소(self)


@라우트("GET", '/org', 권한='부서과제_관리')
def get_org(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return self._send(_org_hub_page(me))
    return _없는주소(self)


@라우트("GET", '/org/edit', 권한='부서과제_관리')
def get_org_edit(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return self._send(_org_page(me, (params.get("err") or [""])[0]))
    return _없는주소(self)


@라우트("GET", '/history', 권한='변경이력_조회')
def get_history(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return self._send(_history_page(me, (params.get("kind") or [""])[0]))
    return _없는주소(self)


@라우트("POST", '/org/dept/rename', 권한='부서과제_관리')
def post_org_dept_rename(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    새이름 = (data.get("name") or [""])[0]
    try:
        옛이름 = auth.rename_department(int((data.get("id") or ["0"])[0]), 새이름)
    except (ValueError, TypeError) as exc:
        return self._redirect("/org/edit?err=" + urllib.parse.quote(str(exc)))
    if 옛이름 != 새이름:
        audit.record(me.아이디, "과제", 새이름, 항목="부서명",
                     이전값=옛이름, 새값=새이름)
    return self._redirect("/org/edit")
    return _없는주소(self)


@라우트("POST", '/org/dept/delete', 권한='부서과제_관리')
def post_org_dept_delete(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    try:
        auth.delete_department(int((data.get("id") or ["0"])[0]))
    except (ValueError, TypeError):
        pass
    return self._redirect("/org/edit")
    return _없는주소(self)


@라우트("POST", '/org/project/rename', 권한='부서과제_관리')
def post_org_project_rename(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    새이름 = (data.get("name") or [""])[0]
    암호 = (data.get("invite") or [""])[0]
    try:
        pid = int((data.get("id") or ["0"])[0])
        옛이름 = auth.rename_project(pid, 새이름)
    except (ValueError, TypeError) as exc:
        return self._redirect("/org/edit?err=" + urllib.parse.quote(str(exc)))
    if 암호.strip():          # 비우면 기존 암호를 그대로 둔다
        auth.set_project_password(pid, 암호)
        audit.record(me.아이디, "과제", 새이름, 비고="초대암호 변경")
    if 옛이름 != 새이름:
        audit.record(me.아이디, "과제", 새이름, 항목="과제명",
                     이전값=옛이름, 새값=새이름)
    return self._redirect("/org/edit")
    return _없는주소(self)


@라우트("POST", '/users/add', 권한='계정_현업추가')
def post_users_add(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    역할 = (data.get("role") or ["현업"])[0]
    if 역할 != "현업" and not can(me, "계정_전체관리"):
        return self._redirect("/users?err=" + urllib.parse.quote(
            "채용담당자는 현업 계정만 만들 수 있습니다."))
    try:
        u = auth.create_user(
            (data.get("userid") or [""])[0],
            (data.get("name") or [""])[0],
            (data.get("password") or [""])[0],
            역할,
            생성자=me.아이디,
        )
    except ValueError as exc:
        return self._redirect("/users?err=" + urllib.parse.quote(str(exc)))
    과제 = (data.get("project") or [""])[0]
    if 과제 and u.역할 == "현업":
        auth.assign(u.아이디, int(과제))
    audit.record(me.아이디, "계정", u.아이디, 비고=f"{u.역할} 계정 생성")
    return self._redirect("/users")
    return _없는주소(self)


# 현업은 계정을 못 다룬다. 이 선언이 없던 동안에는 아래 «현업 계정이면 된다»
# 검사만 있어서, 현업이 다른 현업 계정을 끄거나 지울 수 있었다.
@라우트("POST", '/users/toggle', 권한='계정_현업추가')
def post_users_toggle(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    대상 = auth.get_user((data.get("id") or [""])[0])
    if 대상 is None or 대상.아이디 == me.아이디:
        return self._redirect("/users")
    if not (can(me, "계정_전체관리") or 대상.역할 == "현업"):
        return self._deny()
    auth.set_active(대상.아이디, not 대상.활성)
    if 대상.활성:
        auth.end_all_sessions(대상.아이디)
    audit.record(me.아이디, "계정", 대상.아이디,
                 비고="비활성화" if 대상.활성 else "활성화")
    return self._redirect("/users")
    return _없는주소(self)


# 현업은 계정을 못 다룬다. 이 선언이 없던 동안에는 아래 «현업 계정이면 된다»
# 검사만 있어서, 현업이 다른 현업 계정을 끄거나 지울 수 있었다.
@라우트("POST", '/users/delete', 권한='계정_현업추가')
def post_users_delete(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    대상 = auth.get_user((data.get("id") or [""])[0])
    if 대상 is None or 대상.아이디 == me.아이디:
        return self._redirect("/users")
    if not (can(me, "계정_전체관리") or 대상.역할 == "현업"):
        return self._deny()
    auth.delete_user(대상.아이디)
    audit.record(me.아이디, "계정", 대상.아이디, 비고="계정 삭제")
    return self._redirect("/users")
    return _없는주소(self)


@라우트("POST", '/org/dept/add', 권한='부서과제_관리')
def post_org_dept_add(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    try:
        auth.add_department((data.get("name") or [""])[0])
    except ValueError as exc:
        return self._redirect("/org/edit?err=" + urllib.parse.quote(str(exc)))
    audit.record(me.아이디, "과제", (data.get("name") or [""])[0], 비고="부서 추가")
    return self._redirect("/org/edit")
    return _없는주소(self)


@라우트("POST", '/org/project/add', 권한='부서과제_관리')
def post_org_project_add(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    try:
        auth.add_project(
            int((data.get("dept") or ["0"])[0]),
            (data.get("name") or [""])[0],
            (data.get("invite") or [""])[0],
        )
    except (ValueError, TypeError) as exc:
        return self._redirect("/org/edit?err=" + urllib.parse.quote(str(exc)))
    audit.record(me.아이디, "과제", (data.get("name") or [""])[0], 비고="과제 추가")
    return self._redirect("/org/edit")
    return _없는주소(self)


@라우트("POST", '/org/project/delete', 권한='부서과제_관리')
def post_org_project_delete(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    try:
        auth.delete_project(int((data.get("id") or ["0"])[0]))
    except (ValueError, TypeError):
        pass
    return self._redirect("/org/edit")
    return _없는주소(self)
