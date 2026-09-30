"""메일 — 템플릿 · 작성 · 발송 · 이력.
"""

from __future__ import annotations

import html
import json
import urllib.parse
from pathlib import Path

from ...auth import can, User
from ...clients import mailer as mailapi
from ...config import settings
from ...fsutil import safe_filename
from ...mailing import (
    DEFAULT_RECIPIENT, IMAGE_MODES, MAX_ATTACHMENT_BYTES, RECIPIENT_KINDS, render,
    split_addresses, Template,
)
from ...normalize import MULTI_SEP
from ...schemas import columns as table_columns

from ..state import audit, auth, CONTENT_TYPES, mailing, recruit, registry, store

from ..columns import 덧값, 수식열채우기
from ..mail_targets import _안보낸것, _안보낸수
from ..layout import _page, _알림, _없는주소, _정적JS
from ..router import 라우트
from ..multipart import parse_multipart


#: 메일 본문 편집기.
#: 폐쇄망이라 외부 에디터를 못 받는다. contenteditable + execCommand 로 만들되,
#: 예전 판의 두 가지 고질병을 구조적으로 없앴다.
#:   1) 도구 모음에 native <select> 를 쓰면 포커스가 편집기에서 빠져나가서,
#:      커서 위치를 저장했다 되돌리는 방식이 필요했고 그게 들쭉날쭉했다.
#:      -> 전부 커스텀 단추로 바꾸고 mousedown 을 막아 **포커스를 아예 안 잃는다.**
#:   2) execCommand('fontSize') 는 1~7 만 받아 <font size=N> 을 남긴다.
#:      -> 7 로 표시해 두고 곧바로 <span style="font-size:12pt"> 로 바꿔치기한다.
#:      메일 클라이언트는 <style> 을 지우므로 **인라인 스타일**이 가장 안전하다.
_MAIL_JS = _정적JS("mail_editor.js")


def _볼수있는지원자(cid: str, me: User) -> bool:
    """이 사람이 그 지원자를 다룰 수 있나. 현업은 배정된 과제만."""
    if not cid or store.get(cid) is None:
        return False
    보이는 = auth.visible_project_ids(me)
    return 보이는 is None or recruit.get(cid).project_id in 보이는


def _고른조건(고른것: list[str]) -> str:
    """화면에서 온 조건을 담을 모양으로. **지금 있는 조건만** 남긴다.

    단계나 상태 이름이 바뀌면 옛 조건은 아무도 맞히지 못하는 죽은 값이 되므로,
    저장할 때 걸러 낸다.
    """
    쓸수있는것 = recruit.발송조건들()
    남길것 = [c for c in dict.fromkeys(고른것) if c in 쓸수있는것]
    return "\n".join(남길것)


def _발송조건칸(tpl: Template) -> str:
    """이 메일을 보내야 하는 때 — 단계별로 묶은 체크박스.

    상태가 열일곱 개라 한 줄로 늘어놓으면 눈으로 훑을 수가 없다. 채용 현황 표와
    같은 순서로 **단계마다 한 줄**씩 끊는다.
    """
    고른것 = set(tpl.조건들)
    줄 = []
    for 묶음, 것들 in recruit.발송조건묶음():
        칸 = "".join(
            "<label class='rt-lbl'><input type='checkbox' name='when'"
            f" value='{html.escape(c)}'{' checked' if c in 고른것 else ''}>"
            f" {html.escape(c[len(묶음):].strip() if c.startswith(묶음 + ' ') else c)}</label>"
            for c in 것들
        )
        줄.append(
            "<div class='whenrow'>"
            f"<span class='whenname'>{html.escape(묶음)}</span>{칸}</div>"
        )
    return (
        "<p class='bar' style='align-items:flex-start'>"
        "<b style='padding-top:5px'>보내야 하는 때</b>"
        f"<span style='flex:1'>{''.join(줄)}</span></p>"
        "<p class='muted' style='margin-top:-4px'>고른 상태가 된 사람 중에 이 메일을"
        " 아직 못 받은 사람을 <b>메일 발송이력</b> 화면에서 찾아 줍니다."
        " 안 골라도 됩니다 — 그러면 찾지 않습니다. 보내는 것은 늘 사람이 누릅니다.</p>"
    )


def _지원자자료(cid: str, CV첨부: bool, 지원자첨부: bool) -> tuple[list, str]:
    """그 지원자의 파일을 메일에 붙일 모양으로. (붙일것, 오류)

    면접관에게 CV 를 보내는 것처럼, 지원자 본인이 아니라 **내부로** 나가는
    메일에 그 사람의 자료를 함께 실어야 할 때가 있다. 템플릿 첨부와 달리
    **사람마다 다른 파일**이라 보낼 때 읽는다.
    """
    붙일것: list[tuple[str, bytes]] = []
    if CV첨부:
        원본 = store.file_path(cid)
        if 원본 is not None:
            rec = store.get(cid)
            이름 = (getattr(rec, "원본_파일명", "") or 원본.name) if rec else 원본.name
            붙일것.append((safe_filename(이름), 원본.read_bytes()))
    if 지원자첨부:
        for att in store.attachments(cid):
            길 = store.files_dir / att["저장명"]
            if 길.is_file():
                붙일것.append((safe_filename(att["파일명"]), 길.read_bytes()))
    합 = sum(len(b) for _이름, b in 붙일것)
    if 합 > MAX_ATTACHMENT_BYTES:
        한도 = MAX_ATTACHMENT_BYTES // (1024 * 1024)
        return [], (f"지원자 자료가 {합 / (1024 * 1024):.1f}MB 라 "
                    f"{한도}MB 를 넘습니다. 붙일 자료를 줄이세요.")
    return 붙일것, ""


def _mail_vars(rec, 진행맵=None) -> dict[str, str]:
    """이 지원자에게 쓸 수 있는 자리표시자 값.

    표에 보이는 값과 같은 것을 쓴다(명칭 사전을 거친 대표명). 화면에서 본 것과
    메일에 나가는 것이 달라지면 안 된다.
    """
    값 = {k: str(v or "") for k, v in rec.to_row(registry).items()}
    값.update(store.custom_values(rec.지원자_ID))
    p = (진행맵 or {}).get(rec.지원자_ID)
    if p is not None:
        부서명 = {d["id"]: d["이름"] for d in auth.departments()}
        과제명 = {pr["id"]: pr["이름"] for pr in auth.projects()}
        값["부서"] = 부서명.get(p.부서_id, "")
        값["과제"] = 과제명.get(p.project_id, "")
        값["최종상태"] = p.최종상태
    값.setdefault("부서", "")
    값.setdefault("과제", "")
    값.setdefault("최종상태", "")
    if store.field_formulas():
        # 표에 보이는 값 그대로 — 수식 열의 빈칸은 수식 값으로.
        덧값(값, rec.지원자_ID, recruit.started(), store.top_matches())
        수식열채우기(값, store.custom_values(rec.지원자_ID))
    값["이름"] = 값.get("한글_이름") or 값.get("영문_이름", "")
    return 값


def _mail_page(me: User, error: str = "", msg: str = "") -> bytes:
    """메일 템플릿 목록 + 새 템플릿."""
    templates = mailing.templates()
    설정경고 = ""
    빠진것 = mailapi.missing_settings()
    if settings.mail_dry_run:
        설정경고 = (
            "<div class='warn'><b>연습 모드입니다 (MAIL_DRY_RUN=1).</b> "
            "발송을 눌러도 실제로 나가지 않고 기록만 남습니다. "
            "설정을 확인한 뒤 <code>.env</code> 에서 <code>MAIL_DRY_RUN=0</code> 으로 "
            "바꾸세요.</div>"
        )
    elif 빠진것:
        설정경고 = (
            f"<div class='warn'>메일 설정이 비어 있어 보낼 수 없습니다: "
            f"<b>{html.escape(', '.join(빠진것))}</b> — <code>.env</code> 를 확인하세요.</div>"
        )
    설정경고 += (
        f"<p class='muted'>발송 구현: <b>{html.escape(mailapi.IMPL_NAME)}</b>"
        + ("" if getattr(mailapi, "LOCAL", False) else
           " · 서버에 맞춘 구현을 쓰려면 <code>cvtool/clients/mail_local.py</code> 로 두세요"
           " (git 이 건드리지 않습니다)")
        + "</p>"
    )

    탈락배지 = "<span class='pill p-미분류'>탈락 메일</span>"
    rows = "".join(
        f"<tr><td><a href='/mail/template?id={t.id}'>{html.escape(t.이름)}</a></td>"
        f"<td>{탈락배지 if t.탈락메일 else ''}"
        + ("<span class='pill p-내부'>내부</span>" if t.내부 else "")
        + ("<span class='muted' title='보낼 때 지원자 자료가 함께 붙습니다'>"
           " 지원자 자료</span>" if t.지원자자료 else "")
        + "</td>"
        f"<td title='{html.escape(t.제목)}'>{html.escape(t.제목)}</td>"
        f"<td class='muted'>{html.escape(t.참조)}</td>"
        f"<td class='muted'>{len(mailing.attachments(t.id)) or ''}</td>"
        f"<td class='muted'>{html.escape(t.수정일시)}</td>"
        f"<td><a class='btn sec' href='/mail/test?id={t.id}'>확인·시험 발송</a></td></tr>"
        for t in templates
    ) or "<tr><td colspan='7' class='muted'>아직 만든 템플릿이 없습니다.</td></tr>"

    알림 = _알림(msg=msg)
    오류 = _알림(err=error)
    return _page(
        "메일",
        알림 + 설정경고
        + "<div class='card'><h2>템플릿 만들기</h2>" + 오류
        + "<form method='post' action='/mail/template/add'>"
        "<p><input type='text' name='name' placeholder='템플릿 이름 (예: 서류합격 안내)'"
        " required style='width:320px'></p>"
        "<p class='bar'><b>받는 사람</b>"
        "<label class='rt-lbl'><input type='radio' name='to' value='지원자' checked>"
        " 지원자에게</label>"
        "<label class='rt-lbl'><input type='radio' name='to' value='내부'>"
        " 내부에 (면접관 등)</label>"
        "<span class='muted'>내부로 보내는 메일도 그 지원자 이력에 남습니다. "
        "받는 사람 주소는 보낼 때 작성창에서 적습니다.</span></p>"
        "<p><label><input type='checkbox' name='reject' value='1'> "
        "<b>탈락 메일</b> — 이걸 보낸 지원자에게는 이후 어떤 메일도 보내지 않습니다</label></p>"
        "<button type='submit'>만들기</button>"
        "<span class='muted'> 만든 뒤 편집 화면에서 제목·본문을 꾸미고 첨부를 붙입니다.</span>"
        "</form></div>"
        f"<div class='card'><h2>템플릿 {len(templates)}개</h2><div class='scroll'>"
        "<table data-name='메일 템플릿'><tr><th>이름</th><th>구분</th><th>제목</th>"
        "<th>참조</th><th>첨부</th><th>수정</th><th></th></tr>" + rows + "</table></div>"
        "<p><a class='btn sec' href='/mail/log'>발송 이력</a></p></div>",
        me=me,
    )


