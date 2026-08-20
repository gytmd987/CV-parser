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
_번호_후보 = ("과제번호", "과제_번호", "번호", "코드", "id", "code", "project_id",
           "projectId", "project_code", "key")
_설명_후보 = ("설명", "개요", "내용", "요약", "목표", "description", "summary",
           "detail", "details", "overview", "abstract", "goal", "objective")
_키워드_후보 = ("키워드", "분야", "연구분야", "기술", "keywords", "keywords_kr",
             "keywords_en", "tags", "field", "fields", "areas", "tech",
             "technologies", "skills")
_담당_후보 = ("담당", "담당자", "책임자", "부서", "팀", "owner", "manager",
           "department", "team", "pi", "dep_name", "dept_name", "depName")

#: 사내 과제 파일에서 자주 보이는 필드 이름 -> 사람이 읽을 이름.
#: 모르는 필드도 버리지 않지만, 아는 것은 한글 이름으로 붙여야 LLM 이 덜 헷갈린다.
FIELD_LABELS = {
    "dep_name": "부서",
    "project_name": "과제명",
    "core_tech": "핵심 기술",
    "deliverable": "산출물",
    "challenge": "기술적 난제",
    "background": "배경",
    "milestones": "마일스톤",
    "expected_impact": "기대 효과",
    "keywords_kr": "키워드(국문)",
    "keywords_en": "키워드(영문)",
    "budget": "예산",
    "period": "기간",
    "owner": "담당",
}


def label_of(필드: str) -> str:
    return FIELD_LABELS.get(필드, 필드)


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
    """후보 필드를 **전부** 모은다 (keywords_kr 과 keywords_en 이 따로 있다)."""
    모은것: list[str] = []
    for k in 후보:
        v = 원본.get(k)
        if isinstance(v, list):
            모은것 += [str(x).strip() for x in v if str(x).strip()]
        elif isinstance(v, str) and v.strip():
            모은것 += [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]
    return list(dict.fromkeys(모은것))


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
            나머지.append(f"{label_of(k)}: {글}")
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


# ---------------------------------------------------------------------------
# 과제 파일 다듬기 — 필요한 과제·필드만 남겨 따로 저장한다
# ---------------------------------------------------------------------------
# 원본 과제 파일에는 매칭에 쓸모없는 항목이 많다. 그걸 그대로 LLM 에 밀어 넣으면
# 프롬프트만 길어지고 판단이 흐려진다. 그래서 **사람이 한 번 골라** 다듬은 파일을
# 만들고, 매칭은 그 파일을 쓴다. 원본은 건드리지 않는다.

#: 다듬은 파일에 항상 남기는 필드 (이게 없으면 과제를 알아볼 수 없다)
ALWAYS_KEEP = tuple(_이름_후보)


def read_json(path: str | Path | None):
    """과제 파일을 읽어 JSON 그대로 돌려준다. 문제가 있으면 ProjectsError."""
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
    except UnicodeDecodeError as exc:
        raise ProjectsError(
            f"UTF-8 로 읽히지 않습니다 ({p}). 파일 인코딩을 확인하세요: {exc}"
        ) from exc
    except OSError as exc:
        raise ProjectsError(f"과제 파일을 읽지 못했습니다: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProjectsError(
            f"JSON 형식이 아닙니다 ({p}) — {exc.lineno}번째 줄: {exc.msg}"
        ) from exc


