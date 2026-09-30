"""화면에 심는 JavaScript — `cvtool/web/static/*.js`.

파이썬 문자열 안에 있던 동안에는 문법 검사를 할 수가 없어서, 괄호 하나·`\\t`
하나에 편집기가 **통째로 안 뜨는** 일이 있었고 화면을 열어 봐야 알았다.
이제 파일이라 `node --check` 로 본다. node 가 없는 서버에서는 건너뛴다.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "cvtool" / "web"
JS들 = sorted((WEB / "static").glob("*.js"))
NODE = shutil.which("node") or shutil.which("nodejs")


def test_JS_파일이_있다():
    assert len(JS들) >= 11


@pytest.mark.skipif(NODE is None, reason="node 가 없다")
@pytest.mark.parametrize("파일", JS들, ids=lambda p: p.name)
def test_JS_문법(파일):
    결과 = subprocess.run([NODE, "--check", str(파일)], capture_output=True, text=True)
    assert 결과.returncode == 0, 결과.stderr


@pytest.mark.parametrize("파일", JS들, ids=lambda p: p.name)
def test_화면에_심어도_script_가_안_끊긴다(파일):
    """`</script>` 가 들어 있으면 페이지에 심을 때 거기서 스크립트가 끝나 버린다."""
    assert "</script" not in 파일.read_text(encoding="utf-8").lower()


def test_안_쓰는_JS_파일이_없다():
    """파일은 있는데 아무도 안 불러오면, 고쳐도 화면에 안 나타나 헤맨다."""
    파이썬 = "".join(p.read_text(encoding="utf-8") for p in WEB.rglob("*.py"))
    불러옴 = set(re.findall(r'_정적JS\("([^"]+)"\)', 파이썬))
    assert {p.name for p in JS들} == 불러옴