def _mail_var_groups() -> list[tuple[str, list[str]]]:
    """자리표시자를 사람이 찾기 쉬운 묶음으로 나눈다."""
    모든열 = list(table_columns(registry))
    기본 = ["이름", "한글_이름", "영문_이름", "생년월일", "전화번호", "이메일"]
    학력 = [c for c in 모든열 if c.startswith(("현재_", "박사_", "석사_", "학사_"))]
    연구 = [c for c in 모든열
           if c.startswith("1저자_") or c in ("연구분야_키워드", "보유기술", "경력_요약")]
    쓴것 = set(기본) | set(학력) | set(연구)
    나머지 = [c for c in 모든열 if c not in 쓴것]
    묶음 = [
        ("지원자", [c for c in 기본 if c == "이름" or c in 모든열]),
        ("현재·학력", 학력),
        ("연구·경력", 연구),
        ("채용", ["부서", "과제", "최종상태"]),
    ]
    if 나머지:
        묶음.append(("그 밖의 열", 나머지))
    if store.field_names():
        묶음.append(("추가한 열", store.field_names()))
    return [(이름, 항목) for 이름, 항목 in 묶음 if 항목]


def _mail_var_names() -> list[str]:
    return [v for _, 항목 in _mail_var_groups() for v in 항목]


#: DB 열이 아니라 **여기서 만들어 내는** 자리표시자. 뭘로 채워지는지 화면에 적는다.
#: 이걸 안 적어 두면 "이름 열이 없는데 {{이름}} 은 뭐냐" 는 질문이 계속 나온다.
MAIL_VAR_NOTES = {
    "이름": "한글_이름, 비어 있으면 영문_이름",
    "부서": "채용 현황에서 배정한 부서",
    "과제": "채용 현황에서 배정한 과제",
    "최종상태": "단계 상태에서 계산 (예: 기술 면접 합격)",
}


def _mail_template_page(tid: int, me: User, error: str = "", msg: str = "") -> bytes:
    """메일 쓰듯이 꾸며서 작성한다 (글꼴·색·표·그림)."""
    tpl = mailing.template(tid)
    if tpl is None:
        return _page("없음", "<div class='card'>템플릿을 찾을 수 없습니다.</div>", me=me)

    묶음 = _mail_var_groups()
    변수 = set(_mail_var_names())
    모르는것 = [v for v in tpl.placeholders() if v not in 변수]
    경고 = (
        f"<div class='warn'>모르는 자리표시자가 있습니다: "
        f"<b>{html.escape(', '.join(모르는것))}</b> — 이대로 보내면 그 자리는 빈칸이 되고,"
        f" 해당 지원자는 발송 대상에서 빠집니다.</div>"
        if 모르는것 else ""
    )

    글꼴 = [
        "맑은 고딕", "굴림", "굴림체", "돋움", "돋움체", "바탕", "바탕체", "궁서",
        "나눔고딕", "나눔명조", "함초롬바탕",
        "Arial", "Helvetica", "Verdana", "Tahoma", "Trebuchet MS",
        "Times New Roman", "Georgia", "Courier New", "Consolas",
    ]
    크기 = ["8pt", "9pt", "10pt", "11pt", "12pt", "14pt", "16pt", "18pt",
          "20pt", "24pt", "28pt", "32pt", "36pt", "48pt"]

    def 단추(cmd: str, 표시: str, 도움말: str) -> str:
        return (f"<button type='button' title='{도움말}'"
                f" onclick=\"rtCmd('{cmd}')\">{표시}</button>")

    def 드롭(btn_id: str, 라벨: str, 함수: str, 도움말: str, 너비: str = "") -> str:
        스타일 = f" style='min-width:{너비}'" if 너비 else ""
        return (f"<button type='button' class='rt-drop' id='{btn_id}'"
                f" title='{도움말}' onclick='{함수}(this)'{스타일}>"
                f"<span>{라벨}</span><i>▾</i></button>")

    # 표 편집 도구 — 커서가 표 안에 있을 때만 뜬다.
    #
    # 예전에는 표를 **넣기만** 하고 그 뒤로는 손댈 수가 없었다. 6×6 보다 크게
    # 만들 수도, 열 너비를 잡을 수도, 테두리를 바꿀 수도 없어서 결국 표를 지우고
    # 다시 넣는 수밖에 없었다.
    _표도구 = (
        "<span class='rt-lbl'>표</span>"
        "<button type='button' onclick='rtRow(-1)' title='커서가 있는 줄 위에 넣기'"
        ">행 ↑</button>"
        "<button type='button' onclick='rtRow(1)' title='커서가 있는 줄 아래에 넣기'"
        ">행 ↓</button>"
        "<button type='button' onclick='rtRowDel()' class='sec' title='이 줄 지우기'"
        ">행 −</button>"
        "<span class='rt-sep'></span>"
        "<button type='button' onclick='rtCol(-1)' title='커서가 있는 칸 왼쪽에 넣기'"
        ">열 ←</button>"
        "<button type='button' onclick='rtCol(1)' title='커서가 있는 칸 오른쪽에 넣기'"
        ">열 →</button>"
        "<button type='button' onclick='rtColDel()' class='sec' title='이 열 지우기'"
        ">열 −</button>"
        "<span class='rt-sep'></span>"
        # 표 자체의 너비. 이게 100% 로 박혀 있으면 열 너비를 아무리 고쳐도
        # 정해진 폭을 나눠 갖는 것뿐이라, 열 하나만 넓히는 게 불가능했다.
        "<label class='rt-lbl' title='표 전체의 너비'>표"
        "<select id='rt-tblw' onchange='rtTableWidth()'>"
        "<option value='100%'>창에 맞춤 (100%)</option>"
        "<option value='fit'>열 너비에 맞춤</option>"
        "<option value='auto'>내용에 맞춤</option>"
        "<option value='px'>표 폭 고정 (px)</option>"
        "</select>"
        "<input type='number' id='rt-tblpx' min='80' max='2000' step='10'"
        " style='width:70px;display:none' placeholder='px'"
        " oninput='rtTableWidth()'></label>"
        "<span class='rt-sep'></span>"
        "<label class='rt-lbl' title='커서가 있는 열의 너비. 비우면 자동."
        " 경계선을 끌어도 됩니다'>열"
        "<input type='number' id='rt-colw' min='1' max='2000' step='1'"
        " style='width:66px' oninput='rtColWidth()'>"
        "<select id='rt-colu' onchange='rtColWidth()'>"
        "<option value='px'>px</option><option value='%'>%</option>"
        "</select></label>"
        "<span class='rt-sep'></span>"
        "<label class='rt-lbl' title='표 전체의 테두리'>테두리"
        "<select id='rt-border' onchange='rtBorder(this.value)'>"
        "<option value='1px solid #999'>실선 (얇게)</option>"
        "<option value='2px solid #333'>실선 (굵게)</option>"
        "<option value='1px solid #d1d5db'>연한 회색</option>"
        "<option value='none'>없음</option>"
        "</select></label>"
        "<label class='rt-lbl' title='첫 줄을 머리글처럼 (굵게 + 회색 배경)'>"
        "<input type='checkbox' id='rt-head' onchange='rtHeadRow(this.checked)'>"
        "머리글 줄</label>"
        "<span style='flex:1'></span>"
        "<button type='button' onclick='rtTableDel()' class='danger'"
        " title='표를 통째로 지웁니다'>표 지우기</button>"
    )

    도구 = (
        드롭("rt-font-btn", "맑은 고딕", "rtFontMenu", "글꼴", "104px")
        + 드롭("rt-size-btn", "12pt", "rtSizeMenu", "글씨 크기", "62px")
        + "<span class='rt-sep'></span>"
        + 단추("bold", "<b>가</b>", "굵게 (Ctrl+B)")
        + 단추("italic", "<i>가</i>", "기울임 (Ctrl+I)")
        + 단추("underline", "<u>가</u>", "밑줄 (Ctrl+U)")
        + 단추("strikeThrough", "<s>가</s>", "취소선")
        + 드롭("rt-fore-btn", "<span class='rt-ink' style='color:#b91c1c'>가</span>",
              "rtColorMenuFore", "글자색")
        + 드롭("rt-back-btn", "<span class='rt-ink' style='background:#fef08a'>가</span>",
              "rtColorMenuBack", "배경색")
        + "<span class='rt-sep'></span>"
        + 단추("justifyLeft", "≡", "왼쪽 정렬")
        + 단추("justifyCenter", "☰", "가운데 정렬")
        + 단추("justifyRight", "≣", "오른쪽 정렬")
        + 단추("insertUnorderedList", "•", "글머리 기호")
        + 단추("insertOrderedList", "1.", "번호 매기기")
        + 단추("outdent", "⇤", "내어쓰기")
        + 단추("indent", "⇥", "들여쓰기")
        + "<span class='rt-sep'></span>"
        + "<button type='button' title='링크' onclick='rtLink()'>링크</button>"
        + 드롭("rt-table-btn", "표", "rtTableMenu", "표 넣기")
        + "<label title='그림 넣기' class='btnlike'>그림"
          "<input type='file' accept='image/*' style='display:none'"
          " onchange='rtImage(this)'></label>"
        + 단추("removeFormat", "지우기", "꾸미기 지우기")
        + "<span style='flex:1'></span>"
        + "<button type='button' class='rt-drop rt-var' id='rt-var-btn'"
          " onclick='rtVars(this)'>＋ 자리표시자</button>"
    )

    첨부 = mailing.attachments(tpl.id)
    첨부행 = "".join(
        f"<tr><td><a href='/mail/attachment?id={a['id']}'>{html.escape(a['파일명'])}</a></td>"
        f"<td class='muted'>{a['크기'] // 1024 or 1}KB</td>"
        f"<td class='muted'>{html.escape(a['올린일시'])}</td>"
        f"<td><form method='post' action='/mail/attachment/delete'"
        f" onsubmit=\"return confirm('이 첨부를 뺍니다.')\">"
        f"<input type='hidden' name='id' value='{a['id']}'>"
        f"<input type='hidden' name='template' value='{tpl.id}'>"
        f"<button class='danger'>빼기</button></form></td></tr>"
        for a in 첨부
    ) or "<tr><td colspan='4' class='muted'>붙인 파일이 없습니다.</td></tr>"

    알림 = _알림(msg=msg)
    오류 = _알림(err=error)
    변수JSON = json.dumps([[이름, 항목] for 이름, 항목 in 묶음], ensure_ascii=False)
    설명JSON = json.dumps(MAIL_VAR_NOTES, ensure_ascii=False)
    그림카드 = _mail_image_card(tpl)
    글꼴JSON = json.dumps(글꼴, ensure_ascii=False)
    크기JSON = json.dumps(크기, ensure_ascii=False)

    return _page(
        f"{tpl.이름} 템플릿",
        알림 + 경고
        + "<div class='card'><h2>템플릿 편집</h2>" + 오류
        + "<form method='post' action='/mail/template/save' id='tplform'>"
        f"<input type='hidden' name='id' value='{tpl.id}'>"
        "<p><label>템플릿 이름<br><input type='text' name='name'"
        f" value='{html.escape(tpl.이름)}' required style='width:360px'"
        f" data-orig='{html.escape(tpl.이름)}' oninput='markDirty(this)'></label></p>"
        "<p><label>참조 (CC)<br><input type='text' name='cc'"
        f" value='{html.escape(tpl.참조)}' style='width:100%'"
        " placeholder='team@회사.com, hr@회사.com — 쉼표나 세미콜론으로 구분'"
        f" data-orig='{html.escape(tpl.참조)}' oninput='markDirty(this)'></label>"
        "<br><span class='muted'>이 템플릿으로 보내는 모든 메일에 함께 들어갑니다.</span></p>"
        "<p><label>제목<br><input type='text' name='subject'"
        f" value='{html.escape(tpl.제목)}' style='width:100%'"
        " placeholder='예: [{{부서}}] 서류 전형 결과 안내'"
        f" data-orig='{html.escape(tpl.제목)}' oninput='markDirty(this)'></label></p>"
        "<p>본문</p>"
        f"<div class='rt'><div class='rt-bar'>{도구}</div>"
        f"<div class='rt-bar rt-tablebar' id='rttablebar' hidden>{_표도구}</div>"
        f"<div class='rt-body' id='rtbody' contenteditable='true'>{tpl.본문}</div></div>"
        f"<input type='hidden' name='body' id='bodyfield' data-orig=''>"
        + "<p class='muted'>자리표시자는 대부분 <b>표의 열 이름</b> 그대로입니다"
        " (<code>{{한글_이름}}</code> <code>{{박사_학교}}</code>). "
        "DB 에 없는데 쓸 수 있는 것은 아래 넷뿐이고, 보낼 때 이렇게 채워집니다:"
        "<br>"
        + " · ".join(f"<code>{{{{{k}}}}}</code> = {html.escape(v)}"
                     for k, v in MAIL_VAR_NOTES.items())
        + "</p>"
        "<p class='bar'><b>받는 사람</b>"
        "<label class='rt-lbl'><input type='radio' name='to' value='지원자'"
        f"{'' if tpl.내부 else ' checked'}> 지원자에게</label>"
        "<label class='rt-lbl'><input type='radio' name='to' value='내부'"
        f"{' checked' if tpl.내부 else ''}> 내부에 (면접관 등)</label>"
        "<span class='muted'>"
        + ("받는 사람 주소는 보낼 때 작성창에서 적습니다. 이력은 지원자별로 남습니다."
           if tpl.내부 else "지원자 이메일 주소로 나갑니다.")
        + "</span></p>"
        + _발송조건칸(tpl)
        + "<p class='bar'><b>함께 보낼 지원자 자료</b>"
        "<label class='rt-lbl'><input type='checkbox' name='cvattach' value='1'"
        f"{' checked' if tpl.CV첨부 else ''}> CV 원본</label>"
        "<label class='rt-lbl'><input type='checkbox' name='candattach' value='1'"
        f"{' checked' if tpl.지원자첨부 else ''}> 지원자 첨부파일</label>"
        "<span class='muted'>사람마다 다른 파일이라 보낼 때 붙습니다. "
        "작성창에서 그 자리에서 끌 수도 있습니다.</span></p>"
        "<p><label><input type='checkbox' name='reject' value='1'"
        f"{' checked' if tpl.탈락메일 else ''}> <b>탈락 메일</b> — 이걸 받은 지원자에게는"
        " 이후 어떤 메일도 나가지 않습니다</label></p>"
        "<p><button type='submit'>저장</button> "
        f"<a class='btn sec' href='/mail/test?id={tpl.id}'>확인·시험 발송</a> "
        "<a class='btn sec' href='/mail'>목록</a></p></form>"
        "<form method='post' action='/mail/template/delete' style='margin-top:10px'"
        " onsubmit=\"return confirm('이 템플릿을 지웁니다. 이미 보낸 기록은 남습니다.')\">"
        f"<input type='hidden' name='id' value='{tpl.id}'>"
        "<button class='danger'>템플릿 삭제</button>"
        "<span class='muted'> 발송 기록은 남습니다 — 누구에게 뭘 보냈는지는 기록입니다.</span>"
        "</form></div>"

        + "<div class='card'><h2>첨부파일</h2>"
        "<form method='post' action='/mail/attachment/add' enctype='multipart/form-data'>"
        f"<input type='hidden' name='template' value='{tpl.id}'>"
        "<input type='file' name='files' multiple>"
        "<button type='submit'>붙이기</button>"
        "<span class='muted'> 이 템플릿으로 보내는 모든 메일에 함께 갑니다. "
        "한 개 10MB 까지.</span></form>"
        "<div class='scroll' style='margin-top:10px'><table data-name='첨부파일'>"
        "<tr><th>파일</th><th>크기</th><th>붙인 일시</th><th></th></tr>"
        + 첨부행 + "</table></div>"
        "<p class='muted'>본문에 넣는 그림은 <b>그림</b> 단추를 쓰세요(2MB 까지). "
        "큰 파일은 여기에 붙입니다.</p></div>"
        + 그림카드
        + f"<script>window.템플릿ID = {tpl.id};"
        f"window.자리표시자 = {변수JSON};window.자리표시자설명 = {설명JSON};"
f"window.rtFonts = {글꼴JSON};window.rtSizes = {크기JSON};"
f"function rtColorMenuFore(b){{rtColorMenu(b, 'foreColor');}}"
f"function rtColorMenuBack(b){{rtColorMenu(b, 'hiliteColor');}}"
f"{_MAIL_JS}</script>",
        me=me,
    )


