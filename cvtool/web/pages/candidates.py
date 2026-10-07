"""인재 Pool(지원자 목록) · 지원자 추가(업로드) · 지원자 상세 · 표에서 바로 고치기.
"""

from __future__ import annotations

import html
import urllib.parse
import uuid
from pathlib import Path

from ... import bulk, edit, normalize as N, review
from ...auth import can, User
from ...config import settings
from ...dedup import find_duplicates
from ...edit import (
    ConflictError, custom_field_spec, edit_field, field_spec, READONLY_FIELDS,
    REGISTRY_FIELDS, validate, validate_custom, ValidationError, 보이는값, 사전_따라가기,
)
from ...export import records_to_xlsx
from ...fsutil import safe_filename
from ...mailing import DEFAULT_RECIPIENT
from ...matching import SCORE_RUBRIC
from ...names import observe_record
from ...normalize import MULTI_SEP
from ...recruit import RECRUIT_COLUMNS, STARTED_COLUMN
from ...schemas import (
    columns as table_columns, CVRecord, is_tier_venue, NAME_COLUMNS, Paper, Patent,
    게재상태_ENUM, 저자구분_ENUM, 특허상태_ENUM, 특허지역_ENUM,
)
from ...store import SUPPORTED_SUFFIXES
from ...timeutil import now_kst
from ...xlsx_read import XlsxError
from .dashboard import _프로필값
from .match import _등급이름, _점수색

from ..state import audit, auth, CONTENT_TYPES, mailing, recruit, registry, store
from ..jobs import _enqueue, _set_status, _status, _status_lock
from ..columns import (
    열이름도움,
    _cell, _editable, _tsv_to_xlsx, _볼수있나, _표값맵, MAIL_COLUMN, MANAGE_COLUMNS, 라벨, 머리글, 열폭,
    추가열, 엑셀열, 표열,
)
from ..layout import _busy_count, _page, _status_table, _알림, _없는주소, _정적JS, 홈
from ..router import 라우트, 로그인만
from ..multipart import parse_multipart


def _시작하기(me: User) -> str:
    """지원자가 한 명도 없을 때 — 무엇부터 하면 되는지 세 걸음으로."""
    올리기 = ("<a class='btn' href='/upload'>CV 올리러 가기</a>"
            if can(me, "지원자_등록") else "")
    return (
        "<div class='onboard'><h2>처음이시군요. 이렇게 시작하면 돼요.</h2>"
        "<ol class='obsteps'>"
        "<li><b>CV 올리기</b><span>오른쪽 위 <b>+ 지원자 추가</b> 에서 PDF·워드 파일을 "
        "올리면 이름·학력·논문이 자동으로 정리돼요.</span></li>"
        "<li><b>검토 표시 확인</b><span>자동으로 읽다 헷갈린 곳에는 <span class='pill "
        "p-검토필요'>검토</span> 가 붙어요. 열어서 맞는지만 봐 주세요.</span></li>"
        "<li><b>채용 시작</b><span>이번에 볼 사람을 체크하고 <b>채용 시작</b> 을 누르면 "
        "채용 현황에서 단계별로 관리할 수 있어요.</span></li></ol>"
        f"<p>{올리기} <a class='btn sec' href='/help#start' data-help='start'>사용법 보기</a></p>"
        "</div>"
    )


def _dashboard(me: User, q: str = "", review_only: bool = False, 년도: str = "",
               msg: str = "") -> bytes:
    records = store.list_filtered(q, review_only, 년도, registry=registry)
    전체 = store.count()
    만료 = store.expired_count()
    미분류 = registry.unclassified_count()

    warns = []
    if 미분류:
        warns.append(
            f"<div class='warn'>분류되지 않은 학회·저널이 <b>{미분류}건</b> 있습니다. "
            f"판별 전까지 해외 논문 열이 부정확할 수 있습니다. "
            f"<a href='/venues'>지금 분류하기 →</a></div>"
        )
    if 만료:
        warns.append(
            f"<div class='warn'>보관 기간이 지난 지원자가 <b>{만료}명</b> 있습니다. "
            f"<form method='post' action='/candidates/purge' style='display:inline'>"
            f"<button class='danger' onclick=\"return confirm('만료된 {만료}명을 삭제합니다. "
            f"되돌릴 수 없습니다. 진행할까요?')\">만료분 {만료}명 삭제</button></form></div>"
        )
    연도목록 = store.years()
    연도선택 = "".join(
        f"<option value='{y}'{' selected' if y == 년도 else ''}>{y}년</option>"
        for y in 연도목록
    )

    COLS = 표열()
    사용자열정의 = {f["이름"]: f for f in store.fields()}
    긴글열 = store.긴글열()
    사용자값맵 = store.custom_map()
    표값 = _표값맵()
    보기전용열 = set(RECRUIT_COLUMNS) | set(추가열("채용 현황")) | {MAIL_COLUMN}
    수정가능 = can(me, "지원자_수정")
    채용중 = recruit.started()
    채용가능 = can(me, "채용현황_수정")
    이름표 = 라벨(COLS)
    # 이름은 맨 앞 **고정 열**에 링크로 둔다 — 누르면 상세. 이름 열이 따로 또 있으면
    # 같은 이름이 두 번 보이므로 표 열에서는 뺀다 (이름 고치기는 상세에서).
    COLS = [c for c in COLS if c != "한글_이름"]
    끝낸검토 = store.review_done_map()
    head = "".join(
        f"<th class='{열폭(c)}' title='{html.escape(열이름도움(c))}'>{머리글(이름표[c])}</th>" for c in COLS)
    body_rows = []
    for rec in records:
        row = rec.to_row(registry)
        cid = rec.지원자_ID
        이름 = rec.한글_이름 or rec.영문_이름 or "(이름 없음)"
        남은검토 = review.display(rec.검토_사유, 끝낸검토.get(cid, set()))
        배지 = ""
        if rec.검토_필요 == "Y" and 남은검토 and 남은검토 != review.DONE_MARK:
            배지 += ("<span class='pill p-검토필요' title='자동으로 읽다 헷갈린 곳이 있어요 — "
                   "열어서 확인해 주세요'>검토</span>")
        if cid in 채용중:
            배지 += "<span class='pill p-처리중'>채용 중</span>"
        cells = [
            f"<td class='ck'><input type='checkbox' name='ids' value='{html.escape(cid)}'></td>",
            f"<td class='pname'><a href='/candidate?id={urllib.parse.quote(cid)}'"
            f" title='눌러서 자세히 보기'>{html.escape(이름)}</a>{배지}</td>",
        ]
        for c in COLS:
            폭 = 열폭(c)
            if c == "검토_사유":
                # 검토 카드가 관리하는 열이다. 여기서 글을 고치면 '확인함'
                # 기록과 글자가 어긋나 되돌릴 수 없다. 그래서 고칠 수 없고,
                # **아직 안 본 항목만** 보인다 (전부 봤으면 '확인함').
                v = html.escape(표값.get(cid, {}).get(c, ""))
                남음 = bool(v) and v != review.DONE_MARK
                cells.append(
                    f"<td class='{'flag' if 남음 else 'muted'} {폭}' title='{v}'>"
                    + (f"<a href='/candidate?id={urllib.parse.quote(cid)}#검토'>{v}</a>"
                       if 남음 else v)
                    + "</td>")
                continue
            if c == "구글_스칼라_링크":
                # 표에서는 **눌러서 여는 링크**다. 여기서 칸을 눌러 고치게 하면
                # 링크를 열 수가 없다 — 고치는 건 상세 화면에서 한다.
                v = str(row.get(c, "") or "")
                cells.append(
                    f"<td class='{폭}' title='{html.escape(v)}'>"
                    + (f"<a href='{html.escape(v)}' target='_blank' rel='noopener'>"
                       "구글 스칼라 ↗</a>" if v.startswith("http") else html.escape(v))
                    + "</td>")
                continue
            if c == STARTED_COLUMN:
                v = 표값.get(cid, {}).get(c, "")
                모양 = "p-처리중" if v == "채용 중" else "p-대기중"
                cells.append(f"<td class='{폭}'>"
                             + (f"<span class='pill {모양}'>{html.escape(v)}</span>"
                                if v else "") + "</td>")
                continue
            if c in MANAGE_COLUMNS or c in 보기전용열:
                # 등록·보관 정보, 채용 현황, 메일 이력은 여기서 고치지 않는다.
                # 고치는 자리가 따로 있는 값이라 여기서 덮어쓰면 어긋난다.
                v = html.escape(표값.get(cid, {}).get(c, ""))
                cells.append(f"<td class='muted {폭}' title='{v}'>{v}</td>")
                continue
            if c in 사용자열정의:
                값 = 사용자값맵.get(cid, {}).get(c, "")
                # 수식 열: 사람이 안 적었으면 수식 값을 보인다 (`_표값맵` 이 계산해 뒀다).
                fx = 사용자열정의[c].get("수식") or ""
                수식값 = bool(fx) and not 값.strip()
                보임 = 표값.get(cid, {}).get(c, "") if 수식값 else 값
                if 수정가능:
                    cells.append(_cell(cid, c, 보임, 값,
                                       custom_field_spec(사용자열정의[c],
                                                         c in 긴글열),
                                       scope="사용자", cls=" " + 폭,
                                       fx=fx, 수식값=수식값))
                else:
                    cells.append(f"<td class='{폭}{' fxcol' if 수식값 else ''}'"
                                 f" title='{html.escape(보임)}'>{html.escape(보임)}</td>")
                continue
            표시 = str(row.get(c, "") or "")
            cls = " flag" if c == "검토_필요" and 표시 == "Y" else ""
            if 수정가능 and _editable(c):
                cells.append(_cell(cid, c, 표시, 보이는값(rec, c, registry),
                                   field_spec(c, c in 긴글열), cls=cls + " " + 폭))
            else:
                v = html.escape(표시)
                cells.append(f"<td class='{cls.strip()} {폭}' title='{v}'>{v}</td>")
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    # 걸어 둔 검색 조건. 엑셀도, 메일 보낸 뒤 돌아올 곳도 이 조건을 그대로 쓴다.
    조건 = {k: v for k, v in (("q", q), ("year", 년도),
                             ("review", "1" if review_only else "")) if v}
    조건쿼리 = ("?" + urllib.parse.urlencode(조건)) if 조건 else ""
    내려받기안내 = (f"지금 걸린 조건({len(records)}명)만 받습니다"
                if 조건 else "전체를 받습니다")
    메일단추 = (
        "<button formaction='/mail/compose'>선택한 사람에게 메일</button> "
        if can(me, "메일_발송") else ""
    )
    # 파란 단추는 **한 줄에 하나**만 둔다. 셋이 나란히 파랗게 있으면 무엇이
    # 주된 일인지 알 수 없고, 화면이 시끄럽다.
    묶음단추 = 메일단추 + (
        "<button formaction='/candidates/start' class='sec'>채용 시작</button> "
        "<button formaction='/candidates/stop' class='sec'>채용 현황에서 내리기</button> "
        if 채용가능 else ""
    )
    if records:
        # 줄마다 있는 단추는 이 표 **밖의** 폼으로 보낸다. 폼 안에 폼을 넣으면
        # 브라우저가 안쪽을 버려서 엉뚱한 동작이 실행된다 (전에 그랬다).
        #
        # 여러 명에게 하는 일(메일·채용 시작·삭제)은 **체크했을 때만** 막대로 나온다.
        # 아무도 안 골랐는데 늘 떠 있으면 무엇을 눌러야 하는지 오히려 헷갈린다.
        table = f"""
        <form method='post' action='/candidates/delete' class='selform'>
          <input type='hidden' name='back' value='{html.escape("/" + 조건쿼리)}'>
          <div class='selbar'>
            <span class='selnone muted'>왼쪽 칸을 체크하면 여러 명에게 메일 보내기 · 채용 시작 ·
              삭제를 한꺼번에 할 수 있어요.</span>
            <span class='selsome'><b class='selcount'>0명</b> 선택됨
              {묶음단추}<button type='submit' class='danger ghost'
               onclick="return window.confirm('선택한 지원자를 삭제합니다. 되돌릴 수 없습니다.')"
               >선택 삭제</button></span>
          </div>
          <div class='scroll'><table data-name='인재 Pool'
                data-export='/export.xlsx{조건쿼리}'>
            <tr><th class='ck'><input type='checkbox' onclick="selectVisible(this)"
                title='보이는 줄만 선택합니다'>
            </th><th class='pname'>이름</th>{head}</tr>
            {''.join(body_rows)}
          </table></div>
        </form>"""
    elif 전체:
        table = ("<div class='empty'><b>검색 조건에 맞는 지원자가 없어요.</b>"
                 "<span>검색어를 줄이거나 <a href='/'>초기화</a> 해 보세요.</span></div>")
    else:
        table = _시작하기(me)

    안내 = (
        "<span class='tip' tabindex='0' data-tip='칸을 누르면 그 자리에서 고칠 수 있어요.\n"
        "Enter 저장 · Esc 취소 · 여러 줄 칸은 Ctrl+Enter 로 저장\n"
        "회색 칸(논문 수처럼 계산된 값)은 고칠 수 없어요.\n"
        "열 제목을 누르면 정렬하거나 걸러 볼 수 있어요.'>i</span>"
        if 수정가능 else ""
    )
    checked = " checked" if review_only else ""
    처리중 = _busy_count()
    처리중알림 = (
        f"<div class='warn'>CV {처리중}건을 분석하고 있습니다. "
        f"<a href='/upload'>진행 상황 보기 →</a></div>" if 처리중 else ""
    )

    알림 = _알림(msg=msg)
    return _page(
        "인재 Pool",
        f"""{알림}{''.join(warns)}{처리중알림}
        <h1 class='pt'>인재 Pool <span class='sub'>{len(records)}명{f' / 전체 {전체}명' if len(records) != 전체 else ''} · 채용 중 {len(채용중)}명</span>{안내}</h1>
        <div class='card'>
          <form method='get' action='/' class='bar searchbar'>
            <input type='search' name='q' value='{html.escape(q)}' placeholder='이름 · 소속 · 학교 · 파일명으로 찾기' style='width:300px'>
            <select name='year'><option value=''>전체 년도</option>{연도선택}</select>
            <label class='muted chk'><input type='checkbox' name='review' value='1'{checked}>
              검토 필요만</label>
            <button type='submit' class='sec'>검색</button>
            {"<a class='btn sec' href='/'>초기화</a>" if 조건 else ""}
          </form>
          {table}
        </div>""",
        me=me,
    )


