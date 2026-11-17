"""과제 매칭 · 과제 파일 다듬기.
"""

from __future__ import annotations

import html
import json
import urllib.parse

from ... import projects as projectsmod
from ...auth import can, User
from ...config import settings
from ...matching import GRADE_RUBRIC, 점수등급

from ..state import audit, store
from ..jobs import 가져온과제파일, 과제목록, 다듬은파일, 매칭실행, 쓰는과제파일, 원본과제경로
from ..multipart import parse_multipart
from ..layout import _page, _알림, _없는주소
from ..router import 라우트


def _등급이름(m: dict) -> str:
    """저장된 값으로 등급 이름을 되살린다 (Match.등급 과 같은 기준)."""
    return 점수등급(m.get("점수") or 0, m.get("평가됨", True))


def _등급칸(m: dict) -> str:
    """등급 하나를 색 딱지로. **숫자는 보이지 않는다** — 같은 등급 안의 몇 점 차이는
    모델의 감이라, 보이면 사람이 그 차이를 믿는다."""
    if not m.get("평가됨", True):
        return "<span class='pill p-실패'>미평가</span>"
    return (f"<span class='pill {_점수색(m.get('점수') or 0)}'>"
            f"{html.escape(_등급이름(m))}</span>")


def _점수색(점수: int) -> str:
    if 점수 >= 90:
        return "p-완료"
    if 점수 >= 70:
        return "p-처리중"
    if 점수 >= 50:
        return "p-검토필요"
    return "p-대기중"