#: 본문 그림을 메일에 어떻게 실을지 — 화면에 그대로 적는다.
IMAGE_MODE_NOTE = {
    "본문": "본문에 그림을 박아 보냅니다. 메일 API 나 받는 쪽이 그림을 "
          "어디로 옮기는지 우리가 관여할 수 없습니다.",
    "본문+첨부": "본문에 박고 <b>같은 파일을 첨부로도</b> 보냅니다. 본문 그림이 "
              "나중에 깨져도 받은 사람 손에 파일은 남습니다. (권장)",
    "첨부만": "본문에서 그림을 빼고 첨부로만 보냅니다. 본문이 가볍고 깨질 그림이 "
           "아예 없습니다.",
}


def _mail_image_card(tpl) -> str:
    """본문 그림 — 어떻게 보낼지 고르고, 지금 쓰는 그림을 확인한다.

    본문에 박은 그림이 **시간이 지나 깨지는** 일이 있었다. 우리 DB 에는 원본이
    그대로 있으니, 무엇이 어떤 방식으로 나가는지 눈으로 볼 수 있어야 한다.
    """
    쓰는것 = mailing.used_body_images(tpl.본문)
    쓰는id = {i["id"] for i in 쓰는것}
    안쓰는것 = [i for i in mailing.body_images(tpl.id) if i["id"] not in 쓰는id]

    def 줄(img: dict, 쓴다: bool) -> str:
        있나 = mailing.body_image_bytes(img["id"]) is not None
        상태 = ("<span class='pill p-완료'>원본 있음</span>" if 있나
              else "<span class='pill p-실패'>파일 없음</span>")
        지우기 = (
            "<form method='post' action='/mail/image/delete' style='display:inline'"
            " onsubmit=\"return confirm('이 그림을 지웁니다.')\">"
            f"<input type='hidden' name='id' value='{img['id']}'>"
            f"<input type='hidden' name='template' value='{tpl.id}'>"
            "<button class='danger'>지우기</button></form>" if not 쓴다 else
            "<span class='muted'>본문에서 쓰는 중</span>"
        )
        미리 = (f"<img src='/mail/image?id={img['id']}' alt=''"
              " style='max-height:44px;max-width:80px;vertical-align:middle'>"
              if 있나 else "-")
        return (
            f"<tr><td>{미리}</td><td>{html.escape(img['파일명'])}"
            f"<br><span class='muted'>id {img['id']}</span></td>"
            f"<td>{img['크기'] // 1024}KB</td><td>{상태}</td>"
            f"<td class='muted'>{html.escape(img['올린일시'])}</td>"
            f"<td>{지우기}</td></tr>"
        )

    행 = ("".join(줄(i, True) for i in 쓰는것)
         + "".join(줄(i, False) for i in 안쓰는것)) or (
        "<tr><td colspan='6' class='muted'>본문에 넣은 그림이 없습니다.</td></tr>")

    고르기 = "".join(
        f"<label style='display:block;padding:3px 0'>"
        f"<input type='radio' name='imgmode' form='tplform' value='{html.escape(m)}'"
        f"{' checked' if tpl.그림보내기 == m else ''}> <b>{html.escape(m)}</b> — "
        f"{IMAGE_MODE_NOTE[m]}</label>"
        for m in IMAGE_MODES
    )
    return (
        "<div class='card'><h2>본문 그림</h2>"
        "<p class='muted'>그림 원본은 <b>이 시스템에 파일로</b> 보관됩니다. "
        "본문에는 짧은 참조만 들어가므로, 본문을 아무리 고쳐도 원본은 상하지 "
        "않습니다. 메일에 어떻게 실을지만 고르면 됩니다.</p>"
        f"<div style='margin:8px 0 12px'>{고르기}</div>"
        "<p class='muted'>고른 뒤 위 <b>저장</b> 을 누르세요.</p>"
        "<div class='scroll'><table data-name='본문 그림'>"
        "<tr><th style='width:90px'>미리보기</th><th>파일</th><th>크기</th>"
        "<th>원본</th><th>올린 일시</th><th></th></tr>" + 행 + "</table></div>"
        "<div class='warn' style='margin-top:8px'>메일 프로그램(특히 Outlook)은 "
        "<b>본문에 박은 그림을 막거나 주소로 바꿔</b> 두는 일이 있습니다. "
        "그 주소가 나중에 없어지면 <b>예전에 보낸 메일의 그림이 깨집니다.</b> "
        "우리가 어쩌지 못하는 구간이라, 꼭 봐야 하는 그림이면 "
        "<b>본문+첨부</b> 나 <b>첨부만</b> 으로 두세요 — 첨부는 메일 안에 "
        "남으므로 사라지지 않습니다.</div></div>"
    )


def _mail_targets(ids: list[str], tpl, me: User):
    """(보낼 수 있는 사람, 못 보내는 사람) 을 이유와 함께.

    화면과 실제 발송이 **같은 함수**를 쓴다. 다르면 화면에서 본 것과 나가는
    것이 어긋난다.
    """
    진행맵 = recruit.all()
    보이는과제 = auth.visible_project_ids(me)
    갈사람, 막힌사람 = [], []
    for cid in dict.fromkeys(ids):
        rec = store.get(cid)
        if rec is None:
            continue
        if 보이는과제 is not None and recruit.get(cid).project_id not in 보이는과제:
            continue
        값 = _mail_vars(rec, 진행맵)
        이름 = 값.get("한글_이름") or 값.get("영문_이름") or cid
        받는사람 = (값.get("이메일") or "").split(MULTI_SEP)[0].strip()
        제목, 빈1 = render(tpl.제목, 값)
        본문, 빈2 = render(tpl.본문, 값)
        빈칸 = list(dict.fromkeys(빈1 + 빈2))
        막힘 = mailing.blocked_reason(cid, tpl)
        if tpl.내부:
            # 내부 메일은 받는 사람을 작성창에서 직접 적는다. 지원자 주소가
            # 없다고 막으면, 주소를 모르는 지원자의 CV 를 못 보내게 된다.
            받는사람 = ""
        elif not 막힘 and not 받는사람:
            막힘 = "이메일 주소가 없습니다"
        # 빈 자리표시자는 **막지 않는다.** 예전에는 하나라도 비면 못 보냈는데,
        # 그러면 "빈칸을 채워서 보내 주세요" 라는 메일을 정작 빈칸이 있는
        # 사람에게 못 보낸다. 그게 이 기능이 제일 필요한 자리다.
        # 대신 눈에 띄게 알리고, 보내기 전 인원수 확인은 그대로 거친다.
        한줄 = {"cid": cid, "이름": 이름, "받는사람": 받는사람,
              "제목": 제목, "본문": 본문, "막힘": 막힘, "빈칸": 빈칸}
        (막힌사람 if 막힘 else 갈사람).append(한줄)
    return 갈사람, 막힌사람


