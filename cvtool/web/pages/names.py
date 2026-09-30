"""명칭 관리 (소속 · 학회·저널 · 전공).
"""

from __future__ import annotations

import html
import urllib.parse

from ...auth import User
from ...names import AUTO_CAREER, AUTO_SAME_NAME, canonical_kind, GRADED_KINDS, KINDS, SUBTYPES

from ..state import audit, registry
from ..layout import _page, _알림, _없는주소
from ..router import 라우트


def _names_page(종류: str, me: User | None = None,
                error: str = "", msg: str = "", 안본것만: bool = False,
                보기: str = "표기") -> bytes:
    """소속·학회·저널·전공을 같은 화면에서 관리한다.

    **CV 에 적힌 표기마다 한 줄**이다. 여러 표기를 한 줄로 합쳐 대표명만 남기면,
    잘못 분류한 걸 나중에 알아채도 무엇이 잘못 들어갔는지 볼 수도 떼어낼 수도
    없었다. 지금은 그 줄의 이름만 고치면 된다.

    등급·국내해외·유형·IF 는 표기가 아니라 **이름**에 붙는다. 같은 이름을 쓰는
    표기들은 저절로 같은 분류를 쓴다.
    """
    종류 = canonical_kind(종류)
    if 종류 not in KINDS:
        종류 = "학회·저널"
    전부 = registry.list_all(종류)           # 표시명 오름차순이 기본
    등급목록 = registry.tier_names()
    등급종류 = 종류 in GRADED_KINDS

    # 아직 사람이 안 본 줄을 **위로** 올린다. 할 일이 화면 아래로 밀려나면
    # 스무 줄만 넘어도 못 보고 지나친다. (그 안에서는 원래 순서 그대로)
    안본것 = [i for i in 전부 if not i.확인]
    items = 안본것 if 안본것만 else (안본것 + [i for i in 전부 if i.확인])

    무리: dict[str, list] = {}
    for i in 전부:
        무리.setdefault(i.표시명, []).append(i)

    탭 = " ".join(
        f"<a class='btn {'' if k == 종류 else 'sec'}' href='/names?kind={urllib.parse.quote(k)}"
        f"{'&view=name' if 보기 == '이름' else ''}'>"
        f"{k}"
        + (f" <span class='pill p-안본것'>{registry.unconfirmed_count(k)}</span>"
           if registry.unconfirmed_count(k) else "")
        + "</a>"
        for k in KINDS
    )

    등급열 = ""
    if 등급종류:
        체크 = "".join(
            f"<label style='margin-right:14px'><input type='checkbox' name='tier'"
            f" value='{html.escape(t['이름'])}'{' checked' if t['표에_표시'] else ''}>"
            f"{html.escape(t['이름'])}</label>"
            for t in registry.tiers()
            if t["이름"] != "미분류"
        )
        등급열 = (
            "<div class='card'><h2>표에 개수 열로 낼 등급</h2>"
            "<form method='post' action='/names/tiers'>"
            f"<input type='hidden' name='kind' value='{html.escape(종류)}'>"
            f"{체크}<button type='submit'>저장</button></form>"
            "<p class='muted'>켠 등급마다 표에 열이 둘 생깁니다 — "
            "<code>1저자_해외논문_(등급)</code> 은 <b>몇 편</b>인지, "
            "<code>1저자_(등급)_제출처</code> 는 <b>어디에 냈는지</b>를 "
            "최근 것부터 적습니다 "
            "(<code>CVPR ('24) 1저자, Nano Energy ('23, IF 17.9) 1저자</code>).</p>"
            "</div>"
        )

    이름목록 = registry.display_names(종류)
    이름옵션 = "".join(f"<option value='{html.escape(n)}'>" for n in 이름목록)
    아직안내 = "아직 사람이 안 본 줄입니다 (LLM 이 넣어 둔 그대로)"

    rows = []
    for i in items:
        형제 = [x.원표기 for x in 무리.get(i.표시명, []) if x.id != i.id]
        형제칸 = (
            "<span class='muted'>" + html.escape(", ".join(형제)) + "</span>"
            if 형제 else "<span class='muted'>-</span>"
        )
        등급칸 = ""
        if 등급종류:
            유형opt = "".join(
                f"<option{' selected' if s == i.유형 else ''}>{html.escape(s)}</option>"
                for s in SUBTYPES
            )
            등급opt = "".join(
                f"<option{' selected' if g == i.등급 else ''}>{html.escape(g)}</option>"
                for g in 등급목록
            )
            해외opt = "".join(
                f"<option{' selected' if v == i.국내해외 else ''}>{v}</option>"
                for v in ("불명", "해외", "국내")
            )
            등급칸 = (
                f"<td class='ctl'><select form='saveform' name='유형_{i.id}'"
                f" data-orig='{html.escape(i.유형)}' onchange='markDirty(this)'>{유형opt}"
                f"</select></td>"
                f"<td class='ctl'><select form='saveform' name='등급_{i.id}'"
                f" data-orig='{html.escape(i.등급)}' onchange='markDirty(this)'>{등급opt}</select></td>"
                f"<td class='ctl'><select form='saveform' name='국내해외_{i.id}'"
                f" data-orig='{html.escape(i.국내해외)}' onchange='markDirty(this)'>{해외opt}"
                f"</select></td>"
                f"<td class='ctl'><input type='text' form='saveform' name='IF_{i.id}'"
                f" value='{html.escape(i.IF)}' style='width:64px' placeholder='예: 12.5'"
                f" data-orig='{html.escape(i.IF)}' oninput='markDirty(this)'>"
                f" <a href='{html.escape(i.google_url())}' target='_blank' rel='noopener'"
                f" title='구글에서 &quot;{html.escape(i.표시명)} impact factor&quot; 검색'>찾기</a>"
                f"</td>"
            )
        미분류표시 = (
            " <span class='pill p-미분류'>미분류</span>"
            if 등급종류 and i.등급 == "미분류" else ""
        )
        # 확인칸 — 이 줄을 **사람이 봤는가**. 안 본 줄은 LLM 이 넣어 둔 그대로다.
        본때 = (f"{i.확인일시} {i.확인자}".strip() if i.확인 else "")
        확인칸 = (
            f"<td class='ctl' title='{html.escape(본때) or 아직안내}'>"
            f"<label><input type='checkbox' form='saveform' name='확인_{i.id}'"
            f"{' checked' if i.확인 else ''} data-orig='{'y' if i.확인 else ''}'"
            f" onchange='markDirty(this)'> "
            + (f"<span class='muted'>{html.escape(i.확인일시)}</span>" if i.확인
               else "<b class='flag'>확인</b>")
            + "</label></td>"
        )
        고르기 = (f"<td class='ctl'><input type='checkbox' form='saveform' name='pick'"
                f" value='{i.id}' title='합칠 줄로 고르기'></td>")
        rows.append(
            f"<tr class='{'' if i.확인 else 'needs'}'>"
            f"{고르기}{확인칸}"
            f"<td title='{html.escape(i.원표기)}'>{html.escape(i.원표기)}{미분류표시}</td>"
            f"<td>{i.발견횟수}</td>"
            f"<td class='ctl'>"
            f"<input type='hidden' form='saveform' name='id' value='{i.id}'>"
            f"<input type='text' form='saveform' name='표시명_{i.id}' list='이름목록'"
            f" value='{html.escape(i.표시명)}' style='width:220px'"
            f" data-orig='{html.escape(i.표시명)}' oninput='markDirty(this)'></td>"
            f"<td>{형제칸}</td>"
            f"{등급칸}"
            f"<td><form method='post' action='/names/forget'"
            f" onsubmit=\"return window.confirm('이 표기를 사전에서 지웁니다. "
            f"다시 CV 에 나오면 새로 등록됩니다.')\">"
            f"<input type='hidden' name='kind' value='{html.escape(종류)}'>"
            f"<input type='hidden' name='id' value='{i.id}'>"
            f"<button class='danger'>지움</button></form></td></tr>"
        )

    저장바 = (
        f"<form method='post' action='/names/save' id='saveform' class='mergebar'>"
        f"<input type='hidden' name='kind' value='{html.escape(종류)}'>"
        f"<input type='hidden' name='todo' value='{'1' if 안본것만 else ''}'>"
        f"<button type='submit'>고친 내용 저장</button>"
        f"<span class='muted'>여러 줄을 고친 뒤 <b>한 번만</b> 누르세요. "
        f"고친 칸은 노랗게 표시됩니다. <b>고친 줄은 저절로 확인 표시</b>가 됩니다."
        f"</span>"
        f"<span style='flex-basis:100%'></span>"
        f"<b>합치기</b> <span class='muted'>「합칠」 칸을 고른 줄을</span>"
        f"<input type='text' name='merge_to' list='이름목록' style='width:220px'"
        f" placeholder='합칠 이름 (고르거나 적기)'>"
        f"<button type='submit' name='action' value='merge' class='sec'>한 이름으로 합치기</button>"
        f"<span style='flex:1'></span>"
        f"<button type='submit' name='action' value='confirm_all' class='sec'"
        f" onclick=\"return window.confirm('이 목록의 줄을 모두 확인한 것으로 표시합니다.\\n"
        f"(「표에서 찾기」로 가려 둔 줄도 포함됩니다)')\">이 목록 모두 확인</button>"
        f"</form>"
        if items else ""
    )

    등급머리 = (
        "<th class='ctl'>학회/저널</th><th class='ctl'>등급</th>"
        "<th class='ctl'>국내/해외</th><th class='ctl'>Impact Factor</th>"
        if 등급종류 else ""
    )
    표 = (
        "<table><tr><th class='ctl w-sm' title='합칠 줄 고르기'>합칠</th>"
        "<th class='ctl w-sm' title='사람이 보고 맞다고 한 줄'>확인</th>"
        "<th>CV 에 적힌 표기</th><th style='width:56px'>발견</th>"
        f"<th class='ctl'>표에 보일 이름</th><th>같은 이름으로 묶인 표기</th>"
        f"{등급머리}<th></th></tr>{''.join(rows)}</table>"
        if rows
        else ("<p class='muted'>안 본 항목이 없습니다. 전부 확인했습니다.</p>"
              if 안본것만 and 전부 else
              "<p class='muted'>아직 등록된 항목이 없습니다. "
              "CV를 업로드하면 자동으로 등록됩니다.</p>")
    )

    # 안 본 것만 보기 — 표기가 수백 줄이 되면 이게 유일하게 쓸 만한 길이 된다
    주소 = f"/names?kind={urllib.parse.quote(종류)}"
    거르개 = (
        f"<a class='btn {'sec' if 안본것만 else ''}' href='{주소}'>전체 {len(전부)}</a> "
        f"<a class='btn {'' if 안본것만 else 'sec'}' href='{주소}&todo=1'>"
        f"아직 안 본 것 {len(안본것)}</a>"
        if 전부 else ""
    )
    보기전환 = (
        f"<a class='btn {'' if 보기 == '이름' else 'sec'}' href='{주소}&view=name'>"
        f"이름별로 보기</a> "
        f"<a class='btn {'' if 보기 != '이름' else 'sec'}' href='{주소}'>표기별로 보기</a>"
        if 전부 else ""
    )
    if 보기 == "이름":
        거르개 = ""
        저장바, 표 = _names_group_table(종류, 전부, 무리, 등급목록, 등급종류)
    알림 = _알림(msg=msg)
    오류 = _알림(err=error)
    설명 = (
        "학교·회사가 CV 마다 다르게 적혀 있습니다(포항공대 / POSTECH / 포항공과대학교)."
        if 종류 == "소속"
        else "같은 곳이 CV 마다 다르게 적혀 있습니다(ICML / Proc. of ICML 2023)."
    )
    분류설명 = (
        " 등급·국내해외·유형·IF 는 <b>이름에 붙습니다</b> — 같은 이름을 쓰는 표기는"
        " 자동으로 같은 분류가 됩니다."
        if 등급종류 else ""
    )
    return _page(
        f"{종류} 관리",
        f"""{알림}{오류}<div class='card'><h2>명칭 관리</h2><p>{탭}</p>
        <p class='muted'>{설명}
        <b>CV 에 적힌 표기마다 한 줄</b>이고, 각 줄의 <b>표에 보일 이름</b>만 고칩니다.
        같은 곳이면 같은 이름을 적으세요 — 지원자 표에는 그 이름으로 함께 나옵니다.
        잘못 묶였으면 그 줄의 이름만 다시 고치면 됩니다.{분류설명}
        <b>이름별로 보기</b>에서는 이름 하나를 고치면 묶인 표기가 전부 따라가고,
        「합칠」 칸을 골라 <b>한 번에 합칠</b> 수 있습니다.</p>
        <p class='muted'>표기는 CV 에서 발견하는 대로 <b>자동으로</b> 등록되고,
        등급·국내해외는 LLM 이 짐작한 값입니다. 그래서 각 줄에
        <b>확인</b> 칸이 있습니다 — 사람이 보고 맞다고 한 줄은 체크가 켜지고,
        <span class='pill p-안본것'>아직 안 본 줄</span>은 노랗게 남습니다.
        값을 고쳐서 저장하면 그 줄은 저절로 확인 처리됩니다.
        이미 확인한 이름을 물려받은 새 표기와 경력 목록에만 나온 회사는
        할 일로 세지 않습니다.</p></div>
        {등급열}
        <div class='card'><h2>{html.escape(종류)} <span class='muted'>표기 {len(전부)}개 ·
        이름 {len(무리)}개</span></h2>
        <p class='bar'>{보기전환}<span style='flex:1'></span>{거르개}</p>
        {저장바}
        <div class='scroll'>{표}</div>
        <datalist id='이름목록'>{이름옵션}</datalist></div>""",
        me=me,
    )


