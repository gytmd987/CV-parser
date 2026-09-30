"""누구에게 아직 메일을 안 보냈나 (탭 옆 숫자와 메일 화면이 같이 쓴다).
"""

from __future__ import annotations

from ..auth import can, User
from ..mailing import Template

from .state import auth, mailing, recruit, store


def _조건에맞는사람(조건들: list[str], 진행: dict) -> dict[str, str]:
    """그 상태에 있는 사람들. {지원자_ID: 걸린 조건}

    보는 것은 **지금 상태뿐**이다. `Progress.최종상태` 가 그렇게 만들어져 있어서,
    서류 불합격이던 사람을 되돌리면 목록에서도 빠진다. 지난 상태를 뒤져 소급하지
    않는 이유는 되돌린 것도 사람이 한 판단이기 때문이다.
    """
    if not 조건들:
        return {}
    고른것 = set(조건들)
    시작도 = "채용 시작" in 고른것
    걸린것: dict[str, str] = {}
    for cid, p in 진행.items():
        if not p.시작함:
            continue
        상태 = p.최종상태
        if 상태 in 고른것:
            걸린것[cid] = 상태
        elif 시작도:
            걸린것[cid] = "채용 시작"
    return 걸린것


def _메일문맥(me: User | None) -> dict:
    """안 보낸 것을 셀 때 템플릿마다 다시 읽지 않아도 되는 것들.

    모든 화면이 `_page` 를 거치며 이 셈을 하므로, 템플릿 수만큼 채용 현황과
    지원자 목록을 다시 읽으면 그게 그대로 화면 여는 시간이 된다. 한 번만 읽는다.
    """
    return {
        "진행": recruit.all(),
        "있는사람": store.ids(),
        "탈락자": mailing.rejected_ids(),
        "보이는": auth.visible_project_ids(me) if me else None,
    }


def _안보낸것(tpl: Template, me: User | None = None,
           문맥: dict | None = None) -> dict[str, str]:
    """이 템플릿을 **보냈어야 하는데 안 보낸** 사람들. {지원자_ID: 걸린 조건}

    사람이 기억하고 있어야 했던 것을 대신 세어 준다. 빼는 것은 셋:
    이미 받은 사람 · 보낼 수 없는 사람(탈락 메일을 이미 받았거나) · 그리고 지금
    들어와 있는 사람이 볼 수 없는 지원자. 마지막 것을 빼지 않으면 **현업에게
    배정 안 된 과제의 지원자 수가 배지 숫자로 새어 나간다.**
    """
    문맥 = 문맥 or _메일문맥(me)
    걸린것 = _조건에맞는사람(tpl.조건들, 문맥["진행"])
    if not 걸린것:
        return {}
    보낸사람 = mailing.sent_ids(tpl.id)
    있는사람, 탈락자, 보이는 = 문맥["있는사람"], 문맥["탈락자"], 문맥["보이는"]
    남은것: dict[str, str] = {}
    for cid, 조건 in 걸린것.items():
        if cid in 보낸사람 or cid in 탈락자 or cid not in 있는사람:
            continue
        if 보이는 is not None and 문맥["진행"][cid].project_id not in 보이는:
            continue
        남은것[cid] = 조건
    return 남은것


def _안보낸수(me: User | None) -> dict[int, int]:
    """템플릿마다 안 보낸 사람 수. 조건을 안 정한 템플릿은 아예 세지 않는다.

    모든 화면이 `_page` 를 거치면서 이걸 부르므로, **이 기능을 안 쓰는 동안에는
    셈이 한 번도 돌지 않게** 조건이 붙은 템플릿이 있는지부터 본다.
    """
    if not me or not can(me, "메일_템플릿"):
        return {}
    쓰는것 = [t for t in mailing.templates() if t.조건들]
    if not 쓰는것:
        return {}
    문맥 = _메일문맥(me)
    return {t.id: len(_안보낸것(t, me, 문맥)) for t in 쓰는것}
