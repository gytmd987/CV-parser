"""과제로 사람 찾기 — 과제를 고르면 인재 Pool 전체를 맞는 순서로.

매칭 결과는 지원자 상세에서 **한 사람씩** 보게 돼 있었다. 새 과제가 열렸을 때
궁금한 것은 반대 방향이다 — «예전 지원자 중에 이 과제에 맞는 사람이 누구지?»
그래서 이미 저장된 매칭 점수를 과제 기준으로 모아 줄을 세운다. LLM 을 새로
부르지 않는다 (안 맞춰 본 사람은 «아직» 으로 보이고, 맞춰 보는 단추는
`과제 정보 (매칭)` 화면에 있다).

키워드로도 좁힌다. 보유기술 · 연구분야 · 전공 · 논문/특허 제목 · 수상 · 경력에서
찾고, 여러 낱말을 적으면 **모두 들어 있는 사람**만 남긴다.
"""

from __future__ import annotations

import html
import re
import urllib.parse

from ...auth import can, User

from ..jobs import 과제목록
from ..layout import _page
from ..router import 라우트
from ..state import recruit, registry, store
from .match import _등급이름, _점수색

#: 키워드를 찾는 곳 (보이는 이름)
찾는곳 = ("보유기술", "연구분야_키워드", "박사_전공", "석사_전공", "수상실적", "경력_요약",
        "현재_소속")


def _낱말들(q: str) -> list[str]:
    return [w for w in re.split(r"[,\s]+", (q or "").strip().lower()) if w]


def _찾을글(rec, row: dict) -> str:
    조각 = [str(row.get(c) or "") for c in 찾는곳]
    조각 += [p.제목 for p in (rec.논문 or []) if getattr(p, "제목", "")]
    조각 += [p.제목 for p in (rec.특허 or []) if getattr(p, "제목", "")]
    return " ".join(조각).lower()


def 찾기(과제키: str, q: str) -> list[dict]:
    """[{rec, row, 매칭(없으면 None), 맞은낱말}] — 화면과 테스트가 같이 쓴다."""
    낱말 = _낱말들(q)
    매칭 = store.matches_for(과제키) if 과제키 else {}
    나온것 = []
    for rec in store.list_all():
        row = rec.to_row(registry)
        if 낱말:
            글 = _찾을글(rec, row)
            if not all(w in 글 for w in 낱말):
                continue
        나온것.append({"rec": rec, "row": row, "매칭": 매칭.get(rec.지원자_ID)})
    if 과제키:
        나온것.sort(key=lambda x: (x["매칭"] is not None and x["매칭"]["평가됨"],
                                 (x["매칭"] or {}).get("점수", -1)), reverse=True)
    return 나온것


def _강조(글: str, 낱말: list[str]) -> str:
    글 = html.escape(글)
    for w in 낱말:
        글 = re.sub(f"({re.escape(html.escape(w))})", r"<mark>\1</mark>", 글, flags=re.I)
    return 글


