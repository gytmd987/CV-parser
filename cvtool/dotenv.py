""".env 파일 로더 (표준 라이브러리만).

python-dotenv 가 폐쇄망에 없을 수 있어 직접 읽는다.

규칙:
  - `KEY=VALUE` 형식. `export KEY=VALUE` 도 허용
  - `#` 로 시작하는 줄과 빈 줄은 무시
  - 값의 앞뒤 따옴표는 벗겨낸다
  - **이미 설정된 환경변수는 덮어쓰지 않는다** (실제 환경변수가 항상 우선)
"""

from __future__ import annotations

import os
from pathlib import Path

# 패키지 위치 기준 저장소 루트 (cvtool/dotenv.py -> cvtool -> 루트)
_REPO_ROOT = Path(__file__).resolve().parent.parent


def candidate_paths() -> list[Path]:
    """.env 를 찾아볼 위치. 앞쪽이 우선."""
    paths = []
    env_path = os.environ.get("CVTOOL_ENV_FILE")
    if env_path:
        paths.append(Path(env_path))
    paths.append(Path.cwd() / ".env")
    paths.append(_REPO_ROOT / ".env")
    # 중복 제거(순서 유지)
    seen, out = set(), []
    for p in paths:
        rp = p.expanduser()
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_dotenv() -> Path | None:
    """.env 를 찾아 os.environ 에 반영하고, 사용한 경로를 반환한다."""
    for path in candidate_paths():
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for key, value in parse(text).items():
            os.environ.setdefault(key, value)  # 기존 환경변수가 우선
        return path
    return None


#: 임포트 시점에 한 번 읽는다. config 의 기본값 계산보다 먼저 실행돼야 한다.
LOADED_FROM: Path | None = load_dotenv()