def _엑셀양식열() -> tuple[list[str], dict[str, str]]:
    """엑셀 양식에 낼 열과 머리글. 화면·양식·읽기가 같은 것을 보게 한 곳에서."""
    열 = bulk.양식열(store)
    return 열, 라벨(열)


def _엑셀등록(data: bytes, me: User) -> tuple[list[str], list[str], list[str]]:
    """채워 올린 양식을 읽어 지원자를 만든다.

    Returns:
        (만든 지원자_ID 들, 빠진 줄 설명, 못 알아본 머리글)

    **틀린 줄은 그 줄만 빠진다.** 100명 중 2명 때문에 98명을 다시 올리게 하면
    아무도 안 쓴다. 대신 몇 행이 왜 빠졌는지 그대로 돌려준다.
    """
    열, 머리 = _엑셀양식열()
    줄들, 모르는것 = bulk.읽기(data, 열, 머리)
    긴글열 = store.긴글열()

    만든것: list[str] = []
    빠진것: list[str] = []
    for 행번호, 값들 in 줄들:
        rec = CVRecord(지원자_ID=f"CV-{uuid.uuid4().hex[:8].upper()}")
        추가값: dict[str, str] = {}
        년도 = ""
        탈 = ""
        for 항목, 값 in 값들.items():
            if not 값:
                continue
            try:
                if 항목 == "등록년도":
                    년도 = 값
                elif 항목 in REGISTRY_FIELDS:
                    # 명칭 사전이 관리하는 열은 **사전에 있는 이름만** 받는
                    # edit.validate_registry 를 쓰지 않는다. 새 지원자를 무더기로
                    # 넣는 자리에서는 거의 다 걸린다. CV 업로드가 가는 길과 같게
                    # 원문 표기를 그대로 담고, 아래 observe_record 가 사전에
                    # 등록한다 (미분류로 올라와 명칭 관리에서 판별한다).
                    setattr(rec, 항목, 값)
                elif (정의 := store.field(항목)) is not None:
                    추가값[항목] = validate_custom(정의, 값, 항목 in 긴글열)
                else:
                    setattr(rec, 항목, validate(항목, 값, 긴글=항목 in 긴글열))
            except (ValidationError, ValueError) as exc:
                탈 = f"{행번호}행 {항목}: {exc}"
                break
        if 탈:
            빠진것.append(탈)
            continue

        # 사람이 손으로 적은 값이라 **무조건** 검토를 거친다.
        사유 = [bulk.등록사유]
        사유.extend(observe_record(rec, registry))
        # 지문(CV 원문)이 없어도 이메일·전화·이름+생년월일 일치는 잡힌다.
        후보 = find_duplicates(rec, [], store.fingerprints())
        메모 = " / ".join(str(m) for m in 후보)
        if 후보:
            사유.append(("중복 확실: " if any(m.수준 == "확실" for m in 후보)
                       else "중복 의심: ") + 메모)
        rec.검토_필요 = "Y"
        rec.검토_사유 = " / ".join(사유)

        store.save(rec, 저장_파일명="", 지문=[], 중복_메모=메모)
        if 년도:
            try:
                store.set_year(rec.지원자_ID, 년도)
            except ValueError as exc:
                빠진것.append(f"{행번호}행 등록년도: {exc} (나머지는 등록했습니다)")
        for 이름, 값 in 추가값.items():
            store.set_custom(rec.지원자_ID, 이름, 값)
        audit.record(me.아이디, "지원자", rec.지원자_ID, 비고="엑셀로 등록")
        만든것.append(rec.지원자_ID)
    return 만든것, 빠진것, 모르는것


def _엑셀결과카드(만든것: list[str], 빠진것: list[str], 모르는것: list[str]) -> str:
    이름 = {r.지원자_ID: (r.한글_이름 or r.영문_이름 or r.지원자_ID)
          for r in store.list_all()}
    줄 = "".join(
        f"<li><a href='/candidate?id={urllib.parse.quote(c)}'>"
        f"{html.escape(이름.get(c, c))}</a></li>" for c in 만든것
    )
    본 = [f"<div class='card'><h2>{len(만든것)}명 등록했습니다</h2>"]
    if 만든것:
        본.append("<p class='muted'>전부 <b>검토 필요</b> 로 표시돼 있습니다 — "
                "손으로 적은 값이라 한 번 보고 <b>확인함</b> 을 눌러 주세요.</p>"
                f"<ul style='columns:3'>{줄}</ul>")
    if 빠진것:
        본.append("<h2 style='margin-top:14px'>빠진 줄 "
                f"<span class='pill p-검토필요'>{len(빠진것)}</span></h2>"
                "<p class='muted'>아래 줄만 등록하지 못했습니다. 엑셀에서 그 행을 "
                "고쳐 <b>그 줄만</b> 다시 올리면 됩니다.</p><ul class='flag'>"
                + "".join(f"<li>{html.escape(x)}</li>" for x in 빠진것) + "</ul>")
    if 모르는것:
        본.append("<p class='muted'>모르는 열은 그냥 넘겼습니다: "
                + html.escape(", ".join(모르는것[:10])) + "</p>")
    본.append("<p><a class='btn sec' href='/upload'>지원자 추가로</a> "
            "<a class='btn sec' href='/'>인재 Pool 로</a></p></div>")
    return "".join(본)


def _upload_page(me: User, error: str = "") -> bytes:
    """지원자 추가 — CV 를 올리거나, CV 없이 빈 줄을 만든다.

    예전에는 지원자 목록 맨 위에 업로드 상자가 붙어 있어서, 표를 보러 올 때마다
    쓰지도 않는 상자가 화면을 차지했다. 탭으로 뺐다.
    """
    보관 = "켜짐 (재분석 가능)" if settings.store_cv_text else "꺼짐 (재분석하려면 재업로드 필요)"
    가능 = ", ".join(sorted(SUPPORTED_SUFFIXES))
    등록가능 = can(me, "지원자_등록")
    if not 등록가능:
        본문 = "<div class='card'><h2>지원자 추가</h2><p>추가 권한이 없습니다.</p></div>"
    else:
        본문 = f"""
        <div class='card'><h2>CV 올려서 추가</h2>
          <form method='post' action='/upload' enctype='multipart/form-data'>
            <p><input type='file' name='files' multiple accept='{가능}'></p>
            <button type='submit'>업로드 후 분석</button>
            <span class='muted'>여러 개를 한 번에 고를 수 있습니다 ({가능}).</span>
          </form>
          <p class='muted'>분석은 뒤에서 돌아갑니다. 끝나면 아래 현황에 뜨고
          <a href='/'>인재 Pool</a>에 줄이 생깁니다.</p>
        </div>
        <div class='card'><h2>CV 없이 지원자 추가</h2>
          <form method='post' action='/candidate/new'>
            <button type='submit' class='sec'>빈 지원자 만들기</button>
            <span class='muted'>한 명만 넣을 때. 상세 화면에서 빈 칸을 직접 채웁니다.</span>
          </form>
          <p class='muted' style='margin:14px 0 6px'><b>여러 명 한 번에</b> —
          지금 표 열 그대로 만든 빈 양식을 받아 채운 뒤 올리세요
          (열 {len(_엑셀양식열()[0])}개).</p>
          <form method='post' action='/upload/xlsx' enctype='multipart/form-data'
                style='display:flex;gap:8px;align-items:center;flex-wrap:wrap'>
            <a class='btn sec' href='/upload/template.xlsx'>엑셀 양식 받기</a>
            <input type='file' name='files' accept='.xlsx'>
            <button type='submit'>올려서 등록</button>
          </form>
          <p class='muted'>첫 줄(열 이름)은 지우지 마세요. 형식이 틀린 줄만
          빠지고 나머지는 등록됩니다. 올린 사람은 전부 <b>검토 필요</b> 로
          표시됩니다 — 손으로 적은 값이라 한 번 보고 넘기라는 뜻입니다.</p>
        </div>
        <div class='card'><h2>보관 설정</h2>
          <p class='muted'>원문 텍스트 보관: <b>{보관}</b> ·
          보관 기간 {settings.retention_months}개월
          (0 = 무제한) · 설정은 <code>.env</code> 에서 바꿉니다.</p>
        </div>"""
    return _page("지원자 추가",
                 _알림(err=error) + 본문 + _status_table() + _STATUS_POLL_JS, me=me)


#: 현황 표만 갈아 끼우는 폴링.
#:
#: <meta refresh> 로 페이지를 통째로 다시 그리면 분석이 도는 동안 파일을 고를 수
#: 없다 — 고르는 순간 새로고침이 끼어들어 선택이 풀린다. 그래서 표 안쪽만 바꾼다.
#: 처리 중일 때만 돌고, 다 끝나면 스스로 멈춘다 (빈 서버를 계속 두드리지 않는다).
_STATUS_POLL_JS = '\n<script>' + _정적JS("status_poll.js") + '</script>'