def _curate_page(me: User, error: str = "", msg: str = "") -> bytes:
    """원본 과제 파일을 읽고 **어떤 과제·어떤 정보만 남길지** 고른다.

    원본에는 매칭에 쓸모없는 항목이 많다. 그대로 LLM 에 밀어 넣으면 프롬프트만
    길어지고 판단이 흐려진다. 사람이 한 번 골라 다듬은 파일을 만들고, 매칭은
    그 파일을 쓴다. **원본은 건드리지 않는다.**
    """
    원본경로 = 원본과제경로()
    다듬 = 다듬은파일()
    메타 = projectsmod.curated_meta(다듬)

    try:
        data = projectsmod.read_json(원본경로)
        항목 = projectsmod.raw_items(data)
        필드 = projectsmod.field_stats(항목)
        읽기오류 = ""
    except projectsmod.ProjectsError as exc:
        항목, 필드, 읽기오류 = [], [], str(exc)

    # 지금 다듬은 파일에 들어 있는 것을 미리 체크해 둔다
    고른필드 = set(메타.get("필드") or [])
    고른과제: set[str] = set()
    if 메타:
        try:
            고른과제 = {p.키 for p in projectsmod.load(다듬)}
            고른과제 |= {p.이름 for p in projectsmod.load(다듬)}
        except projectsmod.ProjectsError:
            고른과제 = set()
    처음 = not 메타

    필드줄 = "".join(
        f"<tr><td><label><input type='checkbox' form='curform' name='fields'"
        f" value='{html.escape(f.이름)}'"
        + (" checked disabled" if f.필수 else
           (" checked" if (처음 or f.이름 in 고른필드) else ""))
        + f"> <b>{html.escape(f.라벨)}</b></label>"
        + ("<br><span class='muted'>과제 이름이라 항상 남습니다</span>" if f.필수 else "")
        + f"</td><td class='muted'>{html.escape(f.이름)}</td>"
        f"<td>{f.채운수}/{f.전체수} <span class='muted'>({f.비율}%)</span></td>"
        f"<td class='muted' title='{html.escape(f.예시)}'>"
        f"{html.escape(f.예시)}</td></tr>"
        for f in 필드
    ) or "<tr><td colspan='4' class='muted'>읽은 필드가 없습니다.</td></tr>"

    과제줄 = []
    for 기본키, 원본 in 항목:
        p = projectsmod.to_project(원본, 기본키)
        if p is None:
            continue
        키 = projectsmod.item_key(기본키, 원본)
        체크 = " checked" if (처음 or 키 in 고른과제 or p.이름 in 고른과제) else ""
        과제줄.append(
            f"<tr><td><input type='checkbox' form='curform' name='keys'"
            f" value='{html.escape(키)}'{체크}></td>"
            f"<td>{html.escape(p.담당)}</td>"
            f"<td><b>{html.escape(p.이름)}</b>"
            f"<br><span class='muted'>{html.escape(키)}</span></td>"
            f"<td>{html.escape(', '.join(p.키워드[:8]))}</td>"
            f"<td class='muted' title='{html.escape(p.설명[:400])}'>"
        f"{html.escape(p.설명[:150])}"
            f"{'…' if len(p.설명) > 150 else ''}</td></tr>"
        )
    과제표 = "".join(과제줄) or \
        "<tr><td colspan='5' class='muted'>읽은 과제가 없습니다.</td></tr>"

    현황 = (
        f"<table><tr><th style='width:150px'>원본 파일</th>"
        f"<td><code>{html.escape(str(projectsmod.resolve_path(원본경로) or '(설정 안 됨)'))}"
        f"</code> <span class='muted'>{'엑셀로 가져온 과제' if 가져온과제파일().is_file() else '.env 의 CVTOOL_PROJECTS_JSON'}</span></td></tr>"
        f"<tr><th>원본 과제</th><td>{len(항목)}개 · 필드 {len(필드)}종</td></tr>"
        f"<tr><th>다듬은 파일</th><td><code>{html.escape(str(다듬))}</code></td></tr>"
        + (f"<tr><th>지금 쓰는 것</th><td><b>다듬은 파일</b> — 과제 {메타['과제수']}개 · "
           f"필드 {len(메타['필드'])}종 · {html.escape(메타['만든일시'])}"
           + (f" ({html.escape(메타['만든이'])})" if 메타.get("만든이") else "")
           + "</td></tr>"
           if 메타 else
           "<tr><th>지금 쓰는 것</th><td><b>원본 파일</b> — 아직 다듬지 않았습니다</td></tr>")
        + "</table>"
    )

    오류 = (_알림(err=error)
          + (f"<div class='warn'>{html.escape(읽기오류)}</div>" if 읽기오류 else ""))
    알림 = _알림(msg=msg)
    저장바 = (
        "<form method='post' action='/match/curate' id='curform' class='mergebar'>"
        "<button type='submit'>고른 것만 남겨 저장</button>"
        "<span class='muted'>원본은 그대로 두고 <b>다듬은 파일</b>을 새로 씁니다. "
        "저장하면 매칭은 이 파일을 씁니다.</span></form>"
        if 항목 else ""
    )
    지우기 = (
        "<form method='post' action='/match/curate/reset' style='margin-top:10px'"
        " onsubmit=\"return confirm('다듬은 파일을 지웁니다. 매칭은 다시 원본을 씁니다.')\">"
        "<button class='danger'>다듬은 파일 지우기</button>"
        "<span class='muted'> 원본 파일은 지워지지 않습니다.</span></form>"
        if 메타 else ""
    )
    return _page(
        "과제 파일 다듬기",
        알림 + 오류
        + "<div class='card'><h2>과제 파일</h2>" + 현황
        + "<p class='muted'>원본에 매칭과 상관없는 항목이 많으면 여기서 걸러내세요. "
        "프롬프트가 짧아지고 판단이 또렷해집니다.</p>"
        + f"<p><a class='btn sec' href='/match'>과제 매칭으로</a></p>{지우기}</div>"
        + 저장바
        + f"<div class='card'><h2>1. 남길 정보 고르기 <span class='muted'>필드 {len(필드)}종"
        "</span></h2>"
        "<p class='muted'>채움 비율이 낮거나(작성자·문서버전 같은) 매칭과 상관없는 "
        "필드는 빼세요.</p><div class='scroll'><table data-name='과제 필드'>"
        "<tr><th>남길까</th><th>원본 필드명</th><th>채움</th><th>예시</th></tr>"
        + 필드줄 + "</table></div></div>"
        + f"<div class='card'><h2>2. 남길 과제 고르기 <span class='muted'>"
        f"{len(과제줄)}개</span></h2>"
        "<div class='scroll'><table data-name='과제 고르기'>"
        "<tr><th style='width:34px'><input type='checkbox' title='전체 선택'"
        " onclick=\"for(const c of this.closest('table')"
        ".querySelectorAll('input[name=keys]'))"
        "if(!c.closest('tr').classList.contains('hide'))c.checked=this.checked\"></th>"
        "<th>부서</th><th>과제명</th><th>키워드</th><th>내용</th></tr>"
        + 과제표 + "</table></div></div>",
        me=me,
    )