def _find_page(me: User, 과제키: str, q: str, 최소: int) -> bytes:
    과제들, 파일오류 = 과제목록()
    고른과제 = next((p for p in 과제들 if p.키 == 과제키), None)
    if 고른과제 is None:
        과제키 = ""
    낱말 = _낱말들(q)
    채용중 = recruit.started()

    옵션 = "<option value=''>(과제 고르기 — 안 고르면 키워드로만)</option>" + "".join(
        f"<option value='{html.escape(p.키)}'{' selected' if p.키 == 과제키 else ''}>"
        f"{html.escape(p.이름)}{' · ' + html.escape(p.담당) if p.담당 else ''}</option>"
        for p in 과제들)
    최소옵션 = "".join(
        f"<option value='{n}'{' selected' if n == 최소 else ''}>{말}</option>"
        for n, 말 in ((0, "점수 전부"), (50, "50점 이상"), (70, "70점 이상 (적합)"),
                      (90, "90점 이상 (매우 적합)")))
    고르기 = (
        "<form method='get' action='/find' class='bar findbar'>"
        f"<select name='key' style='min-width:260px'>{옵션}</select>"
        f"<input type='text' name='q' value='{html.escape(q)}' style='width:260px'"
        " placeholder='키워드 (예: 리소그래피 python)'>"
        + (f"<select name='min'>{최소옵션}</select>" if 과제키 else "")
        + "<button type='submit'>찾기</button>"
        + ("<a class='btn sec' href='/find'>초기화</a>" if (과제키 or q) else "")
        + "</form>"
    )
    if 파일오류:
        고르기 += f"<div class='warn'>{html.escape(파일오류)}</div>"

    if not (과제키 or 낱말):
        본문 = ("<div class='empty'><b>과제를 고르거나 키워드를 적어 보세요.</b>"
              "<span>과제를 고르면 매칭 점수가 높은 사람부터, 키워드를 적으면 "
              "보유기술·연구분야·논문 제목 등에 그 말이 든 사람만 보입니다.</span></div>")
        return _page("과제로 사람 찾기", _머리(고르기) + 본문, me=me)

    결과 = 찾기(과제키, q)
    if 과제키 and 최소:
        결과 = [x for x in 결과 if x["매칭"] and x["매칭"]["평가됨"]
              and x["매칭"]["점수"] >= 최소]
    안맞춰봄 = sum(1 for x in 결과 if 과제키 and x["매칭"] is None)

    줄들 = []
    for x in 결과[:300]:
        rec, row, m = x["rec"], x["row"], x["매칭"]
        cid = rec.지원자_ID
        이름 = rec.한글_이름 or rec.영문_이름 or "(이름 없음)"
        배지 = "<span class='pill p-처리중'>채용 중</span>" if cid in 채용중 else ""
        칸 = [f"<td class='ck'><input type='checkbox' name='ids' value='{html.escape(cid)}'></td>",
             f"<td class='pname'><a href='/candidate?id={urllib.parse.quote(cid)}#매칭'>"
             f"{html.escape(이름)}</a>{배지}</td>"]
        if 과제키:
            if m is None:
                칸.append("<td class='muted'>아직</td><td class='muted'>"
                         "아직 이 과제와 맞춰 보지 않았습니다.</td>")
            else:
                점수 = m.get("점수") or 0
                사유 = m.get("사유") or ""
                칸.append(f"<td><span class='pill {_점수색(점수)}'>{점수}</span> "
                         f"<span class='muted'>{html.escape(_등급이름(m))}</span></td>"
                         f"<td class='wrap' title='{html.escape(사유)}'>"
                         f"{html.escape(사유[:140])}{'…' if len(사유) > 140 else ''}</td>")
        박사 = " ".join(v for v in (row.get("박사_학교"), row.get("박사_전공")) if v)
        칸.append(f"<td class='wrap'>{_강조(str(row.get('보유기술') or ''), 낱말)}</td>"
                 f"<td>{_강조(박사, 낱말)}</td>"
                 f"<td>{html.escape(str(row.get('현재_소속') or ''))}</td>")
        줄들.append("<tr>" + "".join(칸) + "</tr>")

    머리글 = ("<th class='ck'><input type='checkbox' onclick='selectVisible(this)'></th>"
           "<th class='pname'>이름</th>"
           + ("<th>점수</th><th>판단 사유</th>" if 과제키 else "")
           + "<th>보유기술</th><th>박사</th><th>현재 소속</th>")
    묶음 = (("<button formaction='/mail/compose'>선택한 사람에게 메일</button> "
            if can(me, "메일_발송") else "")
           + ("<button formaction='/candidates/start' class='sec'>채용 시작</button>"
              if can(me, "채용현황_수정") else ""))
    요약 = (f"<p class='muted'><b>{len(결과)}명</b>"
          + (f" · {html.escape(고른과제.이름)} 기준 점수 높은 순" if 고른과제 else "")
          + (f" · 키워드 «{html.escape(' '.join(낱말))}» 가 모두 든 사람" if 낱말 else "")
          + (f" · 그중 {안맞춰봄}명은 아직 맞춰 보지 않았습니다 "
             "(<a href='/match'>과제 정보 (매칭)</a> 에서 맞춰 볼 수 있습니다)"
             if 안맞춰봄 else "")
          + (" · 앞의 300명만 보입니다" if len(결과) > 300 else "")
          + "</p>")
    돌아올곳 = "/find?" + urllib.parse.urlencode(
        {k: v for k, v in (("key", 과제키), ("q", q), ("min", str(최소) if 최소 else "")) if v})
    if not 결과:
        표 = ("<div class='empty'><b>맞는 사람이 없어요.</b>"
             "<span>키워드를 줄이거나 점수 기준을 낮춰 보세요.</span></div>")
    else:
        표 = (
            "<form method='post' action='/candidates/start' class='selform'>"
            f"<input type='hidden' name='back' value='{html.escape(돌아올곳)}'>"
            "<div class='selbar'><span class='selnone muted'>왼쪽 칸을 체크하면 메일 보내기 · "
            "채용 시작을 한꺼번에 할 수 있어요.</span>"
            f"<span class='selsome'><b class='selcount'>0명</b> 선택됨 {묶음}</span></div>"
            "<div class='scroll'><table data-name='과제로 찾기'>"
            f"<tr>{머리글}</tr>{''.join(줄들)}</table></div></form>"
        )
    return _page("과제로 사람 찾기", _머리(고르기) + "<div class='card'>" + 요약 + 표
                 + "</div>", me=me)


def _머리(고르기: str) -> str:
    return ("<div class='card'><h1 class='pt'>과제로 사람 찾기</h1>"
            "<p class='muted'>새 과제가 열렸을 때 «예전 지원자 중 누가 맞지?» 를 찾는 곳입니다. "
            "점수는 이미 맞춰 본 결과를 씁니다 — 이름을 누르면 상세에서 판단 근거를 볼 수 있습니다.</p>"
            + 고르기 + "</div>")


@라우트("GET", "/find", 권한="과제매칭_조회", 거부말="과제로 찾기는 채용담당자 이상만 할 수 있습니다.")
def get_find(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    try:
        최소 = int((params.get("min") or ["0"])[0])
    except ValueError:
        최소 = 0
    return self._send(_find_page(me, (params.get("key") or [""])[0],
                                 (params.get("q") or [""])[0], 최소))
