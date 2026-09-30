"""뒤에서 도는 CV 분석 대기열과 과제 매칭 실행.
"""

from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path

from .. import projects as projectsmod, review
from ..config import settings
from ..dedup import find_duplicates, fingerprint
from ..extract import extract_cv_from_text
from ..ingestion.parsers import extract_text
from ..matching import candidate_profile, match as match_projects
from ..names import observe_record
from ..timeutil import now_kst

from .state import audit, DATA_DIR, registry, store


_projects_cache: dict = {"mtime": None, "path": None, "목록": [], "오류": ""}


def 다듬은파일() -> Path:
    """필요한 과제·필드만 남겨 저장하는 파일의 위치."""
    if settings.projects_curated:
        return Path(projectsmod.resolve_path(settings.projects_curated))
    return DATA_DIR / "과제_선별.json"


def 쓰는과제파일() -> tuple[str, bool]:
    """(실제로 매칭에 쓰는 경로, 다듬은 파일인지).

    다듬은 파일이 있으면 그것을 쓴다. 원본에는 매칭에 쓸모없는 항목이 많다.
    """
    다듬 = 다듬은파일()
    if 다듬.is_file():
        return str(다듬), True
    return settings.projects_json, False


def 과제목록(다시: bool = False) -> tuple[list, str]:
    """(과제 목록, 오류 메시지). 파일이 바뀌면 자동으로 다시 읽는다."""
    경로, _다듬음 = 쓰는과제파일()
    if not 경로:
        return [], ""
    풀린것 = projectsmod.resolve_path(경로)
    mtime = 풀린것.stat().st_mtime if 풀린것 and 풀린것.is_file() else None
    if (다시 or _projects_cache["path"] != str(풀린것)
            or _projects_cache["mtime"] != mtime):
        try:
            _projects_cache["목록"] = projectsmod.load(경로)
            _projects_cache["오류"] = ""
        except projectsmod.ProjectsError as exc:
            _projects_cache["목록"] = []
            _projects_cache["오류"] = str(exc)
        _projects_cache["path"] = str(풀린것)
        _projects_cache["mtime"] = mtime
    return _projects_cache["목록"], _projects_cache["오류"]


def 매칭실행(rec, *, 사용자: str = "") -> tuple[int, str]:
    """지원자 한 명을 과제와 맞춰 보고 저장한다. (매칭 수, 오류 메시지)"""
    목록, 오류 = 과제목록()
    if 오류:
        return 0, 오류
    if not 목록:
        return 0, ""
    profile = candidate_profile(rec, registry)
    try:
        결과 = match_projects(profile, 목록, batch=settings.match_batch,
                            embed_client=_embed_client())
    except Exception as exc:  # noqa: BLE001 - 매칭이 실패해도 지원자는 남아야 한다
        return 0, f"{type(exc).__name__}: {exc}"
    store.save_matches(rec.지원자_ID, 결과)
    if 사용자 and 결과:
        audit.record(사용자, "지원자", rec.지원자_ID, 항목="과제 매칭",
                     새값=f"{결과[0].과제명} {결과[0].점수}점")
    return len(결과), ""


def _embed_client():
    """임베딩은 있으면 쓰고 없으면 만다 (후보 좁히기에만 쓴다)."""
    try:
        from ..clients.embedding import EmbeddingClient

        return EmbeddingClient()
    except Exception:  # noqa: BLE001
        return None

_jobs: "queue.Queue[tuple[str, str, str | None]]" = queue.Queue()
_status_lock = threading.Lock()
_status: dict[str, dict] = {}  # filename -> {state, message}


# ---------------------------------------------------------------------------
# 백그라운드 추출 워커
# ---------------------------------------------------------------------------
def _set_status(name: str, state: str, message: str = "", cid: str = "") -> None:
    """처리 현황 한 줄. cid 를 실어 둬야 거기서 바로 상세로 갈 수 있다."""
    with _status_lock:
        옛 = _status.get(name) or {}
        _status[name] = {
            "state": state, "message": message,
            "cid": cid or 옛.get("cid", ""),
            "시각": now_kst().strftime("%H:%M:%S"),
        }