def _projects_page(me: User, error: str = "", msg: str = "") -> bytes:
    """과제 정보 관리 — 어떤 과제 파일을 읽고 있고 무엇이 들어 있는지.

    예전에는 `과제 매칭` 탭에서 이 화면과 **지원자별 1순위 표**를 같이 보여줬다.
    지원자별 매칭은 어차피 지원자 상세에서 보므로 표는 뺐다. 여기 남는 것은
    과제 쪽 관리뿐이라 `부서·과제` 아래로 들어왔다.
    """
    목록, 파일오류 = 과제목록()
    경로 = projectsmod.resolve_path(쓰는과제파일()[0])
    _쓰는것, 다듬음 = 쓰는과제파일()
    설정 = (
        "<table><tr><th style='width:150px'>과제 파일</th>"
        f"<td><code>{html.escape(str(경로) if 경로 else '(설정 안 됨)')}</code>"
        + (" <span class='pill p-완료'>다듬은 파일</span>" if 다듬음
           else " <span class='pill p-대기중'>원본</span>")
        + "</td></tr>"
        f"<tr><th>읽은 과제</th><td>{len(목록)}개</td></tr>"
        f"<tr><th>맞춰본 지원자</th><td>{store.matched_count()}명 "
        f"/ 전체 {store.count()}명</td></tr>"
        f"<tr><th>자동 매칭</th><td>{'켜짐' if settings.match_auto else '꺼짐'} "
        "<span class='muted'>(운영 설정)</span></td></tr>"
        f"<tr><th>비교 방식</th><td>과제 <b>전부</b>와 비교 · "
        f"한 번에 {settings.match_batch}개씩 물어봄 "
        "<span class='muted'>(CVTOOL_MATCH_BATCH)</span></td></tr></table>"
    )
    과제줄 = "".join(
        f"<tr><td>{html.escape(p.번호 or p.키)}</td><td><b>{html.escape(p.이름)}</b></td>"
        f"<td>{html.escape(', '.join(p.키워드))}</td>"
        f"<td>{html.escape(p.담당)}</td>"
        f"<td class='muted' title='{html.escape(p.설명[:400])}'>"
        f"{html.escape(p.설명[:160])}"
        f"{'…' if len(p.설명) > 160 else ''}</td></tr>"
        for p in 목록
    ) or "<tr><td colspan='5' class='muted'>읽은 과제가 없습니다.</td></tr>"

    오류 = (_알림(err=error)
          + (f"<div class='warn'>{html.escape(파일오류)}</div>" if 파일오류 else ""))
    알림 = _알림(msg=msg)
    실행 = (
        "<form method='post' action='/match/all' class='mergebar'"
        " onsubmit=\"return confirm('아직 안 맞춰본 지원자를 전부 맞춰 봅니다. "
        "사람이 많으면 시간이 걸립니다. 진행할까요?')\">"
        "<button type='submit'>안 맞춰본 지원자 맞춰보기</button>"
        "<label class='muted'><input type='checkbox' name='again' value='1'> "
        "이미 맞춰본 사람도 다시</label>"
        "<span class='muted'>과제 파일을 고쳤으면 다시 돌리세요.</span></form>"
        if can(me, "지원자_등록") and 목록 else ""
    )
    return _page(
        "과제 정보 관리",
        알림 + 오류
        + "<div class='card'><p><a class='btn sec' href='/org'>부서·과제로</a></p></div>"
        + "<div class='card'><h2>과제 파일</h2>" + 설정
        + ("<p class='muted'>엑셀로 가져온 과제를 쓰고 있습니다 (<code>.env</code> 의 "
           "<code>CVTOOL_PROJECTS_JSON</code> 파일은 처음 가져올 때 합쳐 넣었고, 그 뒤로는 안 씁니다).</p>"
           if 가져온과제파일().is_file() else
           "<p class='muted'>경로는 <code>.env</code> 의 "
           "<code>CVTOOL_PROJECTS_JSON</code> 으로 정합니다. 상대경로는 "
           "<b>CV-parser 폴더 기준</b>입니다. 아래 '엑셀로 과제 추가·갱신' 을 쓰면 그 뒤로는 "
           "가져온 과제 파일을 씁니다.</p>")
        + ("<p><a class='btn' href='/match/curate'>과제 파일 다듬기</a>"
           "<span class='muted'> 원본에서 매칭에 쓸 과제·정보만 골라 둡니다.</span></p>"
           if can(me, "지원자_등록") else "")
        + "</div>"
        + (_가져오기카드() if can(me, "지원자_등록") else "")
        + f"<div class='card'><h2>연구 과제 {len(목록)}개</h2><div class='scroll'>"
        "<table data-name='연구 과제'><tr><th>번호</th><th>과제명</th><th>키워드</th>"
        "<th>담당</th><th>설명</th></tr>" + 과제줄 + "</table></div></div>"
        + "<div class='card'><h2>지원자 맞춰보기</h2>" + 실행
        + "<p class='muted'>지원자마다 <b>모든 과제와 비교</b>합니다. 결과는 "
        "<b>지원자 상세 화면</b>에서 봅니다 — 한 사람을 볼 때 같이 보는 게 "
        "맞아서 따로 목록을 두지 않습니다.</p>"
        f"<pre class='rubric'>{html.escape(GRADE_RUBRIC)}</pre></div>",
        me=me,
    )


