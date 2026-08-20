"""연구 과제 목록 읽기.

과제 정보 JSON 은 회사마다 모양이 다르다. 그래서 **한 가지 모양을 강요하지 않고**
흔한 형태를 모두 받아들인다.

    [{"과제명": "...", "설명": "..."}, ...]          # 목록
    {"projects": [...]}                             # 감싼 목록
    {"P-001": {...}, "P-002": {...}}                # 키가 과제 번호인 사전

필드 이름도 마찬가지다. `과제명 / 이름 / name / title / project_name` 중 있는 것을
과제 이름으로 본다. 못 알아본 필드는 버리지 않고 **설명에 이어 붙인다** — 매칭
판단에 쓸 정보를 임의로 버리면 안 되기 때문이다.

경로는 `.env` 의 `CVTOOL_PROJECTS_JSON` 으로 준다. 상대경로는 **저장소 폴더 기준**
으로 푼다. 실행 위치(cwd)에 따라 달라지면 서비스로 띄웠을 때 못 찾는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

#: 저장소 뿌리 (cvtool/ 의 부모). 상대경로는 여기를 기준으로 푼다.
REPO_ROOT = Path(__file__).resolve().parent.parent

_이름_후보 = ("과제명", "과제_명", "이름", "제목", "name", "title",
           "project_name", "projectName", "project", "subject")
_번호_후보 = ("과제번호", "과제_번호", "번호", "id", "code", "project_id",
           "projectId", "project_code", "key")
_설명_후보 = ("설명", "개요", "내용", "요약", "목표", "description", "summary",
           "detail", "details", "overview", "abstract", "goal", "objective")
_키워드_후보 = ("키워드", "분야", "연구분야", "기술", "keywords", "tags", "field",
             "fields", "areas", "tech", "technologies", "skills")
_담당_후보 = ("담당", "담당자", "책임자", "부서", "팀", "owner", "manager",
           "department", "team", "pi")


def resolve_path(raw: str) -> Path | None:
    """`.env` 에 적힌 경로를 실제 경로로.

    `~` 를 풀고, 상대경로는 저장소 폴더 기준으로 잡는다. 그래서 CV-parser 폴더에서
    `cd ../과제정보` 로 가는 곳이면 `../과제정보/과제.json` 이라고 적으면 된다.
    """
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (REPO_ROOT / p).resolve()


@dataclass
class Project:
    키: str                  # 화면·저장에 쓰는 식별자
    이름: str
    번호: str = ""
    설명: str = ""
    키워드: list[str] = field(default_factory=list)
    담당: str = ""
    원본: dict = field(default_factory=dict)

    def 요약(self, 최대: int = 1200) -> str:
        """LLM 에 넘길 한 덩어리 글."""
        조각 = [f"과제명: {self.이름}"]
        if self.번호:
            조각.append(f"과제번호: {self.번호}")
        if self.키워드:
            조각.append("키워드: " + ", ".join(self.키워드))
        if self.담당:
            조각.append(f"담당: {self.담당}")
        if self.설명:
            조각.append(f"설명: {self.설명}")
        글 = "\n".join(조각)
        return 글[:최대]


def _첫값(원본: dict, 후보: tuple[str, ...]) -> str:
    for k in 후보:
        v = 원본.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return ""


def _목록값(원본: dict, 후보: tuple[str, ...]) -> list[str]:
    for k in 후보:
        v = 원본.get(k)
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            return [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]
    return []


def _평탄화(값, 깊이: int = 0) -> str:
    """알아보지 못한 값도 글로 펴서 설명에 붙인다."""
    if 깊이 > 3:
        return ""
    if isinstance(값, str):
        return 값.strip()
    if isinstance(값, (int, float, bool)):
        return str(값)
    if isinstance(값, list):
        return ", ".join(x for x in (_평탄화(v, 깊이 + 1) for v in 값) if x)
    if isinstance(값, dict):
        return " / ".join(
            f"{k}: {x}" for k, v in 값.items()
            if (x := _평탄화(v, 깊이 + 1))
        )
    return ""


def to_project(원본: dict, 기본키: str = "") -> Project | None:
    """사전 하나를 Project 로. 이름을 못 찾으면 None."""
    if not isinstance(원본, dict):
        return None
    이름 = _첫값(원본, _이름_후보)
    번호 = _첫값(원본, _번호_후보) or 기본키
    if not 이름:
        이름 = 번호
    if not 이름:
        return None

    설명 = _첫값(원본, _설명_후보)
    키워드 = _목록값(원본, _키워드_후보)
    담당 = _첫값(원본, _담당_후보)

    쓴키 = set(_이름_후보) | set(_번호_후보) | set(_설명_후보) \
        | set(_키워드_후보) | set(_담당_후보)
    나머지 = []
    for k, v in 원본.items():
        if k in 쓴키:
            continue
        글 = _평탄화(v)
        if 글:
            나머지.append(f"{k}: {글}")
    if 나머지:
        설명 = (설명 + "\n" if 설명 else "") + "\n".join(나머지)

    return Project(
        키=번호 or 이름, 이름=이름, 번호=번호, 설명=설명,
        키워드=키워드, 담당=담당, 원본=원본,
    )


def parse(data) -> list[Project]:
    """읽어들인 JSON 을 과제 목록으로."""
    항목: list[tuple[str, dict]] = []
    if isinstance(data, list):
        항목 = [("", x) for x in data]
    elif isinstance(data, dict):
        # {"projects": [...]} 처럼 감싼 경우를 먼저 본다
        for k in ("projects", "과제", "과제목록", "items", "data", "list", "result"):
            v = data.get(k)
            if isinstance(v, list):
                항목 = [("", x) for x in v]
                break
            if isinstance(v, dict):
                항목 = [(str(kk), vv) for kk, vv in v.items()]
                break
        if not 항목:
            # {"P-001": {...}} 처럼 키가 과제 번호인 경우
            if all(isinstance(v, dict) for v in data.values()) and data:
                항목 = [(str(k), v) for k, v in data.items()]
            else:
                항목 = [("", data)]        # 과제 하나만 든 파일

    나온것: list[Project] = []
    본키: set[str] = set()
    for 기본키, 원본 in 항목:
        p = to_project(원본, 기본키)
        if p is None:
            continue
        키 = p.키
        n = 2
        while 키 in 본키:                  # 번호가 겹치면 뒤에 번호를 붙인다
            키 = f"{p.키}#{n}"
            n += 1
        본키.add(키)
        p.키 = 키
        나온것.append(p)
    return 나온것


class ProjectsError(RuntimeError):
    """과제 파일을 못 읽었다. 메시지를 그대로 화면에 보여준다."""


def load(path: str | Path | None) -> list[Project]:
    """과제 JSON 을 읽어 목록으로. 문제가 있으면 무엇이 문제인지 말해준다."""
    p = resolve_path(str(path)) if path else None
    if p is None:
        raise ProjectsError(
            "과제 파일 경로가 비어 있습니다. .env 에 CVTOOL_PROJECTS_JSON 을 넣으세요."
        )
    if not p.exists():
        raise ProjectsError(f"과제 파일이 없습니다: {p}")
    if not p.is_file():
        raise ProjectsError(f"파일이 아닙니다: {p}")
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectsError(f"과제 파일을 읽지 못했습니다: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ProjectsError(
            f"UTF-8 로 읽히지 않습니다 ({p}). 파일 인코딩을 확인하세요: {exc}"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProjectsError(
            f"JSON 형식이 아닙니다 ({p}) — {exc.lineno}번째 줄: {exc.msg}"
        ) from exc

    나온것 = parse(data)
    if not 나온것:
        raise ProjectsError(
            f"과제를 하나도 못 읽었습니다 ({p}). 과제 이름에 해당하는 필드가 "
            f"{'/'.join(_이름_후보[:5])} 중에 있어야 합니다."
        )
    return 나온것