def raw_items(data) -> list[tuple[str, dict]]:
    """어떤 모양이든 (기본키, 원본 사전) 목록으로 편다."""
    if isinstance(data, list):
        return [("", x) for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for k in ("projects", "과제", "과제목록", "items", "data", "list", "result"):
        v = data.get(k)
        if isinstance(v, list):
            return [("", x) for x in v if isinstance(x, dict)]
        if isinstance(v, dict):
            return [(str(kk), vv) for kk, vv in v.items() if isinstance(vv, dict)]
    if data and all(isinstance(v, dict) for v in data.values()):
        return [(str(k), v) for k, v in data.items()]
    return [("", data)]


def item_key(기본키: str, 원본: dict) -> str:
    """화면에서 고를 때와 저장할 때 **같은 키**를 쓰기 위한 계산.

    두 곳에서 따로 계산하면 고른 과제가 저장에서 빠지는 사고가 난다.
    """
    return 기본키 or _첫값(원본, _번호_후보) or _첫값(원본, _이름_후보)


@dataclass
class FieldInfo:
    이름: str
    라벨: str
    채운수: int
    전체수: int
    예시: str

    @property
    def 비율(self) -> int:
        return round(100 * self.채운수 / self.전체수) if self.전체수 else 0

    @property
    def 필수(self) -> bool:
        return self.이름 in ALWAYS_KEEP


def field_stats(항목: list[tuple[str, dict]]) -> list[FieldInfo]:
    """원본에 어떤 필드가 있고 얼마나 채워져 있는지. 처음 나온 순서를 지킨다."""
    순서: list[str] = []
    채운수: dict[str, int] = {}
    예시: dict[str, str] = {}
    for _키, 원본 in 항목:
        for k, v in 원본.items():
            if k not in 채운수:
                순서.append(k)
                채운수[k] = 0
                예시[k] = ""
            글 = _평탄화(v)
            if 글:
                채운수[k] += 1
                if not 예시[k]:
                    예시[k] = 글[:120]
    전체 = len(항목)
    return [FieldInfo(k, label_of(k), 채운수[k], 전체, 예시[k]) for k in 순서]


def curate(항목: list[tuple[str, dict]], 고른키: set[str] | None,
           고른필드: set[str] | None) -> list[dict]:
    """고른 과제 × 고른 필드만 남긴다.

    이름 필드는 고르지 않아도 남긴다 — 없으면 과제를 알아볼 수 없다.
    """
    나온것: list[dict] = []
    for 기본키, 원본 in 항목:
        키 = item_key(기본키, 원본)
        if 고른키 is not None and 키 not in 고른키:
            continue
        남길것 = {}
        for k, v in 원본.items():
            if 고른필드 is None or k in 고른필드 or k in ALWAYS_KEEP:
                if _평탄화(v):
                    남길것[k] = v
        if not 남길것:
            continue
        if 기본키 and not any(k in 남길것 for k in _번호_후보):
            남길것["과제번호"] = 기본키          # 사전 키로만 있던 번호를 살려 둔다
        나온것.append(남길것)
    return 나온것


def save_curated(path: str | Path, 과제들: list[dict], *, 출처: str = "",
                 만든이: str = "") -> Path:
    """다듬은 과제 파일을 쓴다. 읽을 때는 그냥 `load()` 로 읽힌다."""
    from .timeutil import now_kst

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    본문 = {
        "_설명": "지원자 관리 도구가 만든 파일입니다. 원본에서 고른 과제·필드만 담겨 있습니다.",
        "_출처": 출처,
        "_만든일시": now_kst().strftime("%Y-%m-%d %H:%M:%S"),
        "_만든이": 만든이,
        "projects": 과제들,
    }
    p.write_text(json.dumps(본문, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        from .fsutil import secure_file

        secure_file(p)
    except Exception:  # noqa: BLE001 - 권한 설정 실패가 저장을 막으면 안 된다
        pass
    return p


def curated_meta(path: str | Path) -> dict:
    """다듬은 파일의 만든 정보. 없으면 빈 사전."""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "출처": data.get("_출처", ""),
        "만든일시": data.get("_만든일시", ""),
        "만든이": data.get("_만든이", ""),
        "과제수": len(data.get("projects") or []),
        "필드": sorted({k for p_ in (data.get("projects") or [])
                      if isinstance(p_, dict) for k in p_}),
    }