def _가져오기카드() -> str:
    되돌리기 = (
        "<form method='post' action='/match/import/reset' style='margin-top:10px'"
        " onsubmit=\"return confirm('엑셀로 가져온 과제를 지우고 .env 의 과제 파일로 돌아갑니다. 진행할까요?')\">"
        "<button class='danger'>가져온 과제 지우기</button>"
        "<span class='muted'> .env 의 과제 파일로 돌아갑니다 (그 파일은 그대로 있습니다).</span></form>"
        if 가져온과제파일().is_file() and settings.projects_json else "")
    return (
        "<div class='card'><h2>엑셀로 과제 추가·갱신</h2>"
        "<p class='muted'>한 줄에 과제 하나, <b>첫 줄은 항목 이름</b>(과제번호 · 과제명 · 부서 · 키워드 · "
        "핵심기술 · 산출물 …). <b>과제번호가 같으면 엑셀에 값이 있는 칸만 고치고</b>, 없는 번호는 새로 넣습니다. "
        "지우지는 않습니다. 번호 열이 없으면 과제명으로 맞춥니다 — 번호가 바뀌면 기존 매칭 결과와 이어지지 "
        "않으니 번호 열을 넣어 주세요. 모르는 항목 이름도 버리지 않고 설명에 붙습니다.</p>"
        "<form method='post' action='/match/import' enctype='multipart/form-data' class='mergebar'>"
        "<input type='file' name='file' accept='.xlsx,.csv' required>"
        "<button type='submit'>읽어 보기</button><span class='muted'>바로 바뀌지 않습니다 — "
        "무엇이 추가·갱신되는지 먼저 보여 드립니다.</span></form>" + 되돌리기 + "</div>")


