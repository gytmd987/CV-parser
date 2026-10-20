"""검증 자료 정리 실행 — 어느 폴더에서든 이 파일 경로로 실행하면 된다.

    python3 /경로/CV-parser/verifysum_run.py

`python3 -m verifysum` 은 CV-parser 폴더 **안에서만** 된다 (파이썬이 지금 폴더에서
모듈을 찾기 때문). 이 파일은 자기 폴더를 먼저 찾을 곳에 넣으므로 어디서든 된다.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

빠진것 = []
for 모듈, 패키지 in (("docx", "python-docx"), ("httpx", "httpx"), ("pydantic", "pydantic")):
    try:
        __import__(모듈)
    except ImportError:
        빠진것.append(패키지)
if 빠진것:
    sys.exit("필요한 패키지가 없습니다: " + ", ".join(빠진것)
             + f"\n  {sys.executable} -m pip install -r requirements.txt  로 설치하세요.")

from verifysum.web import main  # noqa: E402

main()