def _names_group_table(종류: str, 전부: list, 무리: dict, 등급목록: list[str],
                       등급종류: bool) -> tuple[str, str]:
    """이름별 화면 — **이름 하나당 한 줄**. (저장바, 표) 를 돌려준다.

    표기별 화면에서 이름을 바꾸면 그 줄만 떨어져 나가서, 표기가 다섯인 이름을
    바꾸려면 다섯 번 고쳐야 했다. 여기서 이름을 고치면 묶인 표기가 전부 따라간다.
    다른 이름과 같게 적으면 두 그룹이 합쳐진다.
    """
    if not 무리:
        return "", "<p class='muted'>아직 등록된 항목이 없습니다.</p>"
    # 안 본 표기가 있는 이름을 위로, 그 안에서는 이름 오름차순 (list_all 순서)
    이름들 = list(무리)
    이름들.sort(key=lambda n: all(x.확인 for x in 무리[n]))
    rows = []
    for g, 이름 in enumerate(이름들):
        표기들 = 무리[이름]
        안본 = sum(1 for x in 표기들 if not x.확인)
        대표 = 표기들[0]
        분류칸 = ""
        if 등급종류:
            def 고르개(열: str, 값들, 현재: str) -> str:
                opts = "".join(
                    f"<option{' selected' if v == 현재 else ''}>{html.escape(v)}</option>"
                    for v in 값들)
                return (f"<td class='ctl'><select form='groupform' name='{열}_{g}'"
                        f" data-orig='{html.escape(현재)}' onchange='markDirty(this)'>"
                        f"{opts}</select></td>")
            분류칸 = (
                고르개("유형", SUBTYPES, 대표.유형)
                + 고르개("등급", 등급목록, 대표.등급)
                + 고르개("국내해외", ("불명", "해외", "국내"), 대표.국내해외)
                + f"<td class='ctl'><input type='text' form='groupform' name='IF_{g}'"
                f" value='{html.escape(대표.IF)}' style='width:64px' placeholder='예: 12.5'"
                f" data-orig='{html.escape(대표.IF)}' oninput='markDirty(this)'>"
                f" <a href='{html.escape(대표.google_url())}' target='_blank'"
                f" rel='noopener'>찾기</a></td>"
            )
        표기글 = ", ".join(
            html.escape(x.원표기) + (f" <span class='muted'>({x.발견횟수})</span>")
            for x in 표기들)
        확인칸 = (
            f"<td class='ctl'><label><input type='checkbox' form='groupform'"
            f" name='확인_{g}'{'' if 안본 else ' checked'}"
            f" data-orig='{'' if 안본 else 'y'}'> "
            + (f"<b class='flag'>안 본 표기 {안본}</b>" if 안본
               else "<span class='muted'>확인</span>")
            + "</label></td>"
        )
        rows.append(
            f"<tr class='{'needs' if 안본 else ''}'>"
            f"<td class='ctl'><input type='checkbox' form='groupform' name='pick'"
            f" value='{g}' title='합칠 이름으로 고르기'></td>"
            f"{확인칸}"
            f"<td class='ctl'><input type='hidden' form='groupform' name='g'"
            f" value='{html.escape(이름)}'>"
            f"<input type='text' form='groupform' name='이름_{g}' list='이름목록'"
            f" value='{html.escape(이름)}' style='width:220px'"
            f" data-orig='{html.escape(이름)}' oninput='markDirty(this)'></td>"
            f"<td>{len(표기들)}</td>"
            f"<td>{sum(x.발견횟수 for x in 표기들)}</td>"
            f"<td>{표기글}</td>"
            f"{분류칸}</tr>"
        )
    등급머리 = (
        "<th class='ctl'>학회/저널</th><th class='ctl'>등급</th>"
        "<th class='ctl'>국내/해외</th><th class='ctl'>Impact Factor</th>"
        if 등급종류 else ""
    )
    표 = (
        "<table><tr><th class='ctl w-sm' title='합칠 이름 고르기'>합칠</th>"
        "<th class='ctl' title='켜면 이 이름의 표기를 전부 확인한 것으로 표시'>확인</th>"
        "<th class='ctl'>표에 보일 이름</th><th style='width:56px'>표기</th>"
        "<th style='width:56px'>발견</th><th>묶인 표기 (발견 횟수)</th>"
        f"{등급머리}</tr>{''.join(rows)}</table>"
    )
    저장바 = (
        f"<form method='post' action='/names/save_groups' id='groupform' class='mergebar'>"
        f"<input type='hidden' name='kind' value='{html.escape(종류)}'>"
        f"<button type='submit'>고친 내용 저장</button>"
        f"<span class='muted'>이름을 고치면 <b>묶인 표기가 전부</b> 따라갑니다. "
        f"다른 이름과 똑같이 적으면 두 이름이 <b>합쳐집니다</b>. "
        f"잘못 묶인 표기 하나만 떼려면 «표기별로 보기» 에서 그 줄만 고치세요.</span>"
        f"<span style='flex-basis:100%'></span>"
        f"<b>합치기</b> <span class='muted'>「합칠」 칸을 고른 이름들을</span>"
        f"<input type='text' name='merge_to' list='이름목록' style='width:220px'"
        f" placeholder='합칠 이름 (고르거나 적기)'>"
        f"<button type='submit' name='action' value='merge' class='sec'>한 이름으로 합치기</button>"
        f"</form>"
    )
    return 저장바, 표