def _candidate_page(지원자_ID: str, me: User, error: str = "",
                    msg: str = "") -> bytes:
    rec = store.get(지원자_ID)
    if rec is None:
        return _page("없음", "<div class='card'>해당 지원자를 찾을 수 없습니다.</div>")
    meta = store.meta(지원자_ID) or {}
    row = rec.to_row(registry)
    수정가능 = can(me, "지원자_수정")
    긴글열 = store.긴글열()
    # 현업에게는 검토 필요·관리 정보·변경 이력을 내지 않는다. 자기 과제 지원자가
    # 어디까지 왔는지만 보면 되는 자리라, 추출 신뢰도·등록 경위·누가 무엇을
    # 고쳤는지까지 열어 둘 이유가 없다.
    관리정보 = can(me, "지원자_관리정보")

    # 검토가 필요한 항목이 어느 열에 대한 이야기인지 미리 알아 둔다.
    끝낸검토 = store.review_done(지원자_ID)
    검토항목 = review.items(rec.검토_사유, 끝낸검토)
    검토열 = review.columns_needing_review(rec.검토_사유, 끝낸검토)

    def 입력칸(항목: str, 값: str, 이름: str) -> str:
        """한 칸. 이름은 값_{i} 처럼 번호를 달아 **한 폼에** 담는다."""
        if 항목 in REGISTRY_FIELDS:
            종류 = NAME_COLUMNS[항목]
            # <select> 가 아니라 목록이 딸린 입력칸이다. 사전에 있는 이름을
            # 고르는 것도, **이 사람만의 값을 적는 것도** 한 칸에서 된다.
            # (드롭다운이던 동안에는 한 사람만의 예외를 적을 자리가 없었다.)
            목록 = f"dl_{이름}"
            opts = "".join(f"<option value='{html.escape(n)}'>"
                           for n in registry.display_names(종류))
            고정 = 항목 in rec.직접입력
            표딱지 = (" <span class='pill p-검토필요'>직접 입력</span>"
                    f"<button class='sec' form='unpinform' name='col'"
                    f" value='{html.escape(항목)}'"
                    f" title='이 칸을 다시 명칭 관리에 맡깁니다'>사전 따라가기</button>"
                    if 고정 else "")
            return (f"<input type='text' form='saveform' name='{이름}'"
                    f" list='{목록}' value='{html.escape(값)}'"
                    f" style='width:100%;max-width:420px'"
                    f" data-orig='{html.escape(값)}' oninput='markDirty(this)'"
                    f" placeholder='목록에서 고르거나 직접 적으세요'>"
                    f"<datalist id='{목록}'>{opts}</datalist>{표딱지}")
        spec = field_spec(항목, 항목 in 긴글열)
        if spec.입력 == "select":
            opts = "".join(
                f"<option value='{html.escape(o)}'{' selected' if o == 값 else ''}>"
                f"{html.escape(o) or '(빈칸)'}</option>"
                for o in spec.선택지
            )
            return (f"<select form='saveform' name='{이름}' onchange='markDirty(this)'"
                    f" data-orig='{html.escape(값)}'>{opts}</select>")
        도움 = f" placeholder='{html.escape(spec.도움말)}'" if spec.도움말 else ""
        if spec.입력 == "긴글":
            return (f"<textarea form='saveform' name='{이름}' rows='4'"
                    f" style='width:100%;max-width:640px'"
                    f" data-orig='{html.escape(값)}' oninput='markDirty(this)'"
                    f"{도움}>{html.escape(값)}</textarea>")
        return (f"<input type='text' form='saveform' name='{이름}'"
                f" value='{html.escape(값)}' style='width:100%;max-width:420px'"
                f" data-orig='{html.escape(값)}' oninput='markDirty(this)'{도움}>")

    def 검토배지(c: str) -> str:
        return (" <span class='pill p-검토필요'>검토</span>"
                if c in 검토열 else "")

    이름표 = 라벨(list(table_columns(registry)))
    항목행 = []
    숨은칸 = []
    번호 = 0
    for c in table_columns(registry):
        값 = str(row.get(c, "") or "")
        보기 = html.escape(값) or "<span class='muted'>-</span>"
        if c == "검토_사유" and 검토항목:
            # 확인한 항목은 여기서 빠진다. 원문은 위 검토 카드에 회색으로 남아
            # 있으니 무엇을 봤는지 되짚을 수 있고, 이 줄은 **남은 것만** 말한다.
            보일글 = review.display(rec.검토_사유, 끝낸검토)
            보기 = ("<a href='#검토'>위 검토 카드에서 항목별로 봅니다 →</a>"
                  f"<br><span class='muted'>{html.escape(보일글)}</span>")
        줄표시 = " class='needs'" if c in 검토열 else ""
        # 검토_사유는 위 검토 카드가 관리한다. 여기서 글을 고치면 '확인함'
        # 표시와 짝이 안 맞는다.
        if (not 수정가능 or c in READONLY_FIELDS or c == "검토_사유"
                or c.startswith("1저자_해외논문_") or is_tier_venue(c)):
            항목행.append(
                f"<tr{줄표시}><th style='width:180px'>{html.escape(이름표[c])}"
                f"{검토배지(c)}</th>"
                f"<td style='white-space:normal;max-width:none'>{보기}</td></tr>"
            )
            continue
        번호 += 1
        # 칸에 넣는 값도, 숨은 «이전 값» 도 **화면에 뜨는 값**이다. 레코드가 든
        # 날값을 넣으면 표와 상세가 서로 다른 말을 하고(사전 이름·졸업 판정),
        # 손도 안 댄 칸이 바뀐 것으로 잡혀 «바뀐 것» 목록·알림·검사에 끼어든다.
        이전값 = 보이는값(rec, c, registry)
        숨은칸.append(
            f"<input type='hidden' form='saveform' name='항목_{번호}'"
            f" value='{html.escape(c)}'>"
            f"<input type='hidden' form='saveform' name='이전_{번호}'"
            f" value='{html.escape(이전값)}'>"
        )
        항목행.append(
            f"<tr{줄표시}><th style='width:180px'>{html.escape(이름표[c])}"
            f"{검토배지(c)}</th>"
            f"<td style='white-space:normal;max-width:none'>"
            f"{입력칸(c, 이전값, f'값_{번호}')}</td></tr>"
        )

    사용자열 = store.fields()
    사용자값 = store.custom_values(지원자_ID)
    사용자행 = []
    # 수식이 걸린 열은 비워 두면 수식 값이 나온다 — 그 값을 보여 준다.
    계산값 = (_프로필값(지원자_ID)
             if any((f.get("수식") or "").strip() for f in 사용자열) else {})
    for f in 사용자열:
        이름, 값 = f["이름"], 사용자값.get(f["이름"], "")
        fx = (f.get("수식") or "").strip()
        수식값 = 계산값.get(이름, "") if fx and not 값.strip() else ""
        수식안내 = (f"<br><span class='muted' title='칸에 적으면 그 값이 우선, 지우면 다시 수식'>"
                 f"수식 <code>{html.escape(fx)}</code></span>" if fx else "")
        if not 수정가능:
            보임 = (html.escape(값) or (f"<i class='muted'>{html.escape(수식값)}</i>" if 수식값
                                      else "<span class=muted>-</span>"))
            사용자행.append(
                f"<tr><th style='width:180px'>{html.escape(이름)}{수식안내}</th>"
                f"<td>{보임}</td></tr>"
            )
            continue
        번호 += 1
        spec = custom_field_spec(f, 이름 in 긴글열)
        안내 = (f"비워 두면 수식 값 ({수식값})" if fx and 수식값
                else "비워 두면 수식 값" if fx else spec.도움말)
        if spec.입력 == "select":
            opts = "".join(
                f"<option value='{html.escape(o)}'{' selected' if o == 값 else ''}>"
                f"{html.escape(o) or (f'(수식: {수식값})' if fx else '(빈칸)')}</option>"
                for o in spec.선택지
            )
            칸 = (f"<select form='saveform' name='값_{번호}' onchange='markDirty(this)'"
                 f" data-orig='{html.escape(값)}'>{opts}</select>")
        elif spec.입력 == "긴글":
            칸 = (
                f"<textarea form='saveform' name='값_{번호}' rows='4'"
                f" style='width:100%;max-width:640px'"
                f" data-orig='{html.escape(값)}' oninput='markDirty(this)'"
                f" placeholder='{html.escape(안내)}'>{html.escape(값)}</textarea>"
            )
        else:
            칸 = (
                f"<input type='text' form='saveform' name='값_{번호}'"
                f" value='{html.escape(값)}' style='width:100%;max-width:420px'"
                f" data-orig='{html.escape(값)}' oninput='markDirty(this)'"
                f" placeholder='{html.escape(안내)}'>"
            )
        숨은칸.append(
            f"<input type='hidden' form='saveform' name='항목_{번호}'"
            f" value='{html.escape(이름)}'>"
            f"<input type='hidden' form='saveform' name='이전_{번호}'"
            f" value='{html.escape(값)}'>"
            f"<input type='hidden' form='saveform' name='구분_{번호}' value='추가'>"
        )
        사용자행.append(
            f"<tr><th style='width:180px'>{html.escape(이름)}"
            f"<br><span class='muted'>{html.escape(f['유형'])}</span>{수식안내}</th>"
            f"<td>{칸}</td></tr>"
        )

    사용자카드 = (
        f"<div class='card'><h2>추가 항목</h2><table>{''.join(사용자행)}</table></div>"
        if 사용자열 else ""
    )

    # 검토 카드 — 항목마다 '확인함' 을 눌러 하나씩 지운다.
    남은검토 = [x for x in 검토항목 if not x["완료"]]
    본검토 = [x for x in 검토항목 if x["완료"]]

    def 검토줄(x: dict, 완료: bool) -> str:
        가리킴 = " · ".join(이름표.get(c, c) for c in x["열"])
        단추 = ""
        if 수정가능:
            길 = "/candidate/review/undo" if 완료 else "/candidate/review/done"
            단추 = (
                f"<form method='post' action='{길}' style='display:inline'>"
                f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
                f"<input type='hidden' name='사유' value='{html.escape(x['글'])}'>"
                + ("<button class='sec'>되돌리기</button>" if 완료
                   else "<button>확인함</button>")
                + "</form>"
            )
        return (
            f"<tr><td style='white-space:normal'>"
            + ("<span class='muted'>" if 완료 else "<b>")
            + html.escape(x["글"])
            + ("</span>" if 완료 else "</b>")
            + (f"<br><span class='muted'>관련 항목: {html.escape(가리킴)}</span>"
               if 가리킴 else "")
            + f"</td><td class='ctl'>{단추}</td></tr>"
        )

    검토카드 = ""
    if 검토항목 and 관리정보:
        줄 = ("".join(검토줄(x, False) for x in 남은검토)
             + "".join(검토줄(x, True) for x in 본검토))
        머리 = (f"검토 필요 <span class='pill p-검토필요'>{len(남은검토)}건</span>"
              if 남은검토 else "검토 완료 <span class='pill p-완료'>전부 확인함</span>")
        안내 = (
            "<p class='muted'>LLM 이 <b>확신하지 못한 것</b>들입니다. 아래 표에서 "
            "해당 항목이 <span class='pill p-검토필요'>검토</span> 로 표시돼 있습니다. "
            "값을 고치거나 그대로 둔 뒤 <b>확인함</b> 을 누르세요. "
            "전부 확인하면 이 지원자는 검토 필요에서 빠집니다.</p>"
            if 남은검토 else
            "<p class='muted'>모두 확인했습니다. 이 지원자는 검토 필요가 아닙니다.</p>"
        )
        검토카드 = (
            f"<div class='card' id='검토'"
            + (" style='border-color:#fcd34d;background:#fffbeb'" if 남은검토 else "")
            + f"><h2>{머리}</h2>{안내}"
            "<table><tr><th>사유</th><th style='width:110px'></th></tr>"
            + 줄 + "</table></div>"
        )


    # 논문·특허 목록 — 표의 '수' 열이 무엇을 세었는지 눈으로 볼 수 있어야 한다.
    # **심사중까지 전부 보여준다.** 세는 것과 보여주는 것은 다른 일이다.
    논문보기 = rec.papers_view(registry)
    주저자배지 = "<span class='pill p-완료'>주저자</span>"
    공저자배지 = "<span class='muted'>공저자</span>"
    심사중배지 = "<span class='pill p-안본것'>심사중</span>"

    # 논문과 특허는 **폼이 따로**다 (줄 번호가 서로 섞이면 안 된다). 두 표가
    # 같은 칸 그리기를 쓰므로 폼 이름만 받는다.
    def _고르기(이름: str, 값: str, 고를것: list[str], 빈칸: str = "",
              폼: str = "paperform") -> str:
        opts = "".join(
            f"<option value='{html.escape(o)}'{' selected' if o == 값 else ''}>"
            f"{html.escape(o) or 빈칸}</option>" for o in 고를것)
        return (f"<select form='{폼}' name='{이름}'"
                f" onchange='markDirty(this)'>{opts}</select>")

    def _논문칸(이름: str, 값: str, 폭: str = "100%", 안내: str = "",
             폼: str = "paperform") -> str:
        # 안내(placeholder)를 반드시 단다. 제목과 제출처가 한 칸에 위아래로
        # 놓이는데, 빈 칸이면 어느 쪽이 무엇인지 알 수가 없다.
        도움 = f" placeholder='{html.escape(안내)}'" if 안내 else ""
        return (f"<input type='text' form='{폼}' name='{이름}'"
                f" value='{html.escape(값)}' style='width:{폭}'"
                f" oninput='markDirty(this)'{도움}>")

    def _논문줄(n: int, p_: Paper | None, v: dict | None) -> str:
        """한 줄. `p_` 가 None 이면 맨 아래 **추가용 빈 줄**이다."""
        제목 = p_.제목 if p_ else ""
        제출처 = p_.제출처 if p_ else ""       # 원표기다 — 사전이 이걸로 찾는다
        연도 = p_.연도 if p_ else ""
        유형 = p_.유형 if p_ else ""
        국내해외 = p_.국내해외 if p_ else "불명"
        저자 = p_.저자구분 if p_ else "주저자"
        상태 = p_.게재상태 if p_ else "게재"
        등급 = (v or {}).get("등급") or "-"
        보이는이름 = (v or {}).get("표시명") or ""
        딴이름 = (f"<br><span class='muted'>표에는 {html.escape(보이는이름)}</span>"
                if 보이는이름 and 보이는이름 != 제출처 else "")
        지우기 = ("" if p_ is None else
                f"<label><input type='checkbox' form='paperform'"
                f" name='del_{n}' value='1' onchange='markDirty(this)'> 삭제</label>")
        return (
            f"<tr><td>{_고르기(f'저자구분_{n}', 저자, list(저자구분_ENUM))}</td>"
            f"<td>{_고르기(f'유형_{n}', 유형, ['', '학회', '저널', '기타'], '(빈칸)')}</td>"
            # 제목과 제출처를 **위아래로** 쌓는다. 그냥 나란히 두면 앞의 칸이
            # 자리를 다 먹고 제출처가 실오라기만큼 남는다 — 사전이 찾는 값이라
            # 가장 잘 보여야 하는 칸인데 안 보였다.
            f"<td><div style='display:flex;flex-direction:column;gap:4px'>"
            f"{_논문칸(f'제목_{n}', 제목, '100%', '논문 제목')}"
            f"{_논문칸(f'제출처_{n}', 제출처, '100%', '제출처 (학회·저널 이름)')}"
            f"</div>{딴이름}</td>"
            f"<td>{_논문칸(f'연도_{n}', 연도, '70px', 'YYYY')}</td>"
            f"<td>{_고르기(f'국내해외_{n}', 국내해외, ['불명', '국내', '해외'])}</td>"
            f"<td>{_고르기(f'게재상태_{n}', 상태, list(게재상태_ENUM))}</td>"
            f"<td class='muted'>{html.escape(등급)}</td>"
            f"<td>{지우기}</td></tr>"
        )

    if 수정가능:
        줄들 = [_논문줄(i, p_, v)
              for i, (p_, v) in enumerate(zip(rec.논문, 논문보기), start=1)]
        줄들.append(_논문줄(len(rec.논문) + 1, None, None))   # 추가용 빈 줄
        논문행 = "".join(줄들)
    else:
        논문행 = "".join(
            f"<tr><td>{주저자배지 if v['주저자'] else 공저자배지}</td>"
            f"<td>{html.escape(v['유형'])}</td>"
            f"<td title='{html.escape(v.get('제목') or v['표시명'])}'>"
            f"{html.escape(v.get('제목') or '')}"
            + (f"<br><span class='muted'>{html.escape(v['표시명'])}</span>"
               if v.get('제목') else html.escape(v['표시명']))
            + f"</td><td>{html.escape(v['연도'])}</td>"
            f"<td>{html.escape(v['국내해외'])}</td>"
            + (f"<td>{심사중배지}</td>" if v['게재상태'] == "심사중"
               else "<td class='muted'>게재</td>")
            + f"<td>{html.escape(v['등급'])}</td></tr>"
            for v in 논문보기
        )
    def _특허줄(n: int, pt: Patent | None) -> str:
        """특허 한 줄. `pt` 가 None 이면 맨 아래 **추가용 빈 줄**이다."""
        상태 = pt.상태 if pt else "등록"
        국가 = pt.국가 if pt else ""
        지역 = pt.국내해외 if pt else "불명"
        지우기 = ("" if pt is None else
                f"<label><input type='checkbox' form='patentform'"
                f" name='특허del_{n}' value='1' onchange='markDirty(this)'>"
                " 삭제</label>")
        # 칸 이름에 «특허» 를 붙인다. 논문 표에도 `제목_1`·`국내해외_1` 이 있어서
        # 안 붙이면 한 화면에 같은 이름이 두 벌 생긴다. 폼이 달라 서버는
        # 헷갈리지 않지만, 화면을 짚어 보는 사람과 브라우저 시험이 헷갈린다.
        ㄱ = lambda 이름, 값, 고를것: _고르기(f"특허{이름}_{n}", 값, 고를것,
                                      폼="patentform")
        ㅊ = lambda 이름, 값, 폭, 안내: _논문칸(f"특허{이름}_{n}", 값, 폭, 안내,
                                        폼="patentform")
        return (
            f"<tr><td>{ㄱ('상태', 상태, list(특허상태_ENUM))}</td>"
            f"<td>{ㅊ('국가', 국가, '90px', '한국·미국…')}</td>"
            f"<td>{ㄱ('국내해외', 지역, list(특허지역_ENUM))}</td>"
            f"<td>{ㅊ('제목', pt.제목 if pt else '', '100%', '특허 제목')}</td>"
            f"<td>{ㅊ('연도', pt.연도 if pt else '', '70px', 'YYYY')}</td>"
            f"<td>{ㅊ('번호', pt.번호 if pt else '', '140px', '등록·출원 번호')}</td>"
            f"<td>{지우기}</td></tr>"
        )

    if 수정가능:
        특허줄들 = [_특허줄(i, pt) for i, pt in enumerate(rec.특허, start=1)]
        특허줄들.append(_특허줄(len(rec.특허) + 1, None))     # 추가용 빈 줄
        특허행 = "".join(특허줄들)
    else:
        특허행 = "".join(
            f"<tr><td>{html.escape(pt.상태)}</td>"
            f"<td>{html.escape(pt.국가) or '-'}</td>"
            f"<td>{html.escape(pt.국내해외)}</td>"
            f"<td title='{html.escape(pt.제목)}'>{html.escape(pt.제목) or '-'}</td>"
            f"<td>{html.escape(pt.연도)}</td><td>{html.escape(pt.번호)}</td></tr>"
            for pt in rec.특허
        )
    # 수상 — 논문·특허와 같은 모양의 목록. 명칭 관리는 안 한다 (적힌 그대로).
    def _수상줄(n: int, a) -> str:
        지우기 = ("" if a is None else
                f"<label><input type='checkbox' form='awardform'"
                f" name='수상del_{n}' value='1' onchange='markDirty(this)'> 삭제</label>")
        ㅊ = lambda 이름, 값, 폭, 안내: _논문칸(f"수상{이름}_{n}", 값, 폭, 안내, 폼="awardform")
        연월 = (a.연월 if a else "")
        보일연월 = (f"{연월[:4]}.{연월[4:6]}" if len(연월) == 6 and 연월[4:] != "00"
                 else 연월[:4])
        return (
            f"<tr><td>{ㅊ('상명', a.상명 if a else '', '100%', '상 이름')}</td>"
            f"<td>{ㅊ('수여처', a.수여처 if a else '', '100%', '대회명 또는 주관처')}</td>"
            f"<td>{ㅊ('연월', 보일연월, '90px', 'YYYY.MM')}</td>"
            f"<td>{지우기}</td></tr>"
        )

    if 수정가능:
        수상행 = "".join([_수상줄(i, a) for i, a in enumerate(rec.수상, start=1)]
                      + [_수상줄(len(rec.수상) + 1, None)])
    else:
        수상행 = "".join(
            f"<tr><td>{html.escape(a.상명)}</td><td>{html.escape(a.수여처) or '-'}</td>"
            f"<td>{html.escape(a.한줄().rsplit(' (', 1)[-1].rstrip(')') if a.연월 else '-')}</td></tr>"
            for a in sorted(rec.수상, key=lambda a: a.연월 or "", reverse=True))
    수상폼 = (
        "<form method='post' action='/candidate/awards' id='awardform' class='mergebar'>"
        f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
        f"<input type='hidden' name='끝' value='{len(rec.수상) + 1}'>"
        "<button type='submit'>수상 목록 저장</button>"
        "<span class='muted'>맨 아랫줄에 적으면 <b>새 수상</b>이 됩니다. "
        "상 이름을 비우면 그 줄은 저장되지 않습니다.</span></form>"
        if 수정가능 else "")
    수상표 = (
        "<h2 style='margin-top:14px'>수상 "
        f"<span class='muted'>{len(rec.수상)}건</span></h2>" + 수상폼
        + "<div class='scroll'><table data-name='수상'>"
        "<tr><th>상</th><th>대회명 / 주관처</th><th style='width:100px'>연월</th>"
        + ("<th style='width:60px'></th>" if 수정가능 else "") + "</tr>"
        + 수상행 + "</table></div>"
        + "<p class='muted'>표의 <b>수상실적</b> 열에는 <code>"
        + html.escape(rec.수상_요약() or "무슨 상, 대회명 ('yy.m)")
        + "</code> 처럼 최근 것부터 들어갑니다.</p>"
        if (rec.수상 or 수정가능) else "")

    센것 = {**rec.논문_수(registry), **rec.특허_수()}
    출원수 = rec.특허_출원_건수()
    심사중수 = sum(1 for v in 논문보기 if v["게재상태"] == "심사중")
    실적카드 = ""
    if 논문보기 or rec.특허 or rec.수상 or 수정가능:
        논문머리 = ("<tr><th style='width:90px'>저자</th><th style='width:80px'>유형</th>"
                 "<th>제목 / 제출처</th><th style='width:80px'>연도</th>"
                 "<th style='width:80px'>국내해외</th>"
                 "<th style='width:90px'>게재상태</th><th style='width:70px'>등급</th>"
                 + ("<th style='width:60px'></th>" if 수정가능 else "") + "</tr>")
        논문표 = ("<div class='scroll'><table data-name='논문'>"
               + 논문머리 + 논문행 + "</table></div>")
        특허머리 = ("<tr><th style='width:80px'>상태</th>"
                 "<th style='width:100px'>국가</th>"
                 "<th style='width:90px'>국내/해외</th><th>제목</th>"
                 "<th style='width:80px'>연도</th><th style='width:150px'>번호</th>"
                 + ("<th style='width:60px'></th>" if 수정가능 else "") + "</tr>")
        논문폼 = 특허폼 = ""
        if 수정가능:
            # 논문은 **줄이 여럿인 목록**이라 지원자 정보 폼(saveform)과 따로
            # 받는다. 한 폼에 담으면 줄 번호와 항목 번호가 섞인다.
            논문폼 = (
                "<form method='post' action='/candidate/papers' id='paperform'"
                " class='mergebar'>"
                f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
                f"<input type='hidden' name='끝' value='{len(rec.논문) + 1}'>"
                "<button type='submit'>논문 목록 저장</button>"
                "<span class='muted'>맨 아랫줄에 적으면 <b>새 논문</b>이 됩니다. "
                "제출처를 비우면 그 줄은 저장되지 않습니다.</span></form>"
            )
            특허폼 = (
                "<form method='post' action='/candidate/patents' id='patentform'"
                " class='mergebar'>"
                f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
                f"<input type='hidden' name='끝' value='{len(rec.특허) + 1}'>"
                "<button type='submit'>특허 목록 저장</button>"
                "<span class='muted'>맨 아랫줄에 적으면 <b>새 특허</b>가 됩니다. "
                "제목과 번호를 둘 다 비우면 그 줄은 저장되지 않습니다. "
                "<b>국내/해외를 «불명» 으로 두면 개수에서 빠집니다.</b></span></form>"
            )
        실적카드 = (
            "<div class='card'><h2>연구 실적 <span class='muted'>"
            f"저널 {센것['저널_수']}편(주저자 {센것['저널_주저자_수']}) · "
            f"학회 {센것['학회_수']}편(주저자 {센것['학회_주저자_수']}) · "
            f"특허 등록 국내 {센것['특허_등록_국내_수']} · "
            f"해외 {센것['특허_등록_해외_수']}"
            + (f" <span class='muted'>(출원 {출원수}건은 안 셈)</span>"
               if 출원수 else "")
            + (f" · <b>심사중 {심사중수}편은 빼고 셈</b>" if 심사중수 else "")
            + "</span></h2>"
            + 논문폼
            + (논문표 if (논문보기 or 수정가능) else "")
            + (("<h2 style='margin-top:14px'>특허</h2>" + 특허폼
                + "<div class='scroll'><table data-name='특허'>" + 특허머리
                + 특허행 + "</table></div>") if (rec.특허 or 수정가능) else "")
            + 수상표
            + "<p class='muted'>표의 <b>저널_수 · 학회_수 · "
              "특허_등록_국내_수 · 특허_등록_해외_수</b> 열은 "
              "여기 있는 것을 셉니다 — <b>심사중 논문은 빼고</b> 셉니다. "
              "특허는 <b>등록만</b> 세고, <b>같은 제목은 국내·해외 안에서 한 건</b>"
              "으로 셉니다 (같은 발명을 미국·중국에 냈으면 해외 1건). "
              "학회·저널 구분과 등급은 "
              "<a href='/names?kind=" + urllib.parse.quote("학회·저널")
            + "'>명칭 관리</a>에서 판별한 값을 씁니다.</p></div>"
        )

    년도 = store.year_of(지원자_ID)
    if 수정가능:
        번호 += 1
        숨은칸.append(
            f"<input type='hidden' form='saveform' name='항목_{번호}' value='등록년도'>"
            f"<input type='hidden' form='saveform' name='이전_{번호}'"
            f" value='{html.escape(년도)}'>"
            f"<input type='hidden' form='saveform' name='구분_{번호}' value='년도'>"
        )
        년도폼 = (
            f"<input type='text' form='saveform' name='값_{번호}'"
            f" value='{html.escape(년도)}' style='width:90px' placeholder='YYYY'"
            f" data-orig='{html.escape(년도)}' oninput='markDirty(this)'>"
            "<span class='muted'> 아래 <b>고친 내용 저장</b> 으로 함께 저장됩니다.</span>"
        )
    else:
        년도폼 = html.escape(년도)

    되돌리기폼 = (
        "<form method='post' action='/candidate/unpin' id='unpinform'>"
        f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'></form>"
    )
    저장바 = (
        되돌리기폼
        + "<form method='post' action='/candidate/save' id='saveform' class='mergebar'>"
        f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
        f"<input type='hidden' name='끝' value='{번호}'>"
        + "".join(숨은칸)
        + "<button type='submit'>고친 내용 저장</button>"
        "<span class='muted'>여러 칸을 고치고 <b>한 번만</b> 누르세요. "
        "고친 칸은 노랗게 표시됩니다.</span></form>"
        if 수정가능 else ""
    )

    진행 = recruit.get(지원자_ID)
    if 진행.시작함:
        채용줄 = (
            f"<span class='pill p-처리중'>채용 중</span> "
            f"<span class='muted'>{html.escape(진행.채용시작일시)} 시작</span>"
            + ("<form method='post' action='/candidates/stop' style='display:inline'>"
               f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
               "<button class='sec' style='margin-left:8px'>채용 현황에서 내리기</button>"
               "</form>" if can(me, "채용현황_수정") else "")
        )
    else:
        채용줄 = (
            "<span class='pill p-대기중'>인재 Pool</span> "
            "<span class='muted'>아직 채용 절차를 시작하지 않았습니다</span>"
            + ("<form method='post' action='/candidates/start' style='display:inline'>"
               f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
               "<button style='margin-left:8px'>채용 시작</button></form>"
               if can(me, "채용현황_수정") else "")
        )

    관리 = (
        f"<tr><th style='width:170px'>채용</th><td>{채용줄}</td></tr>"
        f"<tr><th>등록 년도</th><td>{년도폼}</td></tr>"
        f"<tr><th>원본 파일명</th><td>{html.escape(meta.get('원본_파일명') or '-')}</td></tr>"
        f"<tr><th>등록 일시</th><td>{html.escape(meta.get('등록일시') or '-')}</td></tr>"
        f"<tr><th>원본 파일 보관</th><td>{'예' if meta.get('원본보유') else '아니오'}</td></tr>"
    )
    중복 = store.duplicate_note(지원자_ID)
    if 중복:
        관리 += f"<tr><th>중복 후보</th><td class='flag' style='white-space:normal'>{html.escape(중복)}</td></tr>"
    관리카드 = (f"<div class='card'><h2>관리 정보</h2><table>{관리}</table></div>"
             if 관리정보 else "")

    매칭 = store.matches(지원자_ID)
    매칭카드 = ""
    # 매칭 결과에는 회사 연구 과제 전체가 들어 있다. 현업에게는 보이면 안 된다.
    if settings.projects_json and can(me, "과제매칭_조회"):
        보여줄수 = max(1, settings.match_show)

        def 매칭줄(m: dict) -> str:
            유사 = ("<br><span class='muted'>임베딩 유사도 "
                  f"{m['유사도']:.2f}</span>" if m.get("유사도") is not None else "")
            점수칸 = (f"<span class='pill {_점수색(m['점수'])}'>{m['점수']}점</span>"
                   f"<br><span class='muted'>{_등급이름(m)}</span>"
                   if m["평가됨"] else
                   "<span class='pill p-실패'>미평가</span>")
            return (
                f"<tr><td>{m['순위']}</td><td><b>{html.escape(m['과제명'])}</b>"
                f"<br><span class='muted'>{html.escape(m['과제키'])}</span>{유사}</td>"
                f"<td>{점수칸}</td>"
                f"<td class='w-xl' title='{html.escape(m['사유'])}'>"
                f"{html.escape(m['사유'])}"
                + ("<br><span class='muted'>근거: "
                   + html.escape(" · ".join(m["근거"])) + "</span>" if m["근거"] else "")
                + "</td></tr>"
            )

        if 매칭:
            위 = "".join(매칭줄(m) for m in 매칭[:보여줄수])
            나머지 = "".join(매칭줄(m) for m in 매칭[보여줄수:])
            더보기 = (
                f"<details><summary class='muted' style='cursor:pointer;padding:6px 0'>"
                f"나머지 {len(매칭) - 보여줄수}개 과제도 보기</summary>"
                "<div class='scroll'><table>"
                "<tr><th style='width:44px'>순위</th><th>과제</th>"
                "<th style='width:92px'>점수</th><th>판단 사유</th></tr>"
                f"{나머지}</table></div></details>"
                if len(매칭) > 보여줄수 else ""
            )
            미평가 = sum(1 for m in 매칭 if not m["평가됨"])
            안내 = (
                f"<p class='muted'>{html.escape(매칭[0]['판단일시'])} 기준 · "
                f"과제 <b>{len(매칭)}개 전부</b>와 비교했습니다"
                + (f" · <span class='flag'>{미평가}개는 모델이 답하지 않아 미평가</span>"
                   if 미평가 else "")
                + "</p>"
            )
        else:
            위, 더보기, 안내 = ("<tr><td colspan='4' class='muted'>"
                             "아직 맞춰보지 않았습니다.</td></tr>", "", "")
        다시 = (
            "<form method='post' action='/match/one' style='display:inline'>"
            f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
            "<button type='submit'>과제와 맞춰보기</button></form>"
            if can(me, "지원자_등록") else ""
        )
        매칭카드 = (
            "<div class='card'><h2>연구 과제 매칭</h2>" + 안내
            + f"<p>{다시} <a class='btn sec' href='/match'>과제 매칭 화면</a></p>"
            "<div class='scroll'><table data-name='과제 매칭'>"
            "<tr><th style='width:44px'>순위</th><th>과제</th>"
            "<th style='width:92px'>점수</th><th>판단 사유</th></tr>"
            + 위 + "</table></div>" + 더보기
            + "<p class='muted'>점수는 아래 눈금으로 매깁니다. "
            "<b>LLM 의 판단이지 측정값이 아닙니다</b> — 순위를 참고하고 "
            "사유와 근거를 사람이 확인하세요.</p>"
            f"<pre class='rubric'>{html.escape(SCORE_RUBRIC)}</pre></div>"
        )

    메일기록 = mailing.history(지원자_ID)
    메일행 = "".join(
        f"<tr><td>{html.escape(m['보낸일시'])}</td>"
        f"<td>{html.escape(m['템플릿이름'])}</td>"
        + ("<td><span class='pill p-내부'>내부</span></td>"
           if (m.get('받는대상') or DEFAULT_RECIPIENT) == '내부'
           else "<td class='muted'>지원자</td>")
        + f"<td>{html.escape(m['받는사람'])}</td>"
        f"<td>{html.escape(m['상태'])}</td>"
        f"<td class='muted' title='{html.escape(m['오류'] or '')}'>"
        f"{html.escape(m['오류'] or '')}</td></tr>"
        for m in 메일기록
    )
    # 이 한 사람에게 바로 보내기. 예전에는 여기서 **이력만** 볼 수 있어서,
    # 상세를 보다가 메일을 보내려면 인재 Pool 로 돌아가 그 사람을 다시 찾아
    # 체크해야 했다. 보내기 화면은 여러 명을 받게 돼 있으니 한 명만 실어 보낸다.
    보내기단추 = ""
    if can(me, "메일_발송"):
        막힘 = mailing.rejected(지원자_ID)
        받는주소 = (row.get("이메일") or "").split(MULTI_SEP)[0].strip()
        if 막힘:
            보내기단추 = ("<p class='muted'>탈락 메일을 보낸 지원자라 "
                      "더는 보낼 수 없습니다.</p>")
        elif not 받는주소 and not any(t.내부 for t in mailing.templates()):
            보내기단추 = ("<p class='muted'>이메일 주소가 없어 보낼 수 없습니다. "
                      "아래 표에서 <b>이메일</b> 을 채우세요.</p>")
        else:
            뒤로 = f"/candidate?id={urllib.parse.quote(지원자_ID)}"
            보내기단추 = (
                f"<form method='post' action='/mail/compose'>"
                f"<input type='hidden' name='ids' value='{html.escape(지원자_ID)}'>"
                f"<input type='hidden' name='back' value='{html.escape(뒤로)}'>"
                f"<button type='submit'>이 지원자에게 메일 보내기</button> "
                + ("<span class='muted'>"
                   + (f"{html.escape(받는주소)} 로 나갑니다. " if 받는주소 else
                      "이메일 주소가 없어 <b>내부 메일</b>만 보낼 수 있습니다. ")
                   + "다음 화면에서 템플릿을 고르고 한 통씩 확인합니다.</span></form>")
            )

    메일카드 = (
        "<div class='card'><h2>메일</h2>"
        + ("<div class='warn'>탈락 메일을 보낸 지원자입니다. "
           "이후 어떤 메일도 보낼 수 없습니다.</div>"
           if mailing.rejected(지원자_ID) else "")
        + 보내기단추
        + ("<div class='scroll'><table data-name='보낸 메일'>"
           "<tr><th>보낸 일시</th><th>템플릿</th><th>구분</th><th>받는 주소</th>"
           "<th>상태</th><th>메모</th></tr>"
           + 메일행 + "</table></div>"
           if 메일기록 else "<p class='muted'>아직 보낸 메일이 없습니다.</p>")
        + "</div>"
    ) if (메일기록 or 보내기단추) else ""

    # 지원자 정보를 고친 것과 **채용 단계를 바꾼 것**을 함께 본다. 사람 눈에는
    # 한 사람에게 일어난 한 가지 일이라, 따로 두면 단계 변경이 어디에도 안 뜬다.
    이력 = audit.for_candidate(지원자_ID) if can(me, "변경이력_조회") else []
    이력행 = "".join(
        f"<tr><td>{html.escape(e.일시)}</td><td>{html.escape(e.사용자)}</td>"
        + ("<td><span class='pill p-처리중'>채용</span></td>"
           if e.대상종류 == "채용현황" else "<td class='muted'>지원자 정보</td>")
        + f"<td title='{html.escape(e.summary())}'>{html.escape(e.summary())}</td></tr>"
        for e in 이력
    ) or "<tr><td colspan=4 class='muted'>아직 수정 내역이 없습니다.</td></tr>"
    이력카드 = (
        "<div class='card'><h2>변경 이력</h2><div class='scroll'><table>"
        "<tr><th>일시</th><th>사용자</th><th>종류</th><th>내용</th></tr>"
        + 이력행 + "</table></div></div>"
        if can(me, "변경이력_조회") else ""
    )

    원본있음 = meta.get("원본보유")
    원본버튼 = (
        f"<a class='btn' href='/candidate/file?id={urllib.parse.quote(지원자_ID)}'>원본 다운로드</a> "
        if 원본있음 else ""
    )
    재분석 = (
        "<form method='post' action='/candidate/reanalyze' style='display:inline'>"
        f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
        "<button type='submit'>다시 분석</button></form> "
        if 원본있음 and 수정가능 else ""
    )
    삭제 = (
        "<form method='post' action='/candidate/delete' style='display:inline'"
        " onsubmit=\"return confirm('이 지원자를 삭제합니다. 되돌릴 수 없습니다.')\">"
        f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
        "<button type='submit' class='danger ghost'>지원자 삭제</button></form>"
        if can(me, "지원자_삭제") else ""
    )
    오류 = _알림(err=error)

    첨부목록 = "".join(
        f"<li><a href='/attachment?id={a['id']}'>{html.escape(a['파일명'])}</a>"
        f" <span class='muted'>{html.escape(a['올린일시'])} · {html.escape(a['올린이'] or '-')}</span>"
        + (
            " <form method='post' action='/attachment/delete' style='display:inline'"
            " onsubmit=\"return confirm('첨부파일을 삭제합니다.')\">"
            f"<input type='hidden' name='id' value='{a['id']}'>"
            f"<input type='hidden' name='cid' value='{html.escape(지원자_ID)}'>"
            "<button class='danger ghost'>삭제</button></form>"
            if 수정가능 else ""
        )
        + "</li>"
        for a in store.attachments(지원자_ID)
    ) or "<li class='muted'>첨부파일 없음</li>"
    올리기 = (
        "<form method='post' action='/attachment/add' enctype='multipart/form-data'"
        " style='display:flex;gap:8px;margin-top:10px'>"
        f"<input type='hidden' name='id' value='{html.escape(지원자_ID)}'>"
        "<input type='file' name='files' multiple>"
        "<button type='submit'>첨부 추가</button></form>"
        if 수정가능 else ""
    )
    첨부카드 = (
        f"<div class='card'><h2>첨부파일</h2><ul>{첨부목록}</ul>{올리기}"
        "<p class='muted'>CV 원본과 별개로 자기소개서·포트폴리오 등을 붙일 수 있습니다. "
        "지원자를 삭제하면 함께 지워집니다.</p></div>"
    )

    알림 = _알림(msg=msg)
    return _page(
        f"지원자 {rec.한글_이름 or rec.지원자_ID}",
        f"""{알림}{오류}
        <div class='card'>
          <h2>{html.escape(rec.한글_이름 or '(이름 미상)')}
              <span class='muted'>{html.escape(rec.지원자_ID)}</span></h2>
          <p class='bar'><a class='btn sec' href='/'>← 목록으로</a>
             {원본버튼}{재분석}<span style='flex:1'></span>{삭제}</p>
          {'<p class=muted>수정 권한이 없어 읽기 전용입니다.</p>' if not 수정가능 else ''}
        </div>
        {검토카드}
        {관리카드}
        <div class='card' id='추출결과'><h2>추출 결과</h2>
          {저장바}
          <table>{''.join(항목행)}</table></div>
        {사용자카드}
        {실적카드}
        {매칭카드}
        {첨부카드}
        {메일카드}
        {이력카드}""",
        me=me,
    )