def _mail_compose_page(ids: list[str], tid: int, me: User, 뒤로: str = "/",
                       error: str = "") -> bytes:
    """고른 사람에게 보낼 템플릿을 고르고, **한 통씩 작성창을 열어** 보낸다.

    예전에는 `메일` 탭에서 템플릿을 열면 **지원자 전원**이 나왔다. 서류 합격
    안내를 보내려는데 누가 서류 합격인지 그 화면에서는 알 수가 없었다.
    이제 반대다 — 인재 Pool·채용 현황에서 **거른 뒤 고른 사람**을 데리고 온다.

    그리고 예전에는 "몇 명에게 나갑니다 → 인원수를 쳐 넣으세요" 한 번으로 끝나서
    **한 통 한 통 무엇이 나가는지 보고 고칠 수가 없었다.** 이제 줄마다 작성창을
    열어 받는 사람·제목·본문·첨부를 확인하고 고쳐서 보낸다. 예전 방식은 아래
    접어 둔 자리에 그대로 남겨 뒀다 — 손댈 게 없는 안내 메일에는 그게 빠르다.
    """
    templates = mailing.templates()
    고른수 = len(dict.fromkeys(ids))
    돌아가기 = f"<a class='btn sec' href='{html.escape(뒤로)}'>돌아가기</a>"
    오류 = _알림(err=error)

    if not ids:
        return _page("메일 보내기", 오류 + "<div class='card'><h2>고른 사람이 없습니다</h2>"
                     "<p class='muted'>표에서 보낼 사람을 체크한 뒤 다시 누르세요.</p>"
                     f"<p>{돌아가기}</p></div>", me=me)

    숨김 = "".join(
        f"<input type='hidden' name='ids' value='{html.escape(c)}'>"
        for c in dict.fromkeys(ids)
    ) + f"<input type='hidden' name='back' value='{html.escape(뒤로)}'>"

    tpl = mailing.template(tid) if tid else None
    if tpl is None:
        고르기 = "".join(
            f"<label style='display:block;padding:4px 0'>"
            f"<input type='radio' name='template' value='{t.id}'"
            f"{' checked' if i == 0 else ''}> <b>{html.escape(t.이름)}</b>"
            + (" <span class='pill p-미분류'>탈락 메일</span>" if t.탈락메일 else "")
            + (" <span class='pill p-내부'>내부</span>" if t.내부 else "")
            + f" <span class='muted'>{html.escape(t.제목)}</span></label>"
            for i, t in enumerate(templates)
        ) or "<p class='muted'>만들어 둔 템플릿이 없습니다.</p>"
        return _page(
            "메일 보내기",
            오류
            + f"<div class='card'><h2>고른 사람 {고른수}명</h2>"
            "<p class='muted'>보낼 템플릿을 고르세요. 다음 화면에서 "
            "<b>한 통씩 작성창을 열어</b> 확인하고 보냅니다.</p>"
            "<form method='post' action='/mail/compose'>" + 숨김 + 고르기
            + "<p><button type='submit'>다음 — 보낼 목록</button> "
            + 돌아가기 + "</p></form>"
            + ("<p class='muted'><a href='/mail'>메일 탭</a>에서 템플릿을 "
               "만들고 고칠 수 있습니다.</p>")
            + "</div>",
            me=me,
        )

    갈사람, 막힌사람 = _mail_targets(ids, tpl, me)

    def 줄(x: dict) -> str:
        cid = x["cid"]
        주소 = (html.escape(x["받는사람"]) if x["받는사람"]
              else "<span class='muted'>작성창에서 입력</span>")
        빈 = x.get("빈칸") or []
        빈칸 = (f"<span class='flag' title='{html.escape(', '.join(빈))}'>"
              f"빈 항목 {len(빈)}개</span>" if 빈 else "")
        열기 = (f"<button type='button' class='sec tiny'"
              f" onclick=\"작성창({tpl.id}, '{html.escape(cid)}')\">작성창 열기</button>")
        return (f"<tr data-cid='{html.escape(cid)}'>"
                f"<td>{html.escape(x['이름'])}</td><td>{주소}</td>"
                f"<td class='보냄칸'><span class='muted'>보낼 수 있음</span> {빈칸}</td>"
                f"<td class='ctl'>{열기}</td></tr>")

    갈줄 = "".join(줄(x) for x in 갈사람) or (
        "<tr><td colspan='4' class='muted'>보낼 수 있는 사람이 없습니다.</td></tr>")
    막힌줄 = "".join(
        f"<tr class='dup'><td>{html.escape(x['이름'])}</td>"
        f"<td class='flag' title='{html.escape(x['막힘'])}'>"
        f"{html.escape(x['막힘'])}</td></tr>"
        for x in 막힌사람
    )

    첨부 = mailing.attachments(tpl.id)
    딸림 = []
    if tpl.내부:
        딸림.append("<b>내부 메일</b> — 받는 사람은 작성창에서 적습니다")
    if tpl.cc():
        딸림.append("참조 <b>" + html.escape(", ".join(tpl.cc())) + "</b>")
    if 첨부:
        딸림.append("첨부 <b>"
                  + html.escape(", ".join(a["파일명"] for a in 첨부)) + "</b>")
    지원자자료 = [이름 for 켬, 이름 in ((tpl.CV첨부, "CV 원본"),
                                (tpl.지원자첨부, "지원자 첨부파일")) if 켬]
    if 지원자자료:
        딸림.append("지원자 자료 <b>" + " · ".join(지원자자료) + "</b>")
    if mailing.used_body_images(tpl.본문):
        딸림.append(f"본문 그림 <b>{tpl.그림보내기}</b>")
    딸림칸 = f"<p class='muted'>{' · '.join(딸림)}</p>" if 딸림 else ""

    연습 = (
        "<div class='warn'><b>연습 모드 (MAIL_DRY_RUN=1)</b> — 실제로 나가지 않고 "
        "기록만 남습니다. 기록이 남으면 '이미 보냄' 으로 처리되니 주의하세요.</div>"
        if settings.mail_dry_run else ""
    )
    탈락표시 = (
        "<div class='warn'><b>이 템플릿은 탈락 메일입니다.</b> 보내고 나면 그 지원자에게는 "
        "이후 어떤 메일도 보낼 수 없습니다.</div>" if tpl.탈락메일 else ""
    )
    빠진것 = mailapi.missing_settings()
    설정경고 = (
        f"<div class='warn'>메일 설정이 비어 있어 보낼 수 없습니다: "
        f"<b>{html.escape(', '.join(빠진것))}</b></div>"
        if 빠진것 and not settings.mail_dry_run else ""
    )

    # 예전 방식 — 손댈 게 없는 안내 메일에는 이쪽이 빠르다. 접어 둔다.
    한번에 = ""
    if can(me, "메일_발송") and 갈사람 and not tpl.내부:
        한번에 = (
            "<details class='draft'><summary>한 번에 보내기 (예전 방식)</summary>"
            "<div style='margin-top:10px'>"
            "<p class='muted'>작성창을 열지 않고 <b>템플릿 그대로</b> 한꺼번에 "
            "보냅니다. 고칠 게 없을 때만 쓰세요.</p>"
            "<form method='post' action='/mail/send'"
            " onsubmit=\"return window.confirm('정말 보냅니다. 되돌릴 수 없습니다.')\">"
            + 숨김
            + f"<input type='hidden' name='template' value='{tpl.id}'>"
            "<p>보낼 인원수 <b>" + str(len(갈사람)) + "</b> 을 그대로 쳐 넣으세요: "
            "<input type='text' name='confirm' style='width:80px'"
            " placeholder='숫자' autocomplete='off' required> "
            "<button type='submit'>한 번에 보내기</button></p>"
            "<p class='muted'>숫자를 직접 치게 하는 이유는 하나입니다 — "
            "확인창은 안 읽고 누르지만 숫자는 화면을 봐야 칠 수 있습니다.</p>"
            "</form></div></details>"
        )
    elif tpl.내부 and 갈사람:
        한번에 = ("<p class='muted'>내부 메일은 받는 사람을 한 통씩 적어야 해서 "
                "한 번에 보내기가 없습니다.</p>")

    못보냄 = ("" if can(me, "메일_발송") else
            "<div class='warn'>메일 발송 권한이 없습니다. 보기만 됩니다.</div>")

    return _page(
        "메일 보내기",
        오류 + 연습 + 설정경고 + 탈락표시 + 못보냄
        + f"<div class='card'><h2>{html.escape(tpl.이름)} "
        + ("<span class='pill p-내부'>내부</span> " if tpl.내부 else "")
        + f"<span class='muted'>{html.escape(tpl.제목)}</span></h2>" + 딸림칸
        + f"<p class='muted'>고른 {고른수}명 중 <b>{len(갈사람)}명</b>에게 보낼 수 있고 "
        f"<b>{len(막힌사람)}명</b>은 못 보냅니다.</p>"
        "<p><form method='post' action='/mail/compose' style='display:inline'>"
        + 숨김 + "<button class='sec'>다른 템플릿 고르기</button></form> "
        + 돌아가기
        + f" <a class='btn sec' href='/mail/template?id={tpl.id}'>템플릿 고치기</a></p>"
        "</div>"
        + f"<div class='card'><h2>보낼 목록 {len(갈사람)}명</h2>"
        "<p class='muted'><b>작성창 열기</b>를 누르면 그 사람에게 나갈 메일이 "
        "새 창으로 뜹니다. 거기서 받는 사람·제목·본문·첨부를 고치고 보내면 됩니다. "
        "여러 줄을 눌러 창을 여러 개 띄워도 됩니다.</p>"
        "<div class='scroll'><table data-name='보낼 목록' id='보낼목록'>"
        "<tr><th style='width:160px'>지원자</th><th style='width:240px'>받는 사람</th>"
        "<th>상태</th><th class='ctl' style='width:120px'></th></tr>"
        + 갈줄 + "</table></div>"
        + 한번에 + "</div>"
        + (f"<div class='card'><h2>못 보내는 사람 {len(막힌사람)}명</h2>"
           "<div class='scroll'><table data-name='못 보내는 사람'>"
           "<tr><th>지원자</th><th>이유</th></tr>" + 막힌줄 + "</table></div></div>"
           if 막힌사람 else "")
        + _COMPOSE_JS,
        me=me,
    )


#: 작성창을 새 창으로 띄운다.
#:
#: 누를 때마다 창 하나라서 브라우저가 막지 않는다 (한 번에 여러 개를 열면 막힌다).
#: 보내고 나면 작성창이 부모의 `보냄표시` 를 불러 그 줄을 바꿔 놓고 닫힌다.
_COMPOSE_JS = '\n<script>' + _정적JS("compose.js") + '</script>'


def _내부주소들(limit: int = 12) -> list[str]:
    """최근에 내부 메일을 보낸 주소. 매번 손으로 치지 않게 제안만 한다."""
    본것: list[str] = []
    for r in mailing.history(limit=300):
        if (r.get("받는대상") or DEFAULT_RECIPIENT) != "내부":
            continue
        for 주소 in (r.get("받는사람") or "").replace(";", ",").split(","):
            주소 = 주소.strip()
            if 주소 and 주소 not in 본것:
                본것.append(주소)
        if len(본것) >= limit:
            break
    return 본것[:limit]