def _enqueue(filename: str, 지원자_ID: str, 저장_파일명: str, 메모: str = "") -> None:
    """분석 대기열에 넣는다. **DB 에 먼저 적는다.**

    메모리 대기열에만 넣으면 서버를 다시 켤 때 대기 중이던 원본이 «DB 에 행이
    없는 파일» 로 보여 `_startup_cleanup` 이 말없이 지운다. 30개를 올려 두고
    재시작하면 나머지가 흔적 없이 사라졌다.
    """
    store.add_job(지원자_ID, filename, 저장_파일명, 메모)
    _set_status(filename, "대기중", 메모, cid=지원자_ID)
    _jobs.put((filename, 지원자_ID, 저장_파일명))


def _resume_jobs() -> int:
    """지난번에 끝내지 못한 분석을 다시 대기열에 넣는다. 넣은 개수."""
    남은것 = store.pending_jobs()
    for j in 남은것:
        _set_status(j["파일명"], "대기중", "서버를 다시 켜서 이어서 분석합니다",
                    cid=j["지원자_ID"])
        _jobs.put((j["파일명"], j["지원자_ID"], j["저장_파일명"]))
    return len(남은것)


def _worker() -> None:
    while True:
        filename, 지원자_ID, 저장_파일명 = _jobs.get()
        try:
            _set_status(filename, "처리중", cid=지원자_ID)
            path = store.files_dir / 저장_파일명
            # 보관된 원본에서 매번 새로 뽑는다. 그래야 PDF 파서를 개선하면
            # 재분석만으로 반영된다 (텍스트를 캐시하면 옛 추출에 갇힌다).
            text = extract_text(path)
            if not text.strip():
                _set_status(filename, "실패", "텍스트를 추출하지 못했습니다(스캔 PDF?)")
                continue
            rec = extract_cv_from_text(text, 원본_파일명=filename, 지원자_ID=지원자_ID)

            # 이름들을 사전에 등록하고 검토 사유를 받는다. 레코드 값은 건드리지 않는다.
            사전사유 = observe_record(rec, registry)
            if 사전사유:
                rec.검토_사유 = review.join([rec.검토_사유, *사전사유])
                rec.검토_필요 = "Y"

            # 중복 검토
            fp = fingerprint(text)
            후보 = find_duplicates(rec, fp, store.fingerprints())
            메모 = " / ".join(str(m) for m in 후보)
            if 후보:
                확실 = [m for m in 후보 if m.수준 == "확실"]
                말머리 = "중복 확실" if 확실 else "중복 의심"
                rec.검토_사유 = f"{rec.검토_사유} / {말머리}: {메모}" if rec.검토_사유 else f"{말머리}: {메모}"
                rec.검토_필요 = "Y"

            store.save(rec, 원문_텍스트=text, 저장_파일명=저장_파일명, 지문=fp, 중복_메모=메모)

            # 과제 매칭. 실패해도 지원자 등록은 이미 끝났으니 메모만 남긴다.
            매칭메모 = ""
            if settings.match_auto and settings.projects_json:
                개수, 매칭오류 = 매칭실행(rec)
                if 매칭오류:
                    매칭메모 = f" / 과제 매칭 실패: {매칭오류}"
                elif 개수:
                    최고 = store.matches(rec.지원자_ID)[0]
                    매칭메모 = f" / 과제: {최고['과제명']} {최고['점수']}점"

            state = "중복의심" if 후보 else ("검토필요" if rec.검토_필요 == "Y" else "완료")
            _set_status(filename, state, (rec.검토_사유 or "") + 매칭메모,
                        cid=지원자_ID)
        except Exception as exc:  # noqa: BLE001 - 워커가 죽으면 안 된다
            _set_status(filename, "실패", f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
        finally:
            # 실패도 끝난 것이다. 남겨 두면 재시작할 때마다 같은 실패를 되풀이한다.
            try:
                store.finish_job(지원자_ID)
            except Exception:  # noqa: BLE001 - 워커가 죽으면 안 된다
                traceback.print_exc()
            _jobs.task_done()


threading.Thread(target=_worker, daemon=True).start()