def _가져오기_합치기(새것: list[dict]):
    """(원본 합친 결과, 다듬은 파일 합친 결과 또는 None, 다듬은 파일을 버릴지, 알림들).
    저장은 하지 않는다.

    엑셀 업로드는 **기존 파일 사정과 상관없이** 되어야 한다. 기존 과제 파일을 못 읽으면
    (권한 · 깨진 파일) 합치지 않고 엑셀 내용만으로 만들고, 그 사실을 알린다. 못 읽는
    다듬은 파일은 버린다 — 남겨 두면 매칭이 계속 그 파일을 읽으려다 실패한다.
    """
    알림: list[str] = []
    경로 = 원본과제경로()
    기존: list = []
    if 경로:
        try:
            기존 = projectsmod.raw_items(projectsmod.read_json(경로))
        except projectsmod.ProjectsError as exc:
            알림.append(f"기존 과제 파일을 읽지 못해 합치지 않고 엑셀 내용만으로 새로 만듭니다. ({exc})")
    원본결과 = projectsmod.추가갱신(기존, 새것)
    다듬 = 다듬은파일()
    다듬결과, 다듬버림 = None, False
    if 다듬.is_file():
        try:
            다듬항목 = projectsmod.raw_items(projectsmod.read_json(str(다듬)))
            원본필드 = {k for _키, d in 기존 for k in d}
            고른 = set(projectsmod.curated_meta(다듬).get("필드") or [])
            # 원본에 없던 새 항목 이름(엑셀에만 있는 열)은 다듬은 파일에도 남긴다
            남길 = 고른 | ({k for d in 새것 for k in d} - 원본필드)
            다듬결과 = projectsmod.추가갱신(다듬항목, 새것, 남길필드=남길)
        except projectsmod.ProjectsError as exc:
            다듬버림 = True
            알림.append("다듬은 과제 파일을 읽지 못해 지우고, 매칭은 새 과제 파일 전체를 씁니다 "
                      f"(필요하면 '과제 파일 다듬기' 를 다시 하세요). ({exc})")
    return 원본결과, 다듬결과, 다듬버림, 알림


def _미리보기(me: User, 새것: list[dict], 열들: list[str], 파일명: str) -> bytes:
    r, 다듬r, _버림, 알림 = _가져오기_합치기(새것)
    열표 = "".join(
        f"<tr><td><b>{html.escape(h)}</b></td><td>{html.escape(projectsmod.열역할(h))}</td></tr>"
        for h in 열들)
    갱신줄 = "".join(f"<li><b>{html.escape(n)}</b> <span class='muted'>— {html.escape(', '.join(k))}</span></li>"
                   for n, k in r.갱신)
    추가줄 = "".join(f"<li>{html.escape(n)}</li>" for n in r.추가)
    본문 = (
        "".join(f"<div class='warn'>{html.escape(m)}</div>" for m in 알림)
        + f"<div class='card'><h2>{html.escape(파일명)} — 과제 {len(새것)}개 읽음</h2>"
        "<table><tr><th style='width:150px'>새로 추가</th>"
        f"<td><b>{len(r.추가)}개</b></td></tr>"
        f"<tr><th>고침</th><td><b>{len(r.갱신)}개</b></td></tr>"
        f"<tr><th>그대로</th><td>{len(r.같음)}개 <span class='muted'>(바뀐 칸 없음)</span></td></tr>"
        f"<tr><th>반영 뒤 전체</th><td>{len(r.과제들)}개</td></tr></table>"
        + ("<p class='muted'>다듬은 과제 파일에도 같이 반영합니다 (다듬을 때 고른 항목과 새 항목 이름만).</p>"
           if 다듬r is not None else "")
        + "<form method='post' action='/match/import/apply' class='mergebar'>"
        f"<input type='hidden' name='rows' value='{html.escape(json.dumps(새것, ensure_ascii=False))}'>"
        f"<input type='hidden' name='name' value='{html.escape(파일명)}'>"
        "<button type='submit'>이대로 반영</button>"
        "<a class='btn sec' href='/match'>취소</a></form></div>"
        "<div class='card'><h2>항목 이름을 이렇게 읽었습니다</h2>"
        "<table><tr><th>엑셀 첫 줄</th><th>쓰임</th></tr>" + 열표 + "</table></div>"
        + (f"<div class='card'><h2>새로 추가 {len(r.추가)}개</h2><ul>{추가줄}</ul></div>" if 추가줄 else "")
        + (f"<div class='card'><h2>고침 {len(r.갱신)}개</h2><ul>{갱신줄}</ul></div>" if 갱신줄 else "")
    )
    return _page("엑셀로 과제 추가·갱신", 본문, me=me)