def _mail_draft_page(tid: int, cid: str, me: User, error: str = "",
                     넣은값: dict | None = None) -> bytes:
    """메일 한 통을 쓰는 창. **새 창으로 뜬다.**

    예전에는 "몇 명에게 나갑니다" 만 보고 보냈다. 그러면 한 사람 한 사람에게
    무엇이 나가는지 보고 고칠 방법이 없다. 여기서는 받는 사람·제목·본문·첨부를
    다 보여 주고, 고친 그대로 보내고 고친 그대로 기록한다.
    """
    넣은값 = 넣은값 or {}
    tpl = mailing.template(tid)
    rec = store.get(cid)
    if tpl is None or rec is None:
        return _page("메일 쓰기", "<div class='card'><p class='flag'>"
                     "템플릿이나 지원자를 찾을 수 없습니다.</p></div>", nav=False)

    값 = _mail_vars(rec, recruit.all())
    이름 = 값.get("한글_이름") or 값.get("영문_이름") or cid
    제목, 빈1 = render(tpl.제목, 값)
    본문, 빈2 = render(tpl.본문, 값)
    빈칸 = list(dict.fromkeys(빈1 + 빈2))
    받는사람 = "" if tpl.내부 else (값.get("이메일") or "").split(MULTI_SEP)[0].strip()

    # 사람이 고쳐 넣은 값이 있으면 그게 이긴다 (오류로 돌아왔을 때)
    받는사람 = 넣은값.get("to", 받는사람)
    참조 = 넣은값.get("cc", tpl.참조 or "")
    제목 = 넣은값.get("subject", 제목)
    본문 = 넣은값.get("body", 본문)
    CV켬 = 넣은값.get("cv", tpl.CV첨부)
    첨부켬 = 넣은값.get("cand", tpl.지원자첨부)

    막힘 = mailing.blocked_reason(cid, tpl)
    경고 = [_알림(err=error)] if error else []
    if 막힘:
        경고.append(f"<div class='warn'><b>보낼 수 없습니다.</b> {html.escape(막힘)}</div>")
    if settings.mail_dry_run:
        경고.append("<div class='warn'><b>연습 모드 (MAIL_DRY_RUN=1)</b> — 실제로 "
                  "나가지 않고 기록만 남습니다.</div>")
    if tpl.탈락메일:
        경고.append("<div class='warn'><b>탈락 메일입니다.</b> 보내고 나면 이 지원자에게는 "
                  "이후 어떤 메일도 보낼 수 없습니다.</div>")
    빠진것 = mailapi.missing_settings()
    if 빠진것 and not settings.mail_dry_run:
        경고.append("<div class='warn'>메일 설정이 비어 있어 보낼 수 없습니다: "
                  f"<b>{html.escape(', '.join(빠진것))}</b></div>")
    if 빈칸:
        경고.append("<div class='warn'>값이 없는 자리표시자가 있어 그 자리가 "
                  f"<b>빈 채로</b> 나갑니다: {html.escape(', '.join(빈칸))}</div>")

    # 첨부 — 템플릿 것은 고정, 지원자 자료는 여기서 켜고 끈다
    템플릿첨부 = mailing.attachments(tpl.id)
    자료, 자료오류 = _지원자자료(cid, CV켬, 첨부켬)
    if 자료오류:
        경고.append(f"<div class='warn'>{html.escape(자료오류)}</div>")
    붙는것 = ([(a["파일명"], int(a["크기"] or 0)) for a in 템플릿첨부]
            + [(이름2, len(b)) for 이름2, b in 자료])
    합 = sum(크기 for _, 크기 in 붙는것)
    첨부줄 = "".join(
        f"<li>{html.escape(n)} <span class='muted'>{크기 / 1024:.0f}KB</span></li>"
        for n, 크기 in 붙는것
    ) or "<li class='muted'>없음</li>"
    있는CV = store.file_path(cid) is not None
    있는첨부 = len(store.attachments(cid))

    제안 = "".join(f"<option value='{html.escape(a)}'>" for a in _내부주소들())
    보낼수있나 = can(me, "메일_발송") and not 막힘 and not 자료오류

    본문칸 = (f"<div class='rt-body' id='본문칸' contenteditable='true'>{본문}</div>"
           if tpl.html else
           f"<textarea id='본문칸' style='width:100%;height:320px'>"
           f"{html.escape(본문)}</textarea>")

    몸 = (
        "".join(경고)
        + "<div class='card'>"
        f"<h2>메일 쓰기 <span class='muted'>{html.escape(이름)}</span> "
        + ("<span class='pill p-내부'>내부</span>" if tpl.내부 else "")
        + "</h2>"
        f"<p class='muted'>템플릿 <b>{html.escape(tpl.이름)}</b> · "
        f"고친 내용 그대로 나가고, 그대로 기록에 남습니다.</p>"
        "<form method='post' action='/mail/send/one' id='쓰기폼'>"
        f"<input type='hidden' name='tpl' value='{tpl.id}'>"
        f"<input type='hidden' name='id' value='{html.escape(cid)}'>"
        "<input type='hidden' name='body' id='본문값'>"
        "<p><label class='rt-lbl' style='width:70px'>받는 사람</label>"
        f"<input type='text' name='to' value='{html.escape(받는사람)}'"
        " list='최근주소' style='width:calc(100% - 90px)'"
        + (" placeholder='면접관 주소를 적으세요 (쉼표로 여러 명)'" if tpl.내부 else "")
        + " required></p>"
        f"<datalist id='최근주소'>{제안}</datalist>"
        "<p><label class='rt-lbl' style='width:70px'>참조</label>"
        f"<input type='text' name='cc' value='{html.escape(참조)}'"
        " style='width:calc(100% - 90px)' placeholder='없으면 비워 두세요'></p>"
        "<p><label class='rt-lbl' style='width:70px'>제목</label>"
        f"<input type='text' name='subject' value='{html.escape(제목)}'"
        " style='width:calc(100% - 90px)' required></p>"
        "<p class='muted' style='margin-bottom:4px'>본문</p>"
        + 본문칸
        + "<div class='bar' style='margin-top:12px'>"
        "<b>지원자 자료 붙이기</b>"
        "<label class='rt-lbl'><input type='checkbox' name='cv' value='1'"
        + (" checked" if CV켬 else "") + (" disabled" if not 있는CV else "")
        + " onchange='this.form.submit()'> CV 원본"
        + ("" if 있는CV else " <span class='muted'>(없음)</span>") + "</label>"
        "<label class='rt-lbl'><input type='checkbox' name='cand' value='1'"
        + (" checked" if 첨부켬 else "") + (" disabled" if not 있는첨부 else "")
        + " onchange='this.form.submit()'> 지원자 첨부파일"
        + (f" <span class='muted'>({있는첨부}개)</span>" if 있는첨부
           else " <span class='muted'>(없음)</span>") + "</label>"
        "<span class='muted'>체크를 바꾸면 첨부 목록이 다시 계산됩니다</span></div>"
        f"<p class='muted' style='margin-top:8px'>붙어서 나갈 파일 "
        f"<b>{len(붙는것)}개</b> · 합계 {합 / 1024:.0f}KB</p>"
        f"<ul class='muted' style='margin:0 0 12px 18px'>{첨부줄}</ul>"
        "<p>"
        + ("<button type='submit' name='send' value='1'>보내기</button> "
           if 보낼수있나 else
           "<button type='submit' disabled>보내기</button> ")
        + "<button type='button' class='sec' onclick='window.close()'>닫기</button>"
        "</p></form></div>"
        + _DRAFT_JS
    )
    return _page(f"메일 쓰기 — {이름}", 몸, nav=False)


#: 작성창 안에서 도는 것. 본문을 숨은 칸에 옮겨 담고, 보낸 뒤 부모 목록을 고친다.
_DRAFT_JS = '\n<script>' + _정적JS("draft.js") + '</script>'


def _mail_sent_window(cid: str, 이름: str, 글: str) -> bytes:
    """작성창에서 보내고 난 뒤. 부모 목록을 고쳐 놓고 창을 닫는다."""
    return (
        "<!doctype html><meta charset='utf-8'><title>보냈습니다</title>"
        "<body style=\"font:14px system-ui;padding:24px\">"
        f"<p><b>{html.escape(이름)}</b> — {html.escape(글)}</p>"
        "<p style='color:#6b7280'>이 창은 곧 닫힙니다.</p>"
        "<script>"
        "try{ if(window.opener && !window.opener.closed && window.opener.보냄표시)"
        f"     window.opener.보냄표시('{html.escape(cid)}', '보냄'); }}catch(e){{}}"
        "setTimeout(function(){ window.close(); }, 700);"
        "</script></body>"
    ).encode("utf-8")


def _mail_request_preview(tpl) -> str:
    """실제로 API 에 보낼 URL 과 본문을 그대로 보여준다.

    '정말 나가는 게 맞나' 를 눈으로 확인할 유일한 방법이라, 토큰만 가리고
    나머지는 손대지 않는다.
    """
    결과 = mailapi.send("보낼주소@example.com", tpl.제목 or "(제목)",
                      tpl.본문 or "(본문)", html=tpl.html, 참조=tpl.cc(),
                      첨부=mailing.attachment_bytes(tpl.id), dry_run=True)
    본문 = 결과.본문
    if len(본문) > 4000:
        본문 = 본문[:4000] + " … (첨부가 커서 줄임)"
    토큰 = settings.mail_api_token
    가린토큰 = (토큰[:3] + "…" + str(len(토큰)) + "자") if 토큰 else "(비어 있음)"
    설정 = "".join(
        f"<tr><th>{이름}</th><td>{html.escape(값)}</td></tr>"
        for 이름, 값 in (
            ("MAIL_API_URL", settings.mail_api_url or "(비어 있음)"),
            ("MAIL_API_TOKEN", 가린토큰),
            ("MAIL_API_SYSTEM_ID", settings.mail_api_system_id or "(비어 있음)"),
            ("MAIL_API_USER_ID", settings.mail_api_user_id or "(비어 있음)"),
            ("MAIL_SENDER", settings.mail_sender or "(안 씀)"),
            ("MAIL_DRY_RUN", "1 (보내지 않음)" if settings.mail_dry_run else "0 (실제로 보냄)"),
        )
    )
    return (
        "<div class='card'><h2>보낼 요청 내용</h2>"
        "<p class='muted'>사내 API 명세와 이 내용을 맞춰 보세요. 필드 이름이나 인증 방식이"
        " 다르면 <code>cvtool/clients/mail.py</code> 의 <code>build_payload()</code> 만"
        " 고치면 됩니다.</p>"
        f"<table style='margin-bottom:12px'>{설정}</table>"
        "<p><b>POST</b> <code>" + html.escape(결과.요청URL or "(URL 이 비어 있음)") + "</code></p>"
        "<p><b>헤더</b> <code>Content-Type: application/json; charset=utf-8</code><br>"
        "<code>Authorization: Bearer &lt;MAIL_API_TOKEN&gt;</code></p>"
        "<p><b>본문</b> (JSON 을 문자열로 직렬화해 그대로 보냅니다)</p>"
        f"<pre style='white-space:pre-wrap;word-break:break-all;background:#f6f7f9;"
        f"border:1px solid var(--line);border-radius:6px;padding:12px;font-size:12px'>"
        f"{html.escape(본문)}</pre></div>"
    )