@라우트("GET", '/', 권한=로그인만)
def get_root(self, me, path):
    if not can(me, "지원자_목록"):
        return self._redirect(홈(me))    # 현업은 채용 현황으로
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return self._send(
        _dashboard(
            me,
            q=(params.get("q") or [""])[0],
            review_only=bool(params.get("review")),
            년도=(params.get("year") or [""])[0],
            msg=(params.get("msg") or [""])[0],
        )
    )
    return _없는주소(self)


@라우트("GET", '/upload', 권한='지원자_등록')
def get_upload(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return self._send(_upload_page(me, (params.get("err") or [""])[0]))
    return _없는주소(self)


@라우트("GET", '/upload/template.xlsx', 권한='지원자_등록')
def get_upload_template_xlsx(self, me, path):
    열, 머리 = _엑셀양식열()
    이름 = urllib.parse.quote(bulk.파일이름)
    return self._send(
        bulk.양식(열, 머리),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        extra={"Content-Disposition":
               'attachment; filename="candidate_template.xlsx";'
               f" filename*=UTF-8\'\'{이름}"},
    )
    return _없는주소(self)


@라우트("GET", '/candidate', 권한=로그인만)
def get_candidate(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    if not _볼수있나(me, (params.get("id") or [""])[0]):
        return self._deny("배정된 과제의 지원자만 볼 수 있습니다.")
    cid = (params.get("id") or [""])[0]
    return self._send(_candidate_page(cid, me, (params.get("err") or [""])[0],
                                      (params.get("msg") or [""])[0]))
    return _없는주소(self)


@라우트("GET", '/candidate/file', 권한=로그인만)
def get_candidate_file(self, me, path):
    if not _볼수있나(
        me,
        (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
         .get("id") or [""])[0],
    ):
        return self._deny("배정된 과제의 지원자만 볼 수 있습니다.")
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    cid = (params.get("id") or [""])[0]
    fpath = store.file_path(cid) if cid else None
    if fpath is None:
        return self._send(
            _page("없음", "<div class='card'>보관된 원본이 없습니다.</div>"), code=404
        )
    meta = store.meta(cid) or {}
    download_name = meta.get("원본_파일명") or fpath.name
    ctype = CONTENT_TYPES.get(fpath.suffix.lower(), "application/octet-stream")
    quoted = urllib.parse.quote(download_name)
    return self._send(
        fpath.read_bytes(), ctype,
        extra={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )
    return _없는주소(self)


@라우트("GET", '/attachment', 권한=로그인만)
def get_attachment(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    try:
        att = store.attachment(int((params.get("id") or ["0"])[0]))
    except ValueError:
        att = None
    if not att:
        return self._send(_page("없음", "<div class='card'>첨부파일이 없습니다.</div>"),
                          code=404)
    if not _볼수있나(me, att["지원자_ID"]):
        return self._deny("배정된 과제의 지원자만 볼 수 있습니다.")
    fpath = store.files_dir / att["저장명"]
    if not fpath.is_file():
        return self._send(_page("없음", "<div class='card'>파일이 사라졌습니다.</div>"),
                          code=404)
    ctype = CONTENT_TYPES.get(fpath.suffix.lower(), "application/octet-stream")
    quoted = urllib.parse.quote(att["파일명"])
    return self._send(
        fpath.read_bytes(), ctype,
        extra={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
    )
    return _없는주소(self)


@라우트("GET", '/status/rows', 권한='지원자_등록')
def get_status_rows(self, me, path):
    # 현황 표 조각만. 페이지를 통째로 다시 그리면 고르던 파일이 풀린다.
    return self._send(_status_table().encode("utf-8"))
    return _없는주소(self)


@라우트("GET", '/export.xlsx', 권한='엑셀_다운로드')
def get_export_xlsx(self, me, path):
    # 화면에 걸어 둔 검색 조건을 **그대로** 따른다. 걸러 놓고 받았는데
    # 전체가 나오면 엉뚱한 사람에게 자료가 나간다.
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    records = store.list_filtered(
        (params.get("q") or [""])[0],
        bool(params.get("review")),
        (params.get("year") or [""])[0],
        registry=registry,
    )
    열 = 엑셀열()
    data = records_to_xlsx(records, registry,
                           (store.field_names(), _표값맵()),
                           열=열, 라벨=라벨(열))
    stamp = now_kst().strftime("%Y%m%d_%H%M")
    return self._send(
        data,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        extra={"Content-Disposition": f'attachment; filename="cv_{stamp}.xlsx"'},
    )
    return _없는주소(self)


@라우트("POST", '/upload', 권한='지원자_등록')
def post_upload(self, me, path):
    form = parse_multipart(self._read_body(), self.headers.get("Content-Type", ""))
    if not form.files:
        return self._redirect("/upload")
    for f in form.files:
        safe_name = safe_filename(f.filename)
        suffix = Path(safe_name).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            _set_status(safe_name, "실패",
                        f"지원하지 않는 형식: {suffix or '(확장자 없음)'}")
            continue
        try:
            cid = f"CV-{uuid.uuid4().hex[:8].upper()}"
            저장명 = store.store_file(cid, safe_name, f.content)
            _enqueue(safe_name, cid, 저장명)
        except Exception as exc:  # noqa: BLE001
            _set_status(safe_name, "실패", f"{type(exc).__name__}: {exc}")
    return self._redirect("/upload")
    return _없는주소(self)


@라우트("POST", '/upload/xlsx', 권한='지원자_등록')
def post_upload_xlsx(self, me, path):
    # 결과를 그 자리에서 그린다. 빠진 줄 목록은 주소창에 담을 길이가
    # 아니라, 리다이렉트하면 "몇 명 등록" 만 남고 이유가 사라진다.
    form = parse_multipart(self._read_body(), self.headers.get("Content-Type", ""))
    올린것 = [f for f in form.files if f.content]
    if not 올린것:
        return self._redirect("/upload?err=" + urllib.parse.quote(
            "채운 엑셀 파일을 고른 뒤 올려 주세요."))
    try:
        만든것, 빠진것, 모르는것 = _엑셀등록(올린것[0].content, me)
    except XlsxError as exc:
        return self._redirect("/upload?err=" + urllib.parse.quote(str(exc)))
    return self._send(_page(
        "엑셀로 지원자 추가", _엑셀결과카드(만든것, 빠진것, 모르는것), me=me))
    return _없는주소(self)


@라우트("POST", '/table.xlsx', 권한=로그인만)
def post_table_xlsx(self, me, path):
    data = urllib.parse.parse_qs(
        self._read_body().decode("utf-8", "replace"), keep_blank_values=True
    )
    이름 = ((data.get("name") or ["표"])[0] or "표").strip()[:40]
    stamp = now_kst().strftime("%Y%m%d_%H%M")
    # 한글 파일명은 RFC 5987 로 따로 보낸다 (옛 브라우저는 ASCII 이름을 쓴다)
    한글 = urllib.parse.quote(f"{이름}_{stamp}.xlsx")
    return self._send(
        _tsv_to_xlsx((data.get("tsv") or [""])[0]),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        extra={"Content-Disposition":
               f'attachment; filename="table_{stamp}.xlsx";'
               f" filename*=UTF-8''{한글}"},
    )
    return _없는주소(self)


@라우트("POST", '/candidate/custom', 권한='지원자_수정')
def post_candidate_custom(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    cid = (data.get("id") or [""])[0]
    필드명 = (data.get("항목") or [""])[0]
    뒤로 = f"/candidate?id={urllib.parse.quote(cid)}"
    field = store.field(필드명)
    if not field:
        return self._redirect(뒤로)
    try:
        저장값 = validate_custom(field, (data.get("새값") or [""])[0])
    except ValidationError as exc:
        return self._redirect(f"{뒤로}&err={urllib.parse.quote(str(exc))}")
    이전 = store.set_custom(cid, 필드명, 저장값)
    if 이전 != 저장값:
        audit.record(me.아이디, "지원자", cid, 항목=필드명, 이전값=이전, 새값=저장값)
    return self._redirect(뒤로)
    return _없는주소(self)


@라우트("POST", '/candidate/new', 권한='지원자_등록')
def post_candidate_new(self, me, path):
    rec = store.create_blank()
    audit.record(me.아이디, "지원자", rec.지원자_ID, 비고="CV 없이 직접 등록")
    return self._redirect(f"/candidate?id={urllib.parse.quote(rec.지원자_ID)}")
    return _없는주소(self)


@라우트("POST", '/attachment/add', 권한='지원자_수정')
def post_attachment_add(self, me, path):
    form = parse_multipart(self._read_body(), self.headers.get("Content-Type", ""))
    cid = (form.fields.get("id") or "").strip()
    뒤로 = f"/candidate?id={urllib.parse.quote(cid)}"
    for f in form.files:
        이름 = safe_filename(f.filename)
        try:
            store.add_attachment(cid, 이름, f.content, me.아이디)
            audit.record(me.아이디, "지원자", cid, 항목="첨부파일", 새값=이름)
        except ValueError as exc:
            return self._redirect(f"{뒤로}&err={urllib.parse.quote(str(exc))}")
    return self._redirect(뒤로)
    return _없는주소(self)


@라우트("POST", '/attachment/delete', 권한='지원자_수정')
def post_attachment_delete(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    cid = (data.get("cid") or [""])[0]
    try:
        이름 = store.delete_attachment(int((data.get("id") or ["0"])[0]))
    except ValueError:
        이름 = ""
    if 이름:
        audit.record(me.아이디, "지원자", cid, 항목="첨부파일 삭제", 이전값=이름)
    return self._redirect(f"/candidate?id={urllib.parse.quote(cid)}")
    return _없는주소(self)


@라우트("POST", '/api/cell', 권한='지원자_수정', json=True, 거부말='수정 권한이 없습니다.')
def post_api_cell(self, me, path):
    # 표에서 칸 하나만 고친다. 상세 화면의 /candidate/edit 과 같은
    # 검사·같은 낙관적 잠금·같은 이력을 탄다. 다른 점은 응답이 JSON 이라
    # 페이지를 새로 그리지 않는다는 것뿐이다.
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    cid = (data.get("id") or [""])[0]
    항목 = (data.get("항목") or [""])[0]
    새값 = (data.get("새값") or [""])[0]
    이전값 = (data.get("이전값") or [""])[0]
    scope = (data.get("scope") or ["기본"])[0]

    if scope == "사용자":
        field = store.field(항목)
        if not field:
            return self._json({"ok": False, "error": f"없는 열입니다: {항목}"}, code=404)
        현재 = store.custom_values(cid).get(항목, "")
        if 현재 != 이전값:
            return self._json({"ok": False, "error": str(
                ConflictError(항목, 현재, 이전값))}, code=409)
        try:
            저장값 = validate_custom(field, 새값, 항목 in store.긴글열())
        except ValidationError as exc:
            return self._json({"ok": False, "error": str(exc)}, code=400)
        이전 = store.set_custom(cid, 항목, 저장값)
        if 이전 != 저장값:
            audit.record(me.아이디, "지원자", cid, 항목=항목,
                         이전값=이전, 새값=저장값, 비고="표에서 수정")
        # 수식 열을 비웠으면 다시 수식 값이 보여야 한다.
        수식값 = bool((field.get("수식") or "").strip()) and not 저장값.strip()
        표시 = _프로필값(cid).get(항목, "") if 수식값 else 저장값
        return self._json({"ok": True, "raw": 저장값, "표시": 표시, "수식": 수식값})

    rec = store.get(cid)
    if rec is None:
        return self._json({"ok": False, "error": "지원자를 찾을 수 없습니다."}, code=404)
    try:
        전, 후 = edit_field(rec, 항목, 새값, 기대_이전값=이전값,
                          registry=registry,
                          긴글=항목 in store.긴글열())
    except ConflictError as exc:
        return self._json({"ok": False, "error": str(exc)}, code=409)
    except ValidationError as exc:
        return self._json({"ok": False, "error": str(exc)}, code=400)
    if 전 != 후:
        store.save(rec)
        audit.record(me.아이디, "지원자", cid, 항목=항목,
                     이전값=전, 새값=후, 비고="표에서 수정")
    # 칸이 다음에 되보낼 «이전 값» 이므로 화면에 뜨는 값이어야 한다.
    return self._json({"ok": True, "raw": 후, "표시": 후})
    return _없는주소(self)


@라우트("POST", '/candidate/save', 권한='지원자_수정')
def post_candidate_save(self, me, path):
    # 상세 화면 한 폼 전체. 줄마다 저장 단추가 있으면 하나 고치고
    # 다른 칸으로 넘어갈 때 앞의 수정이 조용히 날아간다.
    data = urllib.parse.parse_qs(
        self._read_body().decode("utf-8", "replace"), keep_blank_values=True
    )
    cid = (data.get("id") or [""])[0]
    rec = store.get(cid)
    뒤로 = f"/candidate?id={urllib.parse.quote(cid)}"
    if rec is None:
        return self._redirect("/")
    try:
        끝 = int((data.get("끝") or ["0"])[0])
    except ValueError:
        끝 = 0

    바뀐것: list[str] = []
    문제: list[str] = []
    레코드바뀜 = False
    긴글열 = store.긴글열()
    for i in range(1, 끝 + 1):
        항목 = (data.get(f"항목_{i}") or [""])[0]
        if not 항목:
            continue
        새값 = (data.get(f"값_{i}") or [""])[0]
        이전값 = (data.get(f"이전_{i}") or [""])[0]
        # 브라우저는 폼을 보낼 때 줄바꿈을 CRLF 로 바꾼다. 맞춰 놓고
        # 견주지 않으면 여러 줄 칸이 손 안 대도 매번 바뀐 것이 된다.
        if N.lines(새값) == N.lines(이전값):
            continue
        구분 = (data.get(f"구분_{i}") or [""])[0]
        if 구분 == "년도":
            옛 = store.year_of(cid)
            try:
                store.set_year(cid, 새값)
            except ValueError as exc:
                문제.append(str(exc))
                continue
            if 옛 != 새값.strip():
                바뀐것.append("등록년도")
                audit.record(me.아이디, "지원자", cid, 항목="등록년도",
                             이전값=옛, 새값=새값.strip())
            continue
        if 구분 == "추가":
            field = store.field(항목)
            if field is None:
                continue
            try:
                저장값 = validate_custom(field, 새값, 항목 in 긴글열)
            except ValidationError as exc:
                문제.append(str(exc))
                continue
            옛값 = store.set_custom(cid, 항목, 저장값)
            if 옛값 != 저장값:
                바뀐것.append(항목)
                audit.record(me.아이디, "지원자", cid, 항목=항목,
                             이전값=옛값, 새값=저장값)
            continue
        try:
            전, 후 = edit_field(rec, 항목, 새값, 기대_이전값=이전값,
                              registry=registry,
                              긴글=항목 in 긴글열)
        except (ValidationError, ConflictError) as exc:
            문제.append(str(exc))
            continue
        if 전 != 후:
            레코드바뀜 = True
            바뀐것.append(항목)
            audit.record(me.아이디, "지원자", cid, 항목=항목,
                         이전값=전, 새값=후)
    if 레코드바뀜:
        store.save(rec)

    if 문제:
        return self._redirect(
            f"{뒤로}&err=" + urllib.parse.quote(" / ".join(문제[:3]))
            + "#추출결과")
    if not 바뀐것:
        return self._redirect(f"{뒤로}&msg="
                              + urllib.parse.quote("바뀐 내용이 없습니다.")
                              + "#추출결과")
    보임 = ", ".join(바뀐것[:6]) + (" 외" if len(바뀐것) > 6 else "")
    return self._redirect(
        f"{뒤로}&msg=" + urllib.parse.quote(f"{len(바뀐것)}개 저장했습니다 — {보임}")
        + "#추출결과")
    return _없는주소(self)


@라우트("POST", '/candidate/review/done', 권한='지원자_수정')
@라우트("POST", '/candidate/review/undo', 권한='지원자_수정')
def post_candidate_review_done(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    cid = (data.get("id") or [""])[0]
    사유 = (data.get("사유") or [""])[0]
    rec = store.get(cid)
    뒤로 = f"/candidate?id={urllib.parse.quote(cid)}#검토"
    if rec is None or not 사유:
        return self._redirect(뒤로)
    끝냄 = path.endswith("/done")
    if 끝냄:
        store.mark_reviewed(cid, 사유, 본사람=me.아이디)
    else:
        store.unmark_reviewed(cid, 사유)
    # 남은 게 없으면 검토_필요를 내린다. 화면·표·엑셀이 같이 따라온다.
    남은 = review.flagged(rec.검토_사유, store.review_done(cid))
    if rec.검토_필요 != 남은:
        rec.검토_필요 = 남은
        store.save(rec)
    audit.record(me.아이디, "지원자", cid, 항목="검토",
                 이전값="" if 끝냄 else "확인함",
                 새값="확인함" if 끝냄 else "",
                 비고=review.short(사유, 80))
    return self._redirect(뒤로)
    return _없는주소(self)


@라우트("POST", '/candidate/edit', 권한='지원자_수정')
def post_candidate_edit(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    cid = (data.get("id") or [""])[0]
    항목 = (data.get("항목") or [""])[0]
    새값 = (data.get("새값") or [""])[0]
    이전값 = (data.get("이전값") or [""])[0]
    rec = store.get(cid)
    뒤로 = f"/candidate?id={urllib.parse.quote(cid)}"
    if rec is None:
        return self._redirect(뒤로)
    try:
        전, 후 = edit_field(rec, 항목, 새값, 기대_이전값=이전값,
                          registry=registry)
    except (ValidationError, ConflictError) as exc:
        return self._redirect(f"{뒤로}&err={urllib.parse.quote(str(exc))}")
    if 전 != 후:
        store.save(rec)
        audit.record(me.아이디, "지원자", cid, 항목=항목, 이전값=전, 새값=후)
    return self._redirect(뒤로)
    return _없는주소(self)


@라우트("POST", '/candidate/papers', 권한='지원자_수정')
def post_candidate_papers(self, me, path):
    # 논문 목록 통째로 받기. LLM 이 틀린 것을 고치는 유일한 길이라
    # 재분석 없이 여기서 끝나야 한다.
    data = urllib.parse.parse_qs(
        self._read_body().decode("utf-8", "replace"), keep_blank_values=True
    )
    cid = (data.get("id") or [""])[0]
    뒤로 = f"/candidate?id={urllib.parse.quote(cid)}"
    rec = store.get(cid)
    if rec is None:
        return self._redirect("/")
    try:
        끝 = int((data.get("끝") or ["0"])[0])
    except ValueError:
        끝 = 0

    옛것 = list(rec.논문)
    새목록: list[Paper] = []
    고침 = 지움 = 더함 = 0
    for i in range(1, 끝 + 1):
        if (data.get(f"del_{i}") or [""])[0]:
            지움 += 1
            continue
        한줄 = {칸: (data.get(f"{칸}_{i}") or [""])[0]
              for 칸 in ("제목", "제출처", "연도", "유형",
                        "국내해외", "저자구분", "게재상태")}
        try:
            논문 = edit.validate_paper(한줄)
        except ValidationError as exc:
            return self._redirect(
                f"{뒤로}&err={urllib.parse.quote(f'{i}번째 줄 — {exc}')}#실적")
        if 논문 is None:          # 제출처가 빈 줄 (추가용 빈 줄 포함)
            continue
        옛줄 = 옛것[i - 1] if i <= len(옛것) else None
        if 옛줄 is None:
            더함 += 1
        elif 논문.model_dump() != 옛줄.model_dump():
            고침 += 1
        새목록.append(논문)

    if 고침 or 지움 or 더함:
        rec.논문 = 새목록
        # 새로 적어 넣은 제출처를 사전에 등록한다. 안 부르면 방금 넣은
        # 학회가 미분류로도 안 잡혀 등급을 매길 수가 없다.
        observe_record(rec, registry)
        store.save(rec)
        요약 = " · ".join(
            x for x in (f"{고침}줄 고침" if 고침 else "",
                        f"{더함}줄 추가" if 더함 else "",
                        f"{지움}줄 삭제" if 지움 else "") if x)
        # 한 번 저장에 한 줄만 남긴다. 줄마다 남기면 변경 이력이
        # 논문 목록으로 뒤덮인다.
        audit.record(me.아이디, "지원자", cid, 항목="논문",
                     이전값=f"{len(옛것)}편", 새값=f"{len(새목록)}편",
                     비고=요약)
        return self._redirect(
            f"{뒤로}&msg={urllib.parse.quote('논문 목록: ' + 요약)}#실적")
    return self._redirect(
        f"{뒤로}&msg={urllib.parse.quote('바뀐 내용이 없습니다.')}#실적")
    return _없는주소(self)


@라우트("POST", '/candidate/patents', 권한='지원자_수정')
def post_candidate_patents(self, me, path):
    # 특허 목록 통째로 받기. LLM 이 국내/해외를 «불명» 으로 두면 등록
    # 개수에서 빠지는데, 재분석 말고 그것을 고칠 길이 여기뿐이다.
    data = urllib.parse.parse_qs(
        self._read_body().decode("utf-8", "replace"), keep_blank_values=True
    )
    cid = (data.get("id") or [""])[0]
    뒤로 = f"/candidate?id={urllib.parse.quote(cid)}"
    rec = store.get(cid)
    if rec is None:
        return self._redirect("/")
    try:
        끝 = int((data.get("끝") or ["0"])[0])
    except ValueError:
        끝 = 0

    옛것 = list(rec.특허)
    새목록: list[Patent] = []
    고침 = 지움 = 더함 = 0
    for i in range(1, 끝 + 1):
        if (data.get(f"특허del_{i}") or [""])[0]:
            지움 += 1
            continue
        한줄 = {칸: (data.get(f"특허{칸}_{i}") or [""])[0]
              for 칸 in ("제목", "상태", "연도", "번호", "국가", "국내해외")}
        try:
            특허 = edit.validate_patent(한줄)
        except ValidationError as exc:
            return self._redirect(
                f"{뒤로}&err={urllib.parse.quote(f'{i}번째 줄 — {exc}')}#실적")
        if 특허 is None:          # 제목·번호가 다 빈 줄 (추가용 빈 줄)
            continue
        옛줄 = 옛것[i - 1] if i <= len(옛것) else None
        if 옛줄 is None:
            더함 += 1
        elif 특허.model_dump() != 옛줄.model_dump():
            고침 += 1
        새목록.append(특허)

    if 고침 or 지움 or 더함:
        rec.특허 = 새목록
        store.save(rec)
        요약 = " · ".join(
            x for x in (f"{고침}줄 고침" if 고침 else "",
                        f"{더함}줄 추가" if 더함 else "",
                        f"{지움}줄 삭제" if 지움 else "") if x)
        # 논문과 같은 이유로 한 번 저장에 한 줄만 남긴다.
        audit.record(me.아이디, "지원자", cid, 항목="특허",
                     이전값=f"{len(옛것)}건", 새값=f"{len(새목록)}건",
                     비고=요약)
        return self._redirect(
            f"{뒤로}&msg={urllib.parse.quote('특허 목록: ' + 요약)}#실적")
    return self._redirect(
        f"{뒤로}&msg={urllib.parse.quote('바뀐 내용이 없습니다.')}#실적")
    return _없는주소(self)


@라우트("POST", '/candidate/awards', 권한='지원자_수정')
def post_candidate_awards(self, me, path):
    # 수상 목록 통째로 받기 (특허 목록과 같은 모양).
    data = urllib.parse.parse_qs(
        self._read_body().decode("utf-8", "replace"), keep_blank_values=True
    )
    cid = (data.get("id") or [""])[0]
    뒤로 = f"/candidate?id={urllib.parse.quote(cid)}"
    rec = store.get(cid)
    if rec is None:
        return self._redirect("/")
    try:
        끝 = int((data.get("끝") or ["0"])[0])
    except ValueError:
        끝 = 0

    옛것 = list(rec.수상)
    새목록 = []
    고침 = 지움 = 더함 = 0
    for i in range(1, 끝 + 1):
        if (data.get(f"수상del_{i}") or [""])[0]:
            지움 += 1
            continue
        한줄 = {칸: (data.get(f"수상{칸}_{i}") or [""])[0] for 칸 in ("상명", "수여처", "연월")}
        try:
            상 = edit.validate_award(한줄)
        except ValidationError as exc:
            return self._redirect(
                f"{뒤로}&err={urllib.parse.quote(f'수상 {i}번째 줄 — {exc}')}#실적")
        if 상 is None:            # 상 이름이 빈 줄 (추가용 빈 줄)
            continue
        옛줄 = 옛것[i - 1] if i <= len(옛것) else None
        if 옛줄 is None:
            더함 += 1
        elif 상.model_dump() != 옛줄.model_dump():
            고침 += 1
        새목록.append(상)

    if 고침 or 지움 or 더함:
        rec.수상 = 새목록
        store.save(rec)
        요약 = " · ".join(
            x for x in (f"{고침}줄 고침" if 고침 else "",
                        f"{더함}줄 추가" if 더함 else "",
                        f"{지움}줄 삭제" if 지움 else "") if x)
        audit.record(me.아이디, "지원자", cid, 항목="수상",
                     이전값=f"{len(옛것)}건", 새값=f"{len(새목록)}건", 비고=요약)
        return self._redirect(
            f"{뒤로}&msg={urllib.parse.quote('수상 목록: ' + 요약)}#실적")
    return self._redirect(
        f"{뒤로}&msg={urllib.parse.quote('바뀐 내용이 없습니다.')}#실적")
    return _없는주소(self)


@라우트("POST", '/candidate/unpin', 권한='지원자_수정')
def post_candidate_unpin(self, me, path):
    # 손으로 정해 둔 값을 버리고 다시 명칭 관리를 따라가게 한다.
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    cid = (data.get("id") or [""])[0]
    항목 = (data.get("col") or [""])[0]
    뒤로 = f"/candidate?id={urllib.parse.quote(cid)}"
    rec = store.get(cid)
    if rec is None or 항목 not in REGISTRY_FIELDS:
        return self._redirect(뒤로)
    전, 후 = 사전_따라가기(rec, 항목, registry)
    if 전 != 후:
        store.save(rec)
        audit.record(me.아이디, "지원자", cid, 항목=항목,
                     이전값=전, 새값=후, 비고="사전 따라가기")
    return self._redirect(뒤로 + "#추출결과")
    return _없는주소(self)


@라우트("POST", '/candidate/year', 권한='지원자_수정')
def post_candidate_year(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    cid = (data.get("id") or [""])[0]
    년도 = (data.get("년도") or [""])[0]
    뒤로 = f"/candidate?id={urllib.parse.quote(cid)}"
    옛 = store.year_of(cid)
    try:
        store.set_year(cid, 년도)
    except ValueError as exc:
        return self._redirect(f"{뒤로}&err={urllib.parse.quote(str(exc))}")
    if 옛 != 년도:
        audit.record(me.아이디, "지원자", cid, 항목="등록년도", 이전값=옛, 새값=년도)
    return self._redirect(뒤로)
    return _없는주소(self)


@라우트("POST", '/candidate/delete', 권한='지원자_삭제')
def post_candidate_delete(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    cid = (data.get("id") or [""])[0]
    if cid:
        store.delete(cid)
        recruit.delete(cid)
        audit.record(me.아이디, "지원자", cid, 비고="지원자 삭제")
    return self._redirect("/")
    return _없는주소(self)


@라우트("POST", '/candidates/delete', 권한='지원자_삭제')
def post_candidates_delete(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    ids = data.get("ids") or []
    if ids:
        store.delete_many(ids)       # 원본·첨부파일까지 함께 지운다
        for cid in ids:
            recruit.delete(cid)      # 채용 현황에 유령 줄이 남지 않게
            audit.record(me.아이디, "지원자", cid, 비고="지원자 삭제")
    return self._redirect("/")
    return _없는주소(self)


@라우트("POST", '/candidates/start', 권한='채용현황_수정', 거부말='채용 시작은 채용담당자 이상만 할 수 있습니다.')
@라우트("POST", '/candidates/stop', 권한='채용현황_수정', 거부말='채용 시작은 채용담당자 이상만 할 수 있습니다.')
def post_candidates_start(self, me, path):
    # 인재 Pool 에 있는 사람을 채용 현황으로 올리고 내린다.
    # 줄마다 있는 단추는 id 하나, 묶음 단추는 ids 여럿을 보낸다.
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    ids = (data.get("ids") or []) + [
        i for i in (data.get("id") or []) if i
    ]
    시작 = path.endswith("/start")
    보이는 = auth.visible_project_ids(me)
    한것: list[str] = []
    for cid in dict.fromkeys(ids):
        if store.get(cid) is None:
            continue
        if 보이는 is not None and recruit.get(cid).project_id not in 보이는:
            continue
        바뀜 = (recruit.start(cid, me.아이디) if 시작
              else recruit.stop(cid, me.아이디))
        if 바뀜:
            한것.append(cid)
            audit.record(me.아이디, "채용현황", cid, 항목="채용 절차",
                         이전값="" if 시작 else "채용 중",
                         새값="채용 중" if 시작 else "",
                         비고="채용 시작" if 시작 else "채용 현황에서 내림")
    if not 한것:
        return self._redirect("/?msg=" + urllib.parse.quote(
            "고를 사람을 먼저 체크하세요." if not ids else "이미 그 상태입니다."))
    말 = (f"{len(한것)}명 채용을 시작했습니다. 채용 현황에서 이어서 관리하세요."
         if 시작 else f"{len(한것)}명을 채용 현황에서 내렸습니다. "
                     "진행 상황은 지우지 않았습니다.")
    return self._redirect("/?msg=" + urllib.parse.quote(말))
    return _없는주소(self)


@라우트("POST", '/candidates/purge', 권한='지원자_삭제')
def post_candidates_purge(self, me, path):
    지운것 = store.purge_expired()
    for cid in 지운것:
        recruit.delete(cid)
        audit.record(me.아이디, "지원자", cid, 비고="보관기간 만료 삭제")
    return self._redirect("/")
    return _없는주소(self)


@라우트("POST", '/candidate/reanalyze', 권한='지원자_등록')
def post_candidate_reanalyze(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    cid = (data.get("id") or [""])[0]
    meta = store.meta(cid) if cid else None
    if not meta or not meta.get("저장_파일명"):
        return self._redirect(f"/candidate?id={urllib.parse.quote(cid)}")
    name = meta.get("원본_파일명") or cid
    # 재분석하면 사유가 새로 나온다. 옛 '확인함' 기록은 무효다.
    store.clear_reviews(cid)
    _enqueue(name, cid, meta["저장_파일명"], "재분석")
    return self._redirect("/upload")
    return _없는주소(self)


@라우트("POST", '/status/clear', 권한='지원자_등록')
def post_status_clear(self, me, path):
    with _status_lock:
        _status.clear()
    return self._redirect("/upload")
    return _없는주소(self)
