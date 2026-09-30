"""표 항목(추가한 열)에 거는 **수식** — `채용중 = =IF(과제<>"","Y","N")`.

규칙은 하나다: **사람이 칸에 적은 값이 있으면 그 값, 비어 있으면 수식 값.**
엑셀에서 수식 칸에 손으로 값을 덮어쓴 것과 같다. 칸을 지우면 다시 수식이 나온다.

수식은 표의 한 줄(한 사람)을 보고 계산한다 — 대시보드 목록·프로필과 같은
`expr` 문법이다. 수식 열끼리 서로 가리켜도 된다 (먼저 계산할 것을 알아서
먼저 한다). 서로 돌고 도는 것은 저장할 때 막는다.
"""

from __future__ import annotations

from functools import lru_cache

from . import expr as E

#: 계산이 안 될 때 칸에 보일 글. 빈칸으로 두면 «값이 없는 사람» 과 헷갈린다.
오류값 = "#오류"


@lru_cache(maxsize=256)
def _나무(수식: str):
    return E.parse(수식)


def 계산(수식: str, 행: dict) -> str:
    return E._글(E.한값(E._계산(_나무(수식), 행)))


def 차례(수식들: dict[str, str]) -> tuple[list[str], set[str]]:
    """(계산할 차례, 돌고 도는 열). 다른 수식 열을 쓰는 열은 그 뒤에 온다."""
    쓰는것 = {}
    for 열, 수식 in 수식들.items():
        try:
            쓰는것[열] = [c for c in E.columns(수식) if c in 수식들]
        except E.ExprError:
            쓰는것[열] = []
    나온: list[str] = []
    상태: dict[str, int] = {}          # 1 = 보는 중, 2 = 끝
    돈다: set[str] = set()

    def 보기(열: str, 길: list[str]) -> None:
        if 상태.get(열) == 2:
            return
        if 상태.get(열) == 1:
            돈다.update(길[길.index(열):])
            return
        상태[열] = 1
        for 다음 in 쓰는것[열]:
            보기(다음, 길 + [다음])
        상태[열] = 2
        나온.append(열)

    for 열 in 수식들:
        보기(열, [열])
    return 나온, 돈다


def 검사(열: str, 수식: str, 아는열, 수식들: dict[str, str]) -> None:
    """저장하기 전에 본다. 틀리면 ValueError (사람이 읽을 말)."""
    if not (수식 or "").strip():
        return
    try:
        쓴것 = E.columns(수식)
    except E.ExprError as exc:
        raise ValueError(f"수식이 틀렸습니다: {exc}") from None
    if 열 in 쓴것:
        raise ValueError(f"«{열}» 수식이 자기 자신을 씁니다 — 다른 열만 쓸 수 있습니다.")
    try:
        E.validate(수식, set(아는열) | set(수식들))
    except E.ExprError as exc:
        raise ValueError(str(exc)) from None
    _, 돈다 = 차례({**수식들, 열: 수식})
    if 열 in 돈다:
        raise ValueError(
            "수식 열끼리 서로를 가리킵니다: " + " → ".join(sorted(돈다))
            + " — 한쪽은 수식을 빼세요.")


def 채우기(행: dict, 수식들: dict[str, str], 적은값: dict,
         빈칸: dict | None = None) -> set[str]:
    """**사람이 안 적은** 수식 열을 계산해 `행` 에 넣는다. 수식으로 채운 열을 돌려준다.

    `적은값` 은 그 사람의 추가 열에 사람이 적은 값 ({열: 값}).
    `빈칸` 을 주면 줄에 없는 열을 빈칸으로 본다 (모르는 열로 터지지 않게).
    """
    if not 수식들:
        return set()
    문맥 = {**(빈칸 or {}), **행}
    순서, 돈다 = 차례(수식들)
    채운: set[str] = set()
    for 열 in 순서:
        if str(적은값.get(열, "") or "").strip():
            continue
        if 열 in 돈다:
            값 = 오류값
        else:
            try:
                값 = 계산(수식들[열], 문맥)
            except (E.ExprError, RecursionError):
                값 = 오류값
        행[열] = 문맥[열] = 값
        채운.add(열)
    return 채운