def _mail_test_page(tid: int, me: User, error: str = "", msg: str = "",
                    peek: bool = False) -> bytes:
    """메일 탭의 템플릿 확인 화면 — **시험 발송까지만** 한다.

    예전에는 여기서 지원자 전원을 늘어놓고 골라 보냈다. 서류 합격 안내를
    보내려는데 이 화면에서는 누가 서류 합격인지 알 수가 없었다. 실제 발송은
    **인재 Pool·채용 현황**에서 거른 뒤 고른 사람에게 하도록 옮겼다.
    """
    tpl = mailing.template(tid)
    if tpl is None:
        return _page("없음", "<div class='card'>템플릿을 찾을 수 없습니다.</div>", me=me)

    첨부 = mailing.attachments(tpl.id)
    딸림 = []
    if tpl.cc():
        딸림.append("참조 <b>" + html.escape(", ".join(tpl.cc())) + "</b>")
    if 첨부:
        딸림.append("첨부 <b>"
                  + html.escape(", ".join(a["파일명"] for a in 첨부)) + "</b>")
    if mailing.used_body_images(tpl.본문):
        딸림.append(f"본문 그림 <b>{tpl.그림보내기}</b>")
    딸림칸 = f"<p class='muted'>{' · '.join(딸림)}</p>" if 딸림 else ""

    # 보기용 값으로 한 사람 몫을 채워 본다 (자리표시자가 제대로 도는지 확인)
    진행맵 = recruit.all()
    records = store.list_all()
    값 = _mail_vars(records[0], 진행맵) if records else {}
    값 = {k: (v or f"(예시){k}") for k, v in 값.items()}
    for 변수 in _mail_var_names():
        값.setdefault(변수, f"(예시){변수}")
    제목미리, _ = render(tpl.제목, 값)
    본문미리, 빈칸 = render(tpl.본문, 값)

    알림 = _알림(msg=msg)
    오류 = _알림(err=error)
    연습 = (
        "<div class='warn'><b>연습 모드 (MAIL_DRY_RUN=1)</b> — 실제로 나가지 "
        "않습니다.</div>" if settings.mail_dry_run else ""
    )
    탈락표시 = (
        "<div class='warn'><b>이 템플릿은 탈락 메일입니다.</b> 보내고 나면 그 "
        "지원자에게는 이후 어떤 메일도 보낼 수 없습니다.</div>"
        if tpl.탈락메일 else ""
    )
    시험 = (
        "<div class='card'><h2>시험 발송</h2>"
        "<form method='post' action='/mail/test' style='display:flex;gap:8px;flex-wrap:wrap'>"
        f"<input type='hidden' name='id' value='{tpl.id}'>"
        "<input type='text' name='to' placeholder='내 주소를 넣으세요' required"
        " style='width:280px'>"
        "<button type='submit'>이 주소로 한 통 보내보기</button></form>"
        "<p class='muted'>지원자에게는 가지 않고 <b>발송 기록도 남지 않습니다.</b> "
        "설정이 맞는지 먼저 이걸로 확인하세요. "
        + ("지금은 연습 모드라 실제로 나가지 않고 요청 내용만 보여줍니다."
           if settings.mail_dry_run else
           "<b>지금은 실제로 나갑니다 (MAIL_DRY_RUN=0).</b>")
        + f" <a href='/mail/test?id={tpl.id}&peek=1'>보낼 요청 내용 보기</a></p></div>"
        if can(me, "메일_발송") else ""
    )
    점검 = _mail_request_preview(tpl) if peek else ""
    미리 = (
        f"<div class='card'><h2>보기용 미리보기</h2>"
        f"<p class='muted'>제목: {html.escape(제목미리)}</p>"
        + (f"<div class='mailbody'>{본문미리}</div>" if tpl.html
           else f"<pre class='rubric'>{html.escape(본문미리)}</pre>")
        + "<p class='muted'>실제 값이 아니라 <b>(예시)</b> 로 채운 화면입니다. "
        "누구에게 무엇이 나가는지는 보낼 때 하나씩 확인합니다.</p></div>"
    )
    return _page(
        f"{tpl.이름} 확인",
        알림 + 오류 + 연습 + 탈락표시
        + f"<div class='card'><h2>{html.escape(tpl.이름)} "
        f"<span class='muted'>{html.escape(tpl.제목)}</span></h2>" + 딸림칸
        + ("<p class='flag'>값이 빈 자리표시자: "
           + html.escape(", ".join(빈칸)) + "</p>" if 빈칸 else "")
        + "<div class='warn'><b>여기서는 실제 지원자에게 못 보냅니다.</b> "
        "보낼 사람은 <a href='/'>인재 Pool</a> 이나 "
        "<a href='/recruit'>채용 현황</a> 에서 고릅니다 — 거기서는 "
        "<b>누가 서류 합격인지 보면서</b> 고를 수 있습니다. "
        "표에서 체크한 뒤 <b>선택한 사람에게 메일</b> 을 누르세요.</div>"
        + f"<p><a class='btn sec' href='/mail/template?id={tpl.id}'>템플릿 고치기</a> "
        "<a class='btn sec' href='/mail'>목록</a></p></div>"
        + 시험 + 점검 + 미리,
        me=me,
    )


def _mail_log_page(me: User, tid: int = 0) -> bytes:
    """발송 이력. 템플릿을 하나 고르면 **안 보낸 사람**까지 같이 보여준다.

    예전에는 모든 템플릿의 기록이 한 덩어리로 쏟아져서 "이 안내를 누가 받았나"
    를 볼 수가 없었다. 이제 위에 템플릿 단추를 두고, 고른 템플릿의 것만 본다.

    그리고 **보냈어야 하는데 안 보낸 사람**을 위에 올린다. 기록을 보러 오는
    까닭이 대개 "빠뜨린 게 없나" 이기 때문이다. 목록만 보여 주고 다시 인재
    Pool 로 가서 그 사람을 찾게 하면 결국 안 쓰게 되므로, 체크해서 그 자리에서
    작성창으로 넘어간다.
    """
    templates = mailing.templates()
    tpl = next((t for t in templates if t.id == tid), None)
    if tpl is None:
        tid = 0
    안보낸수 = _안보낸수(me)
    이름맵 = {r.지원자_ID: (r.한글_이름 or r.영문_이름 or r.지원자_ID)
             for r in store.list_all()}

    # -- 템플릿 고르는 단추 줄 -------------------------------------------------
    def 단추(라벨: str, 주소: str, 켜짐: bool, 안보낸: int = 0) -> str:
        배지 = f" <span class='pill p-안본것'>{안보낸}</span>" if 안보낸 else ""
        켬 = " style='border-color:var(--accent);color:var(--accent);font-weight:700'" if 켜짐 else ""
        return f"<a class='btn sec' href='{주소}'{켬}>{html.escape(라벨)}{배지}</a> "

    고르기 = 단추("전체", "/mail/log", tid == 0, sum(안보낸수.values()))
    고르기 += "".join(
        단추(t.이름, f"/mail/log?tpl={t.id}", t.id == tid, 안보낸수.get(t.id, 0))
        for t in templates
    )

    # -- 안 보낸 사람 ----------------------------------------------------------
    빠진것 = ""
    if tpl is not None:
        if not tpl.조건들:
            빠진것 = (
                "<div class='card'><h2>보내야 하는 때가 안 정해져 있습니다</h2>"
                "<p class='muted'>이 템플릿을 <b>어떤 채용 상태일 때</b> 보내는지"
                " 정해 두면, 그 상태가 된 사람 중에 아직 못 받은 사람을 여기서"
                " 찾아 드립니다.</p>"
                f"<p><a class='btn sec' href='/mail/template?id={tpl.id}'>"
                "템플릿 편집으로</a></p></div>"
            )
        else:
            남은것 = _안보낸것(tpl, me)
            조건표시 = " · ".join(html.escape(c) for c in tpl.조건들)
            if not 남은것:
                빠진것 = (
                    "<div class='card'><h2>빠진 사람 없습니다</h2>"
                    f"<p class='muted'>보내야 하는 때: <b>{조건표시}</b> — "
                    "여기 해당하는 사람은 모두 이 메일을 받았습니다.</p></div>"
                )
            else:
                줄 = "".join(
                    f"<tr><td><label><input type='checkbox' name='ids'"
                    f" value='{html.escape(cid)}' checked> "
                    f"{html.escape(이름맵.get(cid, cid))}</label></td>"
                    f"<td>{html.escape(조건)}</td>"
                    f"<td class='muted'>{html.escape(recruit.get(cid).채용시작일시[:10])}</td>"
                    f"<td><a href='/candidate?id={html.escape(cid)}'>상세</a></td></tr>"
                    for cid, 조건 in sorted(
                        남은것.items(), key=lambda kv: 이름맵.get(kv[0], kv[0]))
                )
                빠진것 = (
                    "<div class='card'>"
                    f"<h2>안 보낸 사람 <span class='pill p-안본것'>{len(남은것)}</span></h2>"
                    f"<p class='muted'>보내야 하는 때: <b>{조건표시}</b>. "
                    "지금 이 상태인데 이 메일을 아직 못 받은 사람입니다. "
                    "이미 받은 사람과 탈락 메일을 받은 사람은 빠져 있습니다.</p>"
                    "<form method='post' action='/mail/compose'>"
                    f"<input type='hidden' name='template' value='{tpl.id}'>"
                    f"<input type='hidden' name='back' value='/mail/log?tpl={tpl.id}'>"
                    "<div class='scroll'><table data-name='안 보낸 사람'>"
                    "<tr><th>지원자</th><th>지금 상태</th><th>채용 시작</th><th></th></tr>"
                    + 줄 + "</table></div>"
                    + ("<p><button type='submit'>고른 사람에게 이 메일 쓰기</button>"
                       "<span class='muted'> 한 통씩 작성창이 떠서 확인하고 보냅니다."
                       " 여기서 바로 나가지 않습니다.</span></p>"
                       if can(me, "메일_발송") else
                       "<p class='muted'>메일 발송 권한이 있는 사람이 보낼 수 있습니다.</p>")
                    + "</form></div>"
                )

    # -- 보낸 기록 -------------------------------------------------------------
    기록 = mailing.history(limit=500, template_id=tid)
    탈락배지 = " <span class='pill p-미분류'>탈락</span>"
    rows = "".join(
        f"<tr><td>{html.escape(r['보낸일시'])}</td>"
        f"<td>{html.escape(이름맵.get(r['지원자_ID'], r['지원자_ID']))}</td>"
        f"<td>{html.escape(r['받는사람'])}</td>"
        + ("<td><span class='pill p-내부'>내부</span></td>"
           if (r.get('받는대상') or DEFAULT_RECIPIENT) == '내부'
           else "<td class='muted'>지원자</td>")
        + f"<td>{html.escape(r['템플릿이름'])}"
        f"{탈락배지 if r['탈락메일'] else ''}</td>"
        f"<td>{html.escape(r['상태'])}</td>"
        f"<td class='muted' title='{html.escape(r['오류'] or '')}'>"
        f"{html.escape(r['오류'] or '')}</td>"
        f"<td class='muted'>{html.escape(r['보낸이'])}</td></tr>"
        for r in 기록
    ) or "<tr><td colspan='8' class='muted'>보낸 메일이 없습니다.</td></tr>"

    제목 = f"{tpl.이름} 발송 이력" if tpl else "메일 발송 이력"
    return _page(
        제목,
        "<div class='card'><h2>템플릿</h2>"
        f"<p style='display:flex;flex-wrap:wrap;gap:4px 0'>{고르기}</p>"
        "<p class='muted'>숫자는 <b>보냈어야 하는데 안 보낸 사람 수</b>입니다."
        " 템플릿 편집에서 &lt;보내야 하는 때&gt;를 정한 것만 셉니다.</p>"
        "<p><a class='btn sec' href='/mail'>템플릿 목록</a></p></div>"
        + 빠진것
        + f"<div class='card'><h2>보낸 기록 <span class='muted'>총 "
        f"{mailing.count(tid)}건</span></h2>"
        "<div class='scroll'><table data-name='메일 발송 이력'>"
        "<tr><th>보낸 일시</th><th>지원자</th><th>받는 주소</th><th>구분</th>"
        "<th>템플릿</th><th>상태</th><th>메모</th><th>보낸 사람</th></tr>"
        + rows + "</table></div></div>",
        me=me,
    )


