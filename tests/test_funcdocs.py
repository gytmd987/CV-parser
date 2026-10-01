"""수식 안내 — 모든 함수에 설명이 있어야 한다 (빠지면 안내가 안 뜬다)."""

from __future__ import annotations

from cvtool import expr
from cvtool import formula as F
from cvtool.funcdocs import 함수설명


def test_모든_함수에_설명이_있다():
    빠짐 = [n for n in list(expr.PUBLIC_FUNC_NAMES) + list(F.CALLABLE) if n not in 함수설명]
    assert not 빠짐, f"funcdocs.py 에 설명을 적으세요: {빠짐}"


def test_설명_모양():
    for 이름, (인자, 되풀이, 설명, 예) in 함수설명.items():
        assert isinstance(인자, list) and 설명, 이름
        assert 0 <= 되풀이 <= len(인자), 이름
        assert not 예 or 예.startswith("="), 이름


def test_화면에_실린다(web_client):
    m = web_client.module
    did = m.boards.add("안내판", "admin")
    쪽 = web_client.get(f"/dash/edit?id={did}")
    assert '"설명"' in 쪽 and "COUNTIFS" in 쪽 and "fxsig" in 쪽
    표항목 = web_client.get("/fields")
    assert "window.수식목록" in 표항목 and "class='fx' data-kind='row'" in 표항목
