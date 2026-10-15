"""오늘 할 일 — 여러 탭을 돌아다니며 찾던 «내가 처리할 것» 을 한 화면에.

네 가지를 모은다.

1. **검토할 CV** — 자동으로 읽다 헷갈린 곳이 남은 사람
2. **안 보낸 메일** — 보내야 하는 때가 된 사람 중 아직 못 받은 사람
3. **멈춘 채용** — 같은 단계에 오래 머문 사람 (며칠인지는 고른다)
4. **부서·과제 미배정** — 채용을 시작했는데 어디로 갈지 안 정한 사람

여기서는 **보여 주기만** 한다. 고치는 일은 링크를 따라가 원래 화면에서 한다 —
같은 일을 하는 단추가 두 군데 있으면 둘 중 하나는 꼭 낡는다.
"""

from __future__ import annotations

import html
import urllib.parse
from datetime import datetime

from ... import review
from ...auth import User
from ...timeutil import now_kst

from ..layout import _page
from ..mail_targets import _안보낸것
from ..router import 라우트
from ..state import mailing, recruit, store

#: 멈춘 채용을 몇 날 기준으로 볼지 고르는 값
머문날_고르기 = (7, 14, 30)
기본_머문날 = 14


def _며칠전(때: str, 지금: datetime) -> int | None:
    try:
        그때 = datetime.strptime(때[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return (지금.replace(tzinfo=None) - 그때).days


def _이름(cid: str, 이름표: dict[str, str]) -> str:
    return (f"<a href='/candidate?id={urllib.parse.quote(cid)}'>"
            f"{html.escape(이름표.get(cid) or cid)}</a>")


def _칸(제목: str, 수: int, 설명: str, 표: str, 더보기: str = "", 열쇠: str = "") -> str:
    머리 = (f"<h2>{html.escape(제목)} "
          + (f"<span class='pill p-안본것'>{수}</span>" if 수 else
             "<span class='pill p-완료'>없음</span>")
          + "</h2>")
    속 = (f"<p class='muted'>{설명}</p>" + 표 + 더보기) if 수 else \
        "<p class='muted'>처리할 것이 없습니다.</p>"
    return f"<div class='card todo' id='{열쇠}'>{머리}{속}</div>"


def 할일(me: User, 머문날: int = 기본_머문날) -> dict[str, list]:
    """화면과 테스트가 같이 쓰는 셈. {갈래: [줄...]}"""
    지금 = now_kst()
    records = store.list_all()
    이름표 = {r.지원자_ID: (r.한글_이름 or r.영문_이름 or r.지원자_ID) for r in records}
    끝낸검토 = store.review_done_map()

    검토 = []
    for r in records:
        if r.검토_필요 != "Y":
            continue
        남은 = review.remaining(r.검토_사유, 끝낸검토.get(r.지원자_ID, set()))
        if 남은:
            검토.append((r.지원자_ID, 남은))

    메일 = []
    for tpl in mailing.templates():
        if not tpl.조건들:
            continue
        남은것 = _안보낸것(tpl, me)
        if 남은것:
            메일.append((tpl, 남은것))

    진행 = recruit.all()
    움직인때 = recruit.last_moved()
    멈춤, 미배정 = [], []
    for cid, p in 진행.items():
        if not p.시작함 or cid not in 이름표:
            continue
        if not p.부서_id:
            미배정.append((cid, _며칠전(p.채용시작일시, 지금)))
        if p.탈락 or p.최종상태 == "최종 합격":
            continue
        날 = _며칠전(움직인때.get(cid, ""), 지금)
        if 날 is not None and 날 >= 머문날:
            멈춤.append((cid, p.최종상태, 날))
    멈춤.sort(key=lambda x: -x[2])
    return {"이름표": 이름표, "검토": 검토, "메일": 메일, "멈춤": 멈춤, "미배정": 미배정}


def _todo_page(me: User, 머문날: int) -> bytes:
    자료 = 할일(me, 머문날)
    이름표 = 자료["이름표"]

    검토줄 = "".join(
        f"<tr><td class='pname'>{_이름(cid, 이름표)}</td>"
        f"<td>{html.escape(' · '.join(review.short(x, 40) for x in 남은[:3]))}"
        + (f" <span class='muted'>외 {len(남은) - 3}건</span>" if len(남은) > 3 else "")
        + "</td></tr>"
        for cid, 남은 in 자료["검토"][:50])
    검토칸 = _칸(
        "검토할 CV", len(자료["검토"]),
        "자동으로 읽다가 확신하지 못한 곳이 남은 사람입니다. 열어서 원본과 비교해 "
        "<b>확인함</b> 을 누르면 여기서 빠집니다.",
        f"<table><tr><th>이름</th><th>확인할 것</th></tr>{검토줄}</table>",
        "<p><a class='btn sec' href='/?review=1'>인재 Pool 에서 검토 필요만 보기</a></p>"
        if len(자료["검토"]) > 50 else "", "review")

    메일수 = sum(len(남은) for _t, 남은 in 자료["메일"])
    메일줄 = "".join(
        f"<tr><td><b>{html.escape(tpl.이름)}</b></td>"
        f"<td>{', '.join(_이름(cid, 이름표) for cid in list(남은)[:8])}"
        + (f" <span class='muted'>외 {len(남은) - 8}명</span>" if len(남은) > 8 else "")
        + f"</td><td><a class='btn' href='/mail/log?tpl={tpl.id}'>보내러 가기</a></td></tr>"
        for tpl, 남은 in 자료["메일"])
    메일칸 = _칸(
        "안 보낸 메일", 메일수,
        "템플릿에 정해 둔 <b>보내야 하는 때</b>가 됐는데 아직 못 받은 사람입니다.",
        f"<table><tr><th>템플릿</th><th>받을 사람</th><th></th></tr>{메일줄}</table>",
        열쇠="mail")

    고르기 = "".join(
        f"<option value='{n}'{' selected' if n == 머문날 else ''}>{n}일</option>"
        for n in 머문날_고르기)
    멈춤줄 = "".join(
        f"<tr><td class='pname'>{_이름(cid, 이름표)}</td>"
        f"<td>{html.escape(상태)}</td><td>{날}일째</td></tr>"
        for cid, 상태, 날 in 자료["멈춤"])
    멈춤칸 = _칸(
        "멈춘 채용", len(자료["멈춤"]),
        f"채용 중인데 <b>{머문날}일 넘게</b> 단계가 그대로인 사람입니다. "
        "불합격·최종 합격은 빼고 셉니다.",
        f"<table><tr><th>이름</th><th>지금 단계</th><th>그대로인 날</th></tr>{멈춤줄}</table>",
        "<p><a class='btn sec' href='/recruit'>채용 현황으로</a></p>", "stuck")

    미배정줄 = "".join(
        f"<tr><td class='pname'>{_이름(cid, 이름표)}</td>"
        f"<td class='muted'>{'' if 날 is None else f'{날}일 전 채용 시작'}</td></tr>"
        for cid, 날 in 자료["미배정"])
    미배정칸 = _칸(
        "부서·과제 미배정", len(자료["미배정"]),
        "채용을 시작했지만 부서가 정해지지 않은 사람입니다. 채용 현황에서 고르세요.",
        f"<table><tr><th>이름</th><th></th></tr>{미배정줄}</table>",
        "<p><a class='btn sec' href='/recruit'>채용 현황으로</a></p>", "nodept")

    합계 = len(자료["검토"]) + 메일수 + len(자료["멈춤"]) + len(자료["미배정"])
    머리 = (
        "<div class='card'><h1 class='pt'>오늘 할 일 "
        + (f"<span class='pill p-안본것'>{합계}</span>" if 합계 else "")
        + "</h1><p class='muted'>여러 화면에 흩어진 «처리할 것» 을 모았습니다. "
        "이름을 누르면 그 사람이, 단추를 누르면 처리할 화면이 열립니다.</p>"
        "<form method='get' action='/todo' class='bar'>"
        f"<label>멈춘 채용 기준 <select name='days' onchange='this.form.submit()'>{고르기}"
        "</select></label></form></div>"
    )
    return _page("오늘 할 일", 머리 + 검토칸 + 메일칸 + 멈춤칸 + 미배정칸, me=me)


@라우트("GET", "/todo", 권한="지원자_등록")
def get_todo(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    try:
        머문날 = int((params.get("days") or [""])[0])
    except ValueError:
        머문날 = 기본_머문날
    if 머문날 not in 머문날_고르기:
        머문날 = 기본_머문날
    return self._send(_todo_page(me, 머문날))