@라우트("GET", '/mail', 권한='메일_템플릿')
def get_mail(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return self._send(_mail_page(me, (params.get("err") or [""])[0],
                                 (params.get("msg") or [""])[0]))
    return _없는주소(self)


@라우트("GET", '/mail/template', 권한='메일_템플릿')
def get_mail_template(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    try:
        tid = int((params.get("id") or ["0"])[0])
    except ValueError:
        return self._redirect("/mail")
    return self._send(_mail_template_page(tid, me, (params.get("err") or [""])[0],
                                          (params.get("msg") or [""])[0]))
    return _없는주소(self)


@라우트("GET", '/mail/draft', 권한='메일_발송', 거부말='메일 발송 권한이 없습니다.')
def get_mail_draft(self, me, path):
    # 메일 한 통을 쓰는 창. 발송 목록에서 새 창으로 띄운다.
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    try:
        tid = int((params.get("tpl") or ["0"])[0])
    except ValueError:
        return self._redirect("/mail")
    cid = (params.get("id") or [""])[0]
    if not _볼수있는지원자(cid, me):
        return self._deny("이 지원자에게는 보낼 수 없습니다.")
    return self._send(_mail_draft_page(tid, cid, me,
                                       (params.get("err") or [""])[0]))
    return _없는주소(self)


@라우트("GET", '/mail/test', 권한='메일_템플릿')
def get_mail_test(self, me, path):
    # 메일 탭에서는 **시험 발송까지만** 한다. 실제 발송은 인재 Pool·
    # 채용 현황에서 대상을 고른 뒤에 한다.
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    try:
        tid = int((params.get("id") or ["0"])[0])
    except ValueError:
        return self._redirect("/mail")
    return self._send(_mail_test_page(tid, me, (params.get("err") or [""])[0],
                                      (params.get("msg") or [""])[0],
                                      peek=bool(params.get("peek"))))
    return _없는주소(self)


@라우트("GET", '/mail/attachment', 권한='메일_템플릿')
def get_mail_attachment(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    try:
        att = mailing.attachment(int((params.get("id") or ["0"])[0]))
    except (ValueError, TypeError):
        att = None
    if not att:
        return self._send(_page("없음", "<div class='card'>첨부를 찾을 수 없습니다.</div>"),
                          code=404)
    path_ = mailing.files_dir / att["저장명"]
    if not path_.is_file():
        return self._send(_page("없음", "<div class='card'>파일이 없습니다.</div>"),
                          code=404)
    이름 = urllib.parse.quote(att["파일명"])
    return self._send(
        path_.read_bytes(),
        CONTENT_TYPES.get(Path(att["저장명"]).suffix.lower(),
                          "application/octet-stream"),
        extra={"Content-Disposition":
               f"attachment; filename=\"file\"; filename*=UTF-8''{이름}"},
    )
    return _없는주소(self)


@라우트("GET", '/mail/image', 권한='메일_템플릿')
def get_mail_image(self, me, path):
    # 본문 그림. 편집기·미리보기·발송 이력이 이 주소로 그림을 본다.
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    try:
        img = mailing.body_image(int((params.get("id") or ["0"])[0]))
    except (ValueError, TypeError):
        img = None
    내용 = mailing.body_image_bytes(img["id"]) if img else None
    if 내용 is None:
        return self._send(b"", "image/png", code=404)
    return self._send(
        내용,
        CONTENT_TYPES.get(Path(img["저장명"]).suffix.lower(), "image/png"),
        extra={"Cache-Control": "private, max-age=600"},
    )
    return _없는주소(self)


@라우트("GET", '/mail/log', 권한='메일_템플릿')
def get_mail_log(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    try:
        로그tid = int((params.get("tpl") or ["0"])[0])
    except ValueError:
        로그tid = 0
    return self._send(_mail_log_page(me, 로그tid))
    return _없는주소(self)


@라우트("POST", '/mail/template/add', 권한='메일_템플릿')
def post_mail_template_add(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    이름 = (data.get("name") or [""])[0]
    탈락 = bool(data.get("reject"))
    대상 = (data.get("to") or [DEFAULT_RECIPIENT])[0]
    if 대상 not in RECIPIENT_KINDS:
        대상 = DEFAULT_RECIPIENT
    try:
        tid = mailing.add_template(이름, 탈락메일=탈락, 만든이=me.아이디,
                                   본문형식="HTML", 받는대상=대상)
    except ValueError as exc:
        return self._redirect("/mail?err=" + urllib.parse.quote(str(exc)))
    audit.record(me.아이디, "메일", 이름,
                 비고="템플릿 추가" + (" (탈락 메일)" if 탈락 else "")
                 + (" (내부 메일)" if 대상 == "내부" else ""))
    return self._redirect(f"/mail/template?id={tid}")
    return _없는주소(self)


@라우트("POST", '/mail/template/save', 권한='메일_템플릿')
def post_mail_template_save(self, me, path):
    data = urllib.parse.parse_qs(
        self._read_body().decode("utf-8", "replace"), keep_blank_values=True
    )
    try:
        tid = int((data.get("id") or ["0"])[0])
    except ValueError:
        return self._redirect("/mail")
    옛 = mailing.template(tid)
    if 옛 is None:
        return self._redirect("/mail")
    try:
        새것 = mailing.update_template(
            tid,
            이름=(data.get("name") or [None])[0],
            제목=(data.get("subject") or [""])[0],
            본문=(data.get("body") or [""])[0],
            탈락메일=bool(data.get("reject")),
            참조=(data.get("cc") or [""])[0],
            그림방식=(data.get("imgmode") or [None])[0],
            받는대상=(data.get("to") or [None])[0],
            CV첨부=bool(data.get("cvattach")),
            지원자첨부=bool(data.get("candattach")),
            발송조건=_고른조건(data.get("when") or []),
        )
    except ValueError as exc:
        return self._redirect(f"/mail/template?id={tid}&err="
                              + urllib.parse.quote(str(exc)))
    변경 = [
        (항목, 옛값, 새값)
        for 항목, 옛값, 새값 in (
            ("이름", 옛.이름, 새것.이름),
            ("참조", 옛.참조, 새것.참조),
            ("제목", 옛.제목, 새것.제목),
            ("본문", 옛.본문, 새것.본문),
            ("탈락메일", "Y" if 옛.탈락메일 else "", "Y" if 새것.탈락메일 else ""),
            ("받는 사람", 옛.받는대상, 새것.받는대상),
            ("CV 첨부", "Y" if 옛.CV첨부 else "", "Y" if 새것.CV첨부 else ""),
            ("지원자 첨부", "Y" if 옛.지원자첨부 else "",
             "Y" if 새것.지원자첨부 else ""),
            ("보내야 하는 때", ", ".join(옛.조건들), ", ".join(새것.조건들)),
        )
        if 옛값 != 새값
    ]
    for 항목, 옛값, 새값 in 변경:
        # 본문 전체를 이력에 남기면 읽기 어려워서 바뀐 사실만 남긴다
        if 항목 == "본문":
            audit.record(me.아이디, "메일", 새것.이름, 항목="본문", 비고="본문 수정")
        else:
            audit.record(me.아이디, "메일", 새것.이름, 항목=항목,
                         이전값=옛값, 새값=새값)
    메시지 = "저장했습니다." if 변경 else "바뀐 내용이 없습니다."
    return self._redirect(f"/mail/template?id={tid}&msg="
                          + urllib.parse.quote(메시지))
    return _없는주소(self)


@라우트("POST", '/mail/template/delete', 권한='메일_템플릿')
def post_mail_template_delete(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    try:
        이름 = mailing.delete_template(int((data.get("id") or ["0"])[0]))
    except ValueError:
        이름 = ""
    if 이름:
        audit.record(me.아이디, "메일", 이름, 비고="템플릿 삭제 (발송 기록은 유지)")
    return self._redirect("/mail?msg=" + urllib.parse.quote(
        f"'{이름}' 템플릿을 지웠습니다." if 이름 else "지울 템플릿이 없습니다."))
    return _없는주소(self)


@라우트("POST", '/mail/test', 권한='메일_발송')
def post_mail_test(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    try:
        tid = int((data.get("id") or ["0"])[0])
    except (ValueError, TypeError):
        return self._redirect("/mail")
    tpl = mailing.template(tid)
    if tpl is None:
        return self._redirect("/mail")
    주소 = (data.get("to") or [""])[0].strip()
    뒤로 = f"/mail/test?id={tid}"
    if not 주소:
        return self._redirect(뒤로 + "&err=" + urllib.parse.quote(
            "시험 발송할 주소를 넣으세요."))

    # 실제 지원자 한 명의 값으로 채운다 (없으면 보기용 값)
    진행맵 = recruit.all()
    records = store.list_all()
    값 = _mail_vars(records[0], 진행맵) if records else {}
    값 = {k: (v or f"(예시){k}") for k, v in 값.items()}
    for 변수 in _mail_var_names():
        값.setdefault(변수, f"(예시){변수}")
    제목, _ = render(tpl.제목, 값)
    본문, _ = render(tpl.본문, 값)
    try:
        결과 = mailapi.send(주소, f"[시험] {제목}", 본문, html=tpl.html,
                          참조=tpl.cc(),
                          첨부=mailing.attachment_bytes(tpl.id))
    except mailapi.MailError as exc:
        audit.record(me.아이디, "메일", tpl.이름,
                     비고=f"시험 발송 실패 ({주소})")
        return self._redirect(뒤로 + "&err=" + urllib.parse.quote(str(exc)))
    # 시험 발송은 **지원자 발송 기록에 남기지 않는다.**
    # 남기면 그 지원자에게 진짜로 못 보내게 된다.
    audit.record(me.아이디, "메일", tpl.이름,
                 비고=f"시험 발송 {'성공' if 결과.보냄 else '(연습 모드)'} → {주소}")
    if 결과.보냄:
        알림 = (f"{주소} 로 보냈습니다. API 응답 HTTP {결과.상태코드}: "
              f"{결과.응답[:200] or '(본문 없음)'}")
    else:
        알림 = ("연습 모드(MAIL_DRY_RUN=1)라 보내지 않았습니다. "
              "아래 '보낼 요청 내용' 에서 형식을 확인하세요.")
    return self._redirect(뒤로 + "&msg=" + urllib.parse.quote(알림)
                          + "&peek=1")
    return _없는주소(self)


@라우트("POST", '/mail/image/add', 권한='메일_템플릿', json=True)
def post_mail_image_add(self, me, path):
    # 편집기가 그림을 넣을 때 부른다. 본문에 base64 를 박는 대신
    # 파일로 보관하고 짧은 참조만 돌려준다.
    form = parse_multipart(self._read_body(), self.headers.get("Content-Type", ""))
    try:
        tid = int(form.fields.get("template", "0"))
    except (ValueError, TypeError):
        tid = 0
    if mailing.template(tid) is None:
        return self._json({"ok": False, "error": "템플릿을 찾을 수 없습니다."},
                          code=404)
    f = form.files[0] if form.files else None
    if f is None or not f.filename:
        return self._json({"ok": False, "error": "그림 파일이 없습니다."}, code=400)
    try:
        img_id = mailing.add_body_image(tid, f.filename, f.content,
                                        올린이=me.아이디)
    except ValueError as exc:
        return self._json({"ok": False, "error": str(exc)}, code=400)
    audit.record(me.아이디, "메일", str(tid), 항목="본문 그림 추가",
                 새값=f.filename)
    return self._json({"ok": True, "id": img_id,
                       "src": f"/mail/image?id={img_id}"})
    return _없는주소(self)


@라우트("POST", '/mail/image/delete', 권한='메일_템플릿')
def post_mail_image_delete(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    tid = (data.get("template") or ["0"])[0]
    try:
        이름 = mailing.delete_body_image(int((data.get("id") or ["0"])[0]))
    except (ValueError, TypeError):
        이름 = ""
    if 이름:
        audit.record(me.아이디, "메일", tid, 항목="본문 그림 삭제", 이전값=이름)
    return self._redirect(f"/mail/template?id={tid}&msg="
                          + urllib.parse.quote("본문에서 쓰지 않는 그림을 지웠습니다."
                                               if 이름 else "그림을 찾을 수 없습니다."))
    return _없는주소(self)


@라우트("POST", '/mail/attachment/add', 권한='메일_템플릿')
def post_mail_attachment_add(self, me, path):
    form = parse_multipart(self._read_body(), self.headers.get("Content-Type", ""))
    try:
        tid = int(form.fields.get("template", "0"))
    except (ValueError, TypeError):
        return self._redirect("/mail")
    뒤로 = f"/mail/template?id={tid}"
    if mailing.template(tid) is None:
        return self._redirect("/mail")
    붙임, 실패 = [], ""
    for f in form.files:
        if not f.filename:
            continue
        try:
            mailing.add_attachment(tid, f.filename, f.content, 올린이=me.아이디)
            붙임.append(f.filename)
        except ValueError as exc:
            실패 = 실패 or str(exc)
    for 이름 in 붙임:
        audit.record(me.아이디, "메일", str(tid), 항목="첨부 추가", 새값=이름)
    if 실패:
        return self._redirect(f"{뒤로}&err=" + urllib.parse.quote(실패))
    if not 붙임:
        return self._redirect(f"{뒤로}&err="
                              + urllib.parse.quote("붙일 파일을 고르세요."))
    return self._redirect(f"{뒤로}&msg=" + urllib.parse.quote(
        f"{len(붙임)}개 붙였습니다: {', '.join(붙임)}"))
    return _없는주소(self)


@라우트("POST", '/mail/attachment/delete', 권한='메일_템플릿')
def post_mail_attachment_delete(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    try:
        tid = int((data.get("template") or ["0"])[0])
        이름 = mailing.delete_attachment(int((data.get("id") or ["0"])[0]))
    except (ValueError, TypeError):
        return self._redirect("/mail")
    if 이름:
        audit.record(me.아이디, "메일", str(tid), 항목="첨부 삭제", 이전값=이름)
    return self._redirect(f"/mail/template?id={tid}")
    return _없는주소(self)


@라우트("POST", '/mail/compose', 권한='메일_발송', 거부말='메일 발송 권한이 없습니다.')
def post_mail_compose(self, me, path):
    # 인재 Pool·채용 현황에서 고른 사람을 데리고 오는 입구.
    data = urllib.parse.parse_qs(
        self._read_body().decode("utf-8", "replace"), keep_blank_values=True
    )
    ids = [x for x in (data.get("ids") or []) if x.strip()]
    뒤로 = (data.get("back") or ["/"])[0] or "/"
    if not 뒤로.startswith("/"):
        뒤로 = "/"
    try:
        tid = int((data.get("template") or ["0"])[0] or 0)
    except ValueError:
        tid = 0
    return self._send(_mail_compose_page(ids, tid, me, 뒤로))
    return _없는주소(self)


@라우트("POST", '/mail/send/one', 권한='메일_발송', 거부말='메일 발송 권한이 없습니다.')
def post_mail_send_one(self, me, path):
    # 작성창에서 한 통. 고친 내용 그대로 보내고 그대로 기록한다.
    data = urllib.parse.parse_qs(
        self._read_body().decode("utf-8", "replace"), keep_blank_values=True
    )
    try:
        tid = int((data.get("tpl") or ["0"])[0])
    except ValueError:
        return self._redirect("/mail")
    cid = (data.get("id") or [""])[0]
    tpl = mailing.template(tid)
    if tpl is None or not _볼수있는지원자(cid, me):
        return self._deny("보낼 수 없는 요청입니다.")
    넣은값 = {
        "to": (data.get("to") or [""])[0].strip(),
        "cc": (data.get("cc") or [""])[0].strip(),
        "subject": (data.get("subject") or [""])[0],
        "body": (data.get("body") or [""])[0],
        "cv": bool(data.get("cv")),
        "cand": bool(data.get("cand")),
    }
    # 첨부 체크만 바꾼 것 — 보내지 않고 다시 그린다
    if not data.get("send"):
        return self._send(_mail_draft_page(tid, cid, me, 넣은값=넣은값))

    rec = store.get(cid)
    이름 = _mail_vars(rec).get("한글_이름") or cid
    막힘 = mailing.blocked_reason(cid, tpl)
    if 막힘:
        return self._send(_mail_draft_page(tid, cid, me, 막힘, 넣은값))
    if not 넣은값["to"]:
        return self._send(_mail_draft_page(
            tid, cid, me, "받는 사람을 적으세요.", 넣은값))

    자료, 자료오류 = _지원자자료(cid, 넣은값["cv"], 넣은값["cand"])
    if 자료오류:
        return self._send(_mail_draft_page(tid, cid, me, 자료오류, 넣은값))
    참조 = split_addresses(넣은값["cc"])
    첨부파일 = mailing.attachment_bytes(tpl.id) + 자료
    보낼본문, 그림첨부 = mailing.prepare_body(넣은값["body"], tpl.그림보내기)
    첨부이름 = ", ".join([n for n, _ in 첨부파일 + 그림첨부])
    try:
        결과 = mailapi.send(넣은값["to"], 넣은값["subject"], 보낼본문,
                          html=tpl.html, 참조=참조,
                          첨부=첨부파일 + 그림첨부)
    except mailapi.MailError as exc:
        mailing.record(cid, tpl, 넣은값["to"], 넣은값["subject"],
                       넣은값["body"], "실패", 오류=str(exc),
                       보낸이=me.아이디, 참조=", ".join(참조), 첨부=첨부이름)
        return self._send(_mail_draft_page(
            tid, cid, me, f"보내지 못했습니다: {exc}", 넣은값))
    상태 = "성공" if 결과.보냄 else "발송안함"
    메모 = f"HTTP {결과.상태코드} {결과.응답[:300]}".strip()
    mailing.record(cid, tpl, 넣은값["to"], 넣은값["subject"], 넣은값["body"],
                   상태, 오류="" if 결과.보냄 else 메모,
                   보낸이=me.아이디, 참조=", ".join(참조), 첨부=첨부이름)
    audit.record(me.아이디, "메일", cid, 항목=tpl.이름,
                 비고=f"{tpl.받는대상} 발송 → {넣은값['to']} ({상태})")
    글 = "보냈습니다" if 결과.보냄 else "연습 모드라 나가지 않았습니다"
    return self._send(_mail_sent_window(cid, 이름, 글))
    return _없는주소(self)


@라우트("POST", '/mail/send', 권한='메일_발송')
def post_mail_send(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    try:
        tid = int((data.get("template") or data.get("id") or ["0"])[0])
    except ValueError:
        return self._redirect("/mail")
    tpl = mailing.template(tid)
    if tpl is None:
        return self._redirect("/mail")
    돌아갈곳 = (data.get("back") or ["/"])[0] or "/"
    if not 돌아갈곳.startswith("/"):
        돌아갈곳 = "/"
    ids = [x for x in (data.get("ids") or []) if x.strip()]
    if not ids:
        return self._send(_mail_compose_page(
            ids, tid, me, 돌아갈곳, "보낼 지원자를 하나 이상 고르세요."))

    # 안전장치: 보낼 인원수를 사람이 직접 쳐야 한다. 화면을 그린 뒤에
    # 상황이 바뀌었을 수 있으니 **지금 다시 세어** 그 수와 맞춰 본다.
    갈사람, _막힌 = _mail_targets(ids, tpl, me)
    친것 = (data.get("confirm") or [""])[0].strip()
    if 친것 != str(len(갈사람)):
        return self._send(_mail_compose_page(
            ids, tid, me, 돌아갈곳,
            f"보낼 인원수({len(갈사람)})를 그대로 쳐 넣어야 나갑니다. "
            f"넣은 값: {친것 or '(빈칸)'}"
            + ("" if 친것 else "")
        ))
    뒤로 = 돌아갈곳

    진행맵 = recruit.all()
    보이는 = auth.visible_project_ids(me)
    참조 = tpl.cc()
    첨부파일 = mailing.attachment_bytes(tpl.id)
    그림이름 = [f"{i['id']}_{i['파일명']}"
             for i in mailing.used_body_images(tpl.본문)
             if tpl.그림보내기 in ("본문+첨부", "첨부만")]
    # 붙는 파일 이름은 사람마다 달라진다 (지원자 자료). 보낼 때 만든다.
    성공, 실패, 건너뜀 = 0, 0, 0
    첫오류 = ""
    for cid in ids:
        rec = store.get(cid)
        if rec is None:
            건너뜀 += 1
            continue
        if 보이는 is not None and recruit.get(cid).project_id not in 보이는:
            건너뜀 += 1
            continue
        # 화면을 그린 뒤에 상황이 바뀌었을 수 있다. 보내기 직전에 다시 본다.
        막힘 = mailing.blocked_reason(cid, tpl)
        if 막힘:
            건너뜀 += 1
            continue
        값 = _mail_vars(rec, 진행맵)
        받는사람 = (값.get("이메일") or "").split(MULTI_SEP)[0].strip()
        제목, 빈1 = render(tpl.제목, 값)
        본문, 빈2 = render(tpl.본문, 값)
        if not 받는사람:
            건너뜀 += 1
            continue
        # 빈 자리표시자는 빈 채로 나간다 (화면에서 이미 알렸다).
        # 본문 그림을 실제로 실을 모양으로 바꾼다. 이력에는 **참조가 든
        # 본문**을 남긴다 — 나중에 다시 열어도 우리 DB 로 그림이 보인다.
        보낼본문, 그림첨부 = mailing.prepare_body(본문, tpl.그림보내기)
        # 사람마다 다른 파일이라 여기서 읽는다. 너무 크면 그 사람만 건너뛴다.
        자료, 자료오류 = _지원자자료(cid, tpl.CV첨부, tpl.지원자첨부)
        if 자료오류:
            실패 += 1
            첫오류 = 첫오류 or f"{cid}: {자료오류}"
            continue
        이번첨부 = ", ".join([n for n, _ in 첨부파일 + 자료] + 그림이름)
        try:
            결과 = mailapi.send(받는사람, 제목, 보낼본문, html=tpl.html,
                              참조=참조, 첨부=첨부파일 + 자료 + 그림첨부)
        except mailapi.MailError as exc:
            실패 += 1
            첫오류 = 첫오류 or str(exc)
            mailing.record(cid, tpl, 받는사람, 제목, 본문, "실패",
                           오류=str(exc), 보낸이=me.아이디,
                           참조=", ".join(참조), 첨부=이번첨부)
            continue
        상태 = "성공" if 결과.보냄 else "발송안함"
        성공 += 1
        # API 응답을 그대로 남긴다. HTTP 200 이어도 본문에 실패가 적혀 오는
        # API 가 있어서, 사람이 눈으로 확인할 수 있어야 한다.
        메모 = (f"HTTP {결과.상태코드} {결과.응답[:300]}".strip()
              if 결과.보냄 else 결과.응답)
        mailing.record(cid, tpl, 받는사람, 제목, 본문, 상태,
                       오류=메모, 보낸이=me.아이디,
                       참조=", ".join(참조), 첨부=이번첨부)
        audit.record(me.아이디, "메일", cid, 항목=tpl.이름,
                     새값=상태, 비고=f"{받는사람} 로 발송")

    조각 = [f"'{tpl.이름}' 을 {성공}명에게 보냈습니다"]
    if 실패:
        조각.append(f"{실패}명 실패 ({첫오류[:80]})")
    if 건너뜀:
        조각.append(f"{건너뜀}명은 보낼 수 없어 건너뛰었습니다")
    이음 = "&" if "?" in 뒤로 else "?"
    return self._redirect(뒤로 + 이음 + "msg="
                          + urllib.parse.quote(" / ".join(조각)))
    return _없는주소(self)
