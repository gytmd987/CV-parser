"""과제 매칭 · 과제 파일 다듬기.
"""

from __future__ import annotations

import html
import urllib.parse

from ... import projects as projectsmod
from ...auth import can, User
from ...config import settings
from ...matching import SCORE_RUBRIC

from ..state import audit, store
from ..jobs import 과제목록, 다듬은파일, 매칭실행, 쓰는과제파일
from ..layout import _page, _알림, _없는주소
from ..router import 라우트


def _등급이름(m: dict) -> str:
    """저장된 점수로 등급 이름을 되살린다 (Match.등급 과 같은 눈금)."""
    if not m.get("평가됨", True):
        return "미평가"
    점수 = m.get("점수") or 0
    for 문턱, 이름 in ((90, "매우 적합"), (70, "적합"), (50, "인접 분야"),
                    (30, "기초만 겹침")):
        if 점수 >= 문턱:
            return 이름
    return "접점 없음"


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
    원본경로 = settings.projects_json
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
        f"</code> <span class='muted'>.env 의 CVTOOL_PROJECTS_JSON</span></td></tr>"
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
        "<span class='muted'>(CVTOOL_MATCH_AUTO)</span></td></tr>"
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
        + "<p class='muted'>경로는 <code>.env</code> 의 "
        "<code>CVTOOL_PROJECTS_JSON</code> 으로 정합니다. 상대경로는 "
        "<b>CV-parser 폴더 기준</b>입니다.</p>"
        + ("<p><a class='btn' href='/match/curate'>과제 파일 다듬기</a>"
           "<span class='muted'> 원본에서 매칭에 쓸 과제·정보만 골라 둡니다.</span></p>"
           if can(me, "지원자_등록") else "")
        + "</div>"
        + f"<div class='card'><h2>연구 과제 {len(목록)}개</h2><div class='scroll'>"
        "<table data-name='연구 과제'><tr><th>번호</th><th>과제명</th><th>키워드</th>"
        "<th>담당</th><th>설명</th></tr>" + 과제줄 + "</table></div></div>"
        + "<div class='card'><h2>지원자 맞춰보기</h2>" + 실행
        + "<p class='muted'>지원자마다 <b>모든 과제와 비교</b>합니다. 결과는 "
        "<b>지원자 상세 화면</b>에서 봅니다 — 한 사람을 볼 때 같이 보는 게 "
        "맞아서 따로 목록을 두지 않습니다.</p>"
        f"<pre class='rubric'>{html.escape(SCORE_RUBRIC)}</pre></div>",
        me=me,
    )


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
        항목 = projectsmod.raw_items(projectsmod.read_json(settings.projects_json))
    except projectsmod.ProjectsError as exc:
        return self._redirect("/match/curate?err="
                              + urllib.parse.quote(str(exc)))
    고른것 = projectsmod.curate(항목, 고른키, 고른필드)
    if not 고른것:
        return self._redirect("/match/curate?err=" + urllib.parse.quote(
            "고른 조건으로 남는 과제가 없습니다. 필드를 더 고르세요."))
    원본 = projectsmod.resolve_path(settings.projects_json)
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