@라우트("POST", '/match/import', 권한='지원자_등록')
def post_match_import(self, me, path):
    form = parse_multipart(self._read_body(), self.headers.get("Content-Type", ""))
    올린것 = [f for f in form.files if f.content]
    if not 올린것:
        return self._redirect("/match?err=" + urllib.parse.quote("엑셀 파일을 골라 주세요."))
    f = 올린것[0]
    try:
        새것, 열들 = projectsmod.표를_과제로(projectsmod.표_읽기(f.filename, f.content))
    except projectsmod.ProjectsError as exc:
        return self._redirect("/match?err=" + urllib.parse.quote(str(exc)))
    return self._send(_미리보기(me, 새것, 열들, f.filename))
    return _없는주소(self)


@라우트("POST", '/match/import/apply', 권한='지원자_등록')
def post_match_import_apply(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    try:
        새것 = json.loads((data.get("rows") or ["[]"])[0])
        assert isinstance(새것, list) and all(isinstance(x, dict) for x in 새것)
    except (ValueError, AssertionError):
        return self._redirect("/match?err=" + urllib.parse.quote("올린 내용을 읽지 못했습니다. 다시 올려 주세요."))
    파일명 = (data.get("name") or [""])[0]
    r, 다듬r, 다듬버림, 알림 = _가져오기_합치기(새것)
    try:
        projectsmod.save_projects(가져온과제파일(), r.과제들, 출처=f"엑셀 {파일명}", 만든이=me.아이디)
        if 다듬버림:
            다듬은파일().unlink(missing_ok=True)
        if 다듬r is not None:
            옛 = projectsmod.curated_meta(다듬은파일())
            projectsmod.save_curated(다듬은파일(), 다듬r.과제들, 출처=옛.get("출처", ""), 만든이=me.아이디)
    except OSError as exc:
        return self._redirect("/match?err=" + urllib.parse.quote(
            f"과제 파일을 저장하지 못했습니다 ({getattr(exc, 'filename', '') or 가져온과제파일()}): "
            f"{exc.strerror or exc}. 이 프로그램을 띄운 계정이 그 폴더에 쓸 수 있는지 확인하세요."))
    과제목록(다시=True)
    audit.record(me.아이디, "과제", str(가져온과제파일()), 항목="엑셀로 과제 추가·갱신",
                 새값=f"{파일명}: 추가 {len(r.추가)} · 고침 {len(r.갱신)} · 그대로 {len(r.같음)}")
    return self._redirect("/match?msg=" + urllib.parse.quote(
        f"과제 {len(r.추가)}개를 추가하고 {len(r.갱신)}개를 고쳤습니다 (전체 {len(r.과제들)}개). "
        "이미 맞춰본 지원자에 반영하려면 아래 '이미 맞춰본 사람도 다시' 를 켜고 맞춰보기를 돌리세요."
        + "".join(" / " + m.split(" (")[0] for m in 알림)))
    return _없는주소(self)


@라우트("POST", '/match/import/reset', 권한='지원자_등록')
def post_match_import_reset(self, me, path):
    가져온 = 가져온과제파일()
    if not settings.projects_json:
        return self._redirect("/match?err=" + urllib.parse.quote(
            ".env 에 과제 파일이 없어 지울 수 없습니다 (지우면 과제가 하나도 없게 됩니다)."))
    있었나 = 가져온.is_file()
    가져온.unlink(missing_ok=True)
    과제목록(다시=True)
    if 있었나:
        audit.record(me.아이디, "과제", str(가져온), 비고="엑셀로 가져온 과제 파일 삭제")
    return self._redirect("/match?msg=" + urllib.parse.quote(
        "가져온 과제를 지웠습니다. 이제 .env 의 과제 파일을 씁니다." if 있었나 else "지울 파일이 없습니다."))
    return _없는주소(self)


@라우트("GET", '/match/curate', 권한='지원자_등록', 거부말='과제 파일을 다듬는 건 채용담당자 이상만 할 수 있습니다.')
def get_match_curate(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return self._send(_curate_page(me, (params.get("err") or [""])[0],
                                   (params.get("msg") or [""])[0]))
    return _없는주소(self)


@라우트("GET", '/match', 권한='과제매칭_조회', 거부말='과제 정보는 채용담당자 이상만 볼 수 있습니다.')
def get_match(self, me, path):
    # 과제 정보 관리. 부서·과제 탭 아래 화면이다.
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return self._send(_projects_page(me, (params.get("err") or [""])[0],
                                     (params.get("msg") or [""])[0]))
    return _없는주소(self)


@라우트("POST", '/match/curate', 권한='지원자_등록')
def post_match_curate(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    고른키 = set(data.get("keys") or [])
    고른필드 = set(data.get("fields") or [])
    if not 고른키:
        return self._redirect("/match/curate?err=" + urllib.parse.quote(
            "남길 과제를 하나 이상 고르세요."))
    try:
        항목 = projectsmod.raw_items(projectsmod.read_json(원본과제경로()))
    except projectsmod.ProjectsError as exc:
        return self._redirect("/match/curate?err="
                              + urllib.parse.quote(str(exc)))
    고른것 = projectsmod.curate(항목, 고른키, 고른필드)
    if not 고른것:
        return self._redirect("/match/curate?err=" + urllib.parse.quote(
            "고른 조건으로 남는 과제가 없습니다. 필드를 더 고르세요."))
    원본 = projectsmod.resolve_path(원본과제경로())
    저장위치 = projectsmod.save_curated(
        다듬은파일(), 고른것, 출처=str(원본 or ""), 만든이=me.아이디,
    )
    과제목록(다시=True)
    audit.record(me.아이디, "과제", str(저장위치), 항목="과제 파일 다듬기",
                 새값=f"과제 {len(고른것)}개 · 필드 {len(고른필드)}종")
    return self._redirect("/match/curate?msg=" + urllib.parse.quote(
        f"과제 {len(고른것)}개를 남겨 저장했습니다. 이제 매칭은 이 파일을 씁니다. "
        f"이미 맞춰본 지원자는 '과제 매칭' 에서 다시 돌리세요."))
    return _없는주소(self)


@라우트("POST", '/match/curate/reset', 권한='지원자_등록')
def post_match_curate_reset(self, me, path):
    다듬 = 다듬은파일()
    있었나 = 다듬.is_file()
    다듬.unlink(missing_ok=True)
    과제목록(다시=True)
    if 있었나:
        audit.record(me.아이디, "과제", str(다듬), 비고="다듬은 과제 파일 삭제")
    return self._redirect("/match/curate?msg=" + urllib.parse.quote(
        "다듬은 파일을 지웠습니다. 매칭은 다시 원본을 씁니다."
        if 있었나 else "지울 파일이 없습니다."))
    return _없는주소(self)


@라우트("POST", '/match/one', 권한='지원자_등록')
def post_match_one(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    cid = (data.get("id") or [""])[0]
    뒤로 = f"/candidate?id={urllib.parse.quote(cid)}"
    rec = store.get(cid)
    if rec is None:
        return self._redirect("/")
    개수, 오류 = 매칭실행(rec, 사용자=me.아이디)
    if 오류:
        return self._redirect(f"{뒤로}&err=" + urllib.parse.quote(오류))
    return self._redirect(f"{뒤로}&msg=" + urllib.parse.quote(
        f"과제 {개수}건과 맞춰봤습니다." if 개수 else "맞춰볼 과제가 없습니다."))
    return _없는주소(self)


@라우트("POST", '/match/all', 권한='지원자_등록')
def post_match_all(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    다시 = bool(data.get("again"))
    목록, 파일오류 = 과제목록(다시=True)
    if 파일오류:
        return self._redirect("/match?err=" + urllib.parse.quote(파일오류))
    이미 = set() if 다시 else set(store.top_matches())
    한것, 실패, 첫오류 = 0, 0, ""
    for rec in store.list_all():
        if rec.지원자_ID in 이미:
            continue
        개수, 오류 = 매칭실행(rec, 사용자=me.아이디)
        if 오류:
            실패 += 1
            첫오류 = 첫오류 or 오류
        elif 개수:
            한것 += 1
    조각 = [f"{한것}명을 과제와 맞춰봤습니다"]
    if 실패:
        조각.append(f"{실패}명 실패 ({첫오류[:80]})")
    if not 한것 and not 실패:
        조각 = ["새로 맞춰볼 지원자가 없습니다"]
    return self._redirect("/match?msg=" + urllib.parse.quote(" / ".join(조각)))
    return _없는주소(self)