@라우트("GET", '/names', 권한='명칭_관리')
def get_names(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return self._send(_names_page(
        (params.get("kind") or ["학회"])[0],
        me,
        error=(params.get("err") or [""])[0],
        msg=(params.get("msg") or [""])[0],
        안본것만=bool((params.get("todo") or [""])[0]),
        보기="이름" if (params.get("view") or [""])[0] == "name" else "표기",
    ))
    return _없는주소(self)


@라우트("POST", '/names/save', 권한='명칭_관리')
def post_names_save(self, me, path):
    # 빈칸도 받아야 IF 를 지울 수 있다
    data = urllib.parse.parse_qs(
        self._read_body().decode("utf-8", "replace"), keep_blank_values=True
    )
    kind = canonical_kind((data.get("kind") or ["학회·저널"])[0])
    뒤로 = f"/names?kind={urllib.parse.quote(kind)}"
    if (data.get("todo") or [""])[0]:
        뒤로 += "&todo=1"

    할일 = (data.get("action") or [""])[0]
    모두확인 = 할일 == "confirm_all"

    # 화면에 있던 줄 전부가 들어온다. 실제로 값이 달라진 것만 저장한다.
    바뀐것: list[str] = []
    확인바뀜 = 0
    for 원시 in data.get("id") or []:
        try:
            nid = int(원시)
        except (ValueError, TypeError):
            continue
        이전 = registry.get(nid)
        if 이전 is None:
            continue
        registry.classify(
            nid,
            표시명=(data.get(f"표시명_{nid}") or [None])[0],
            등급=(data.get(f"등급_{nid}") or [None])[0],
            국내해외=(data.get(f"국내해외_{nid}") or [None])[0],
            유형=(data.get(f"유형_{nid}") or [None])[0],
            IF=(data.get(f"IF_{nid}") or [""])[0] if f"IF_{nid}" in data else None,
        )
        이후 = registry.get(nid)
        if 이후 is None:
            continue
        변경 = [
            (항목, 옛, 새)
            for 항목, 옛, 새 in (
                ("표에 보일 이름", 이전.표시명, 이후.표시명),
                ("학회/저널", 이전.유형, 이후.유형),
                ("등급", 이전.등급, 이후.등급),
                ("국내해외", 이전.국내해외, 이후.국내해외),
                ("IF", 이전.IF, 이후.IF),
            )
            if 옛 != 새
        ]
        for 항목, 옛, 새 in 변경:
            audit.record(me.아이디, "명칭", f"{kind}:{이후.원표기}",
                         항목=항목, 이전값=옛, 새값=새)
        if 변경:
            이름변경 = [v for v in 변경 if v[0] == "표에 보일 이름"]
            머리 = (f"{이전.표시명} → {이후.표시명}" if 이름변경 else 이후.표시명)
            나머지 = [f"{항목} {새}" for 항목, _, 새 in 변경 if 항목 != "표에 보일 이름"]
            바뀐것.append(f"{이후.원표기}: " + 머리
                        + (f" ({', '.join(나머지)})" if 나머지 else ""))

        # 확인 표시. 체크칸을 켰거나, **값을 실제로 고쳤으면** 본 것이다.
        # 고쳐 놓고 체크를 깜박하면 그 줄이 영영 '안 본 것' 으로 남는다.
        # «보이는 줄 모두 확인» 은 화면의 줄을 전부 켠 것과 같다.
        켬 = bool(data.get(f"확인_{nid}")) or bool(변경) or 모두확인
        if 켬 and not 이전.확인:
            registry.confirm(nid, 사람=me.아이디)
            확인바뀜 += 1
            audit.record(me.아이디, "명칭", f"{kind}:{이후.원표기}",
                         항목="확인", 이전값="", 새값="확인함")
        elif not 켬 and 이전.확인:
            registry.unconfirm(nid)
            확인바뀜 += 1
            audit.record(me.아이디, "명칭", f"{kind}:{이후.원표기}",
                         항목="확인", 이전값="확인함", 새값="")

    if 할일 == "merge":
        # 고친 칸을 먼저 저장했으니, 합치기가 그 위에 덮인다.
        대상 = (data.get("merge_to") or [""])[0].strip()
        고른것 = []
        for 원시 in data.get("pick") or []:
            try:
                고른것.append(int(원시))
            except (ValueError, TypeError):
                continue
        if not 대상 or not 고른것:
            return self._redirect(f"{뒤로}&err=" + urllib.parse.quote(
                "합칠 줄(「합칠」 칸)과 합칠 이름을 모두 정해 주세요."))
        이전들 = {i: registry.get(i) for i in 고른것}
        합친것 = registry.merge(고른것, 대상)
        for nid in 합친것:
            이전 = 이전들[nid]
            audit.record(me.아이디, "명칭", f"{kind}:{이전.원표기}",
                         항목="표에 보일 이름", 이전값=이전.표시명, 새값=대상)
            if not 이전.확인 or 이전.확인자 in (AUTO_SAME_NAME, AUTO_CAREER):
                registry.confirm(nid, 사람=me.아이디)
        앞말 = f"{len(바뀐것)}건 저장, " if 바뀐것 else ""
        return self._redirect(f"{뒤로}&msg=" + urllib.parse.quote(
            f"{앞말}{len(합친것)}줄을 '{대상}' 로 합쳤습니다."
            + (f" ({len(고른것) - len(합친것)}줄은 이미 그 이름)"
               if len(고른것) > len(합친것) else "")))

    if not 바뀐것 and 확인바뀜:
        return self._redirect(f"{뒤로}&msg=" + urllib.parse.quote(
            f"{확인바뀜}줄의 확인 표시를 바꿨습니다."))
    if not 바뀐것:
        return self._redirect(f"{뒤로}&msg=" + urllib.parse.quote("바뀐 내용이 없습니다."))
    보임 = ", ".join(바뀐것[:5]) + (" 외" if len(바뀐것) > 5 else "")
    꼬리 = f" (확인 표시 {확인바뀜}줄)" if 확인바뀜 else ""
    return self._redirect(f"{뒤로}&msg=" + urllib.parse.quote(
        f"{len(바뀐것)}건 저장했습니다 — {보임}{꼬리}"))
    return _없는주소(self)


@라우트("POST", '/names/save_groups', 권한='명칭_관리')
def post_names_save_groups(self, me, path):
    data = urllib.parse.parse_qs(
        self._read_body().decode("utf-8", "replace"), keep_blank_values=True
    )
    kind = canonical_kind((data.get("kind") or ["학회·저널"])[0])
    뒤로 = f"/names?kind={urllib.parse.quote(kind)}&view=name"
    그룹들 = data.get("g") or []
    바뀐것: list[str] = []
    확인한것 = 0

    def 표기들(이름: str) -> list:
        return [n for n in registry.list_all(kind) if n.표시명 == 이름]

    def 확인(이름: str) -> int:
        # 안 본 줄만. 다 본 이름은 확인 칸이 켜진 채로 들어오므로,
        # 자동 확인 줄까지 건드리면 저장할 때마다 확인자가 사람으로 바뀐다.
        n개 = 0
        for n in 표기들(이름):
            if not n.확인:
                registry.confirm(n.id, 사람=me.아이디)
                n개 += 1
        return n개

    # 이름별 줄마다: 분류 -> 이름 순서로 저장한다. 이름을 먼저 바꾸면
    # 분류가 새 이름에 붙어야 하는지 옛 이름에 붙어야 하는지 헷갈린다.
    for g, 옛이름 in enumerate(그룹들):
        새이름 = ((data.get(f"이름_{g}") or [옛이름])[0] or "").strip() or 옛이름
        변경 = []
        if f"등급_{g}" in data or f"IF_{g}" in data:
            전 = registry.class_of(kind, 옛이름)
            새값 = {열: (data.get(f"{열}_{g}") or [None])[0]
                   for 열 in ("유형", "등급", "국내해외", "IF")
                   if f"{열}_{g}" in data}
            registry.set_class(kind, 옛이름, **새값)
            후 = registry.class_of(kind, 옛이름)
            변경 = [(열, 전[열], 후[열]) for 열 in ("유형", "등급", "국내해외", "IF")
                  if 전[열] != 후[열]]
        if 새이름 != 옛이름:
            for n in 표기들(옛이름):
                audit.record(me.아이디, "명칭", f"{kind}:{n.원표기}",
                             항목="표에 보일 이름", 이전값=옛이름, 새값=새이름)
            registry.rename_group(kind, 옛이름, 새이름)
        for 열, 전값, 후값 in 변경:
            audit.record(me.아이디, "명칭", f"{kind}:{옛이름}",
                         항목={"유형": "학회/저널"}.get(열, 열),
                         이전값=전값, 새값=후값)
        if 새이름 != 옛이름 or 변경:
            바뀐것.append(
                (f"{옛이름} → {새이름}" if 새이름 != 옛이름 else 새이름)
                + (f" ({', '.join(f'{열} {후값}' for 열, _, 후값 in 변경)})"
                   if 변경 else ""))
        # 고쳤거나 확인 칸을 켰으면 그 이름의 표기를 모두 본 것이다
        if 새이름 != 옛이름 or 변경 or data.get(f"확인_{g}"):
            확인한것 += 확인(새이름)

    if (data.get("action") or [""])[0] == "merge":
        대상 = (data.get("merge_to") or [""])[0].strip()
        고른 = []
        for 원시 in data.get("pick") or []:
            try:
                고른.append(그룹들[int(원시)])
            except (ValueError, TypeError, IndexError):
                continue
        if not 대상 or not 고른:
            return self._redirect(f"{뒤로}&err=" + urllib.parse.quote(
                "합칠 이름(「합칠」 칸)과 합칠 대상 이름을 모두 정해 주세요."))
        # 위에서 이름을 바꿨을 수 있다. 바뀐 이름을 따라간다.
        고른 = [((data.get(f"이름_{그룹들.index(x)}") or [x])[0] or x).strip()
               for x in 고른]
        합친수 = 0
        for 옛이름 in dict.fromkeys(고른):
            if 옛이름 == 대상:
                continue
            for n in 표기들(옛이름):
                audit.record(me.아이디, "명칭", f"{kind}:{n.원표기}",
                             항목="표에 보일 이름", 이전값=옛이름, 새값=대상)
            합친수 += registry.rename_group(kind, 옛이름, 대상)
        확인한것 += 확인(대상)
        앞말 = f"{len(바뀐것)}건 저장, " if 바뀐것 else ""
        return self._redirect(f"{뒤로}&msg=" + urllib.parse.quote(
            f"{앞말}표기 {합친수}줄을 '{대상}' 로 합쳤습니다."))

    if not 바뀐것:
        return self._redirect(f"{뒤로}&msg=" + urllib.parse.quote(
            f"{확인한것}줄을 확인 표시했습니다." if 확인한것 else "바뀐 내용이 없습니다."))
    보임 = ", ".join(바뀐것[:5]) + (" 외" if len(바뀐것) > 5 else "")
    return self._redirect(f"{뒤로}&msg=" + urllib.parse.quote(
        f"{len(바뀐것)}건 저장했습니다 — {보임}"))
    return _없는주소(self)


@라우트("POST", '/names/forget', 권한='명칭_관리')
def post_names_forget(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    kind = canonical_kind((data.get("kind") or ["학회·저널"])[0])
    뒤로 = f"/names?kind={urllib.parse.quote(kind)}"
    try:
        지운표기 = registry.forget(int((data.get("id") or ["0"])[0]))
    except (ValueError, TypeError):
        지운표기 = ""
    if not 지운표기:
        return self._redirect(뒤로)
    audit.record(me.아이디, "명칭", f"{kind}:{지운표기}", 비고="표기 삭제")
    return self._redirect(f"{뒤로}&msg=" + urllib.parse.quote(
        f"'{지운표기}' 표기를 사전에서 지웠습니다."))
    return _없는주소(self)


@라우트("POST", '/names/tiers', 권한='열_구성', 거부말='표 열 구성은 관리자만 바꿀 수 있습니다.')
def post_names_tiers(self, me, path):
    data = urllib.parse.parse_qs(self._read_body().decode("utf-8", "replace"))
    kind = canonical_kind((data.get("kind") or ["학회"])[0])
    켠것 = set(data.get("tier") or [])
    for t in registry.tiers():
        if t["이름"] != "미분류":
            registry.set_tier_column(t["이름"], t["이름"] in 켠것)
    return self._redirect(f"/names?kind={urllib.parse.quote(kind)}")
    return _없는주소(self)
