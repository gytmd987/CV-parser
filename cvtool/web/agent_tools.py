"""에이전트(사내 클코 등)가 쓰는 도구들 — MCP 와 JSON API 가 같이 쓴다.

사내 LLM API 를 시스템이 직접 부를 수 없을 때, LLM 이 하던 판단을 에이전트가 대신한다.

    시스템  : 할 일(대기열) · 지시문 · 결과 모양(스키마)을 내준다
    에이전트: 원문을 읽고 판단해 JSON 으로 돌려준다
    시스템  : 돌려받은 것을 **LLM 답과 똑같은 길**로 검사 · 정리 · 저장 · 이력에 남긴다

그래서 누가 판단하든 정규화 · 검토 표시 · 중복 검토 · 매칭 정렬이 같다. 도구 이름은
MCP 클라이언트가 받아 주는 영문으로, 설명은 한국어로 둔다.
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import extract as extractmod, matching as matchingmod
from ..auth import can, User
from ..edit import ConflictError, ValidationError, edit_field
from ..fsutil import safe_filename
from ..ingestion.parsers import extract_text
from ..matching import candidate_profile, 점수등급
from ..store import SUPPORTED_SUFFIXES
from .jobs import (
    _embed_client, _enqueue, _set_status, 과제목록, 등록마무리, 에이전트모드, 채우기메모,
)
from .state import audit, registry, store


class 도구오류(ValueError):
    """에이전트에게 그대로 돌려줄 말 (잘못 부른 것 · 없는 것)."""


@dataclass(frozen=True)
class 도구:
    이름: str
    설명: str
    입력: dict
    권한: str
    처리: Callable[[User, dict], dict]


def _입력(필수: tuple[str, ...] = (), **속성) -> dict:
    return {"type": "object", "properties": 속성, "required": list(필수),
            "additionalProperties": False}


def _글(설명: str) -> dict:
    return {"type": "string", "description": 설명}


def _cid(args: dict) -> str:
    cid = str(args.get("지원자_ID") or "").strip()
    if not cid:
        raise 도구오류("지원자_ID 를 주세요.")
    return cid


def _기록(me: User, cid: str, 비고: str, 항목: str = "", 새값: str = "") -> None:
    audit.record(me.아이디, "지원자", cid, 항목=항목, 새값=새값, 비고=비고)


# --- 설명서 · 상태 ------------------------------------------------------------------
설명서 = """# 지원자 관리 시스템 — 에이전트(사내 클코) 사용 설명서

이 시스템은 연구직 지원자 CV 를 분석해 등록하고, 회사 연구 과제와 맞춰 본다.
사내 LLM 을 직접 부를 수 없어서 **AI 판단은 에이전트(당신)가 한다.** 시스템은 할 일 ·
지시문 · 결과 모양을 주고, 돌려받은 결과를 검사해 저장한다. 사람이 시킬 때 아래 순서로 일한다.

## 시작
1. `status` 로 지금 상태(분석 모드 · CV 분석 대기 수 · 매칭 대기 수)를 본다.

## CV 분석 (사람이 «CV 분석 대기 처리해줘» 라고 하면)
1. `cv_pending` 으로 대기 목록을 본다.
2. 한 명씩 `cv_task(지원자_ID)` 로 일감을 받는다 — system · instructions · cv_text · schema.
3. system 과 instructions 를 그대로 따르고 cv_text 만 근거로 schema 모양의 JSON 을 만든다.
   **CV 에 없는 내용을 지어내지 않는다.** 모르는 값은 빈 문자열.
4. `cv_submit(지원자_ID, result)` 로 보낸다. 오류가 오면 메시지대로 고쳐 다시 보낸다.
5. 다음 사람으로. 끝나면 몇 명 등록했는지, 검토가 필요한 사람이 누구인지 사람에게 알린다.

## 과제 매칭 (사람이 «매칭 대기 처리해줘» 라고 하면)
1. `match_pending` 으로 아직 안 맞춰본 지원자를 본다 (과제를 바꿨으면 전부 다시 할 수도 있다).
2. `match_task(지원자_ID)` 로 일감을 받는다 — system · user · schema (과제 전부).
3. system 의 등급 기준을 그대로 따르고, 과제마다 등급 · 사유 · 근거를 schema 모양으로 만든다.
   근거는 지원자 정보에 적힌 표현을 인용한다. 준 과제키만 쓰고 하나도 빠뜨리지 않는다.
4. `match_submit(지원자_ID, result)` 로 보낸다.

## 그 밖에
- `candidates_search` · `candidate_get` · `projects_list` 로 저장된 정보를 본다.
- `candidate_update` 로 지원자 항목 하나를 고친다 (사람이 시킨 것만). 이전값이 지금 값과
  다르면 거절된다 — `candidate_get` 으로 다시 읽고 고친다.
- `cv_upload` 로 사람이 준 CV 파일을 분석 대기열에 올린다.
- 모든 변경은 «agent:토큰이름» 으로 변경 이력에 남는다.
- 개인정보(이름 · 연락처 · CV 원문)를 다룬다. 시킨 일 밖으로 옮기거나 요약해 퍼뜨리지 않는다.
"""


def _status(me: User, args: dict) -> dict:
    목록, 오류 = 과제목록()
    매칭된 = set(store.top_matches())
    return {
        "AI 분석 모드": "사내 클코(에이전트)" if 에이전트모드() else "서버 LLM",
        "CV 분석 대기": len(store.pending_jobs()),
        "매칭 대기": sum(1 for r in store.list_all() if r.지원자_ID not in 매칭된) if 목록 else 0,
        "지원자 수": store.count(),
        "과제 수": len(목록),
        **({"과제 파일 오류": 오류} if 오류 else {}),
        **({} if 에이전트모드() else {
            "주의": "지금은 서버 LLM 모드라 CV 분석은 시스템이 합니다. 대기 목록은 비어 있는 게 보통입니다."}),
    }


# --- CV 분석 -------------------------------------------------------------------------
def _대기찾기(cid: str) -> dict:
    for j in store.pending_jobs():
        if j["지원자_ID"] == cid:
            return j
    raise 도구오류(f"분석 대기 중인 지원자가 아닙니다: {cid} (cv_pending 으로 목록을 보세요)")


def _원문(j: dict) -> str:
    path = store.files_dir / j["저장_파일명"]
    if not path.is_file():
        raise 도구오류(f"원본 파일이 없습니다: {j['파일명']}")
    text = extract_text(path)
    if not text.strip():
        raise 도구오류(f"텍스트를 추출하지 못했습니다(스캔 PDF?): {j['파일명']} — 사람에게 알려 주세요.")
    return text


def _cv_pending(me: User, args: dict) -> dict:
    return {"대기": [
        {"지원자_ID": j["지원자_ID"], "파일명": j["파일명"], "올린때": j.get("등록일시", ""),
         "종류": "빈 항목 채우기" if j.get("메모") == 채우기메모 else "새 분석"}
        for j in store.pending_jobs()]}


def _cv_task(me: User, args: dict) -> dict:
    대기 = store.pending_jobs()
    if not 대기:
        return {"대기": 0, "알림": "분석 대기 중인 CV 가 없습니다."}
    cid = str(args.get("지원자_ID") or "").strip() or 대기[0]["지원자_ID"]
    j = _대기찾기(cid)
    일감 = extractmod.에이전트_요청(_원문(j))
    _set_status(j["파일명"], "처리중", f"{me.아이디} 가 분석 중", cid=cid)
    return {"지원자_ID": cid, "파일명": j["파일명"],
            "종류": "빈 항목 채우기" if j.get("메모") == 채우기메모 else "새 분석", **일감}


def _cv_submit(me: User, args: dict) -> dict:
    cid = _cid(args)
    결과 = args.get("result")
    if not isinstance(결과, dict):
        raise 도구오류("result 는 JSON 객체여야 합니다 (cv_task 의 schema 모양).")
    j = _대기찾기(cid)
    text = _원문(j)
    try:
        rec = extractmod.에이전트_결과로(text, 결과, 지원자_ID=cid, 원본_파일명=j["파일명"])
    except ValueError as exc:
        raise 도구오류(str(exc)) from exc
    상태 = 등록마무리(rec, text, j["파일명"], cid, j["저장_파일명"], j.get("메모") or "")
    store.finish_job(cid)
    _기록(me, cid, "사내 클코가 CV 분석" if j.get("메모") != 채우기메모 else "사내 클코가 빈 항목 채우기")
    저장 = store.get(cid)
    return {"지원자_ID": cid, "상태": 상태,
            "이름": (저장.한글_이름 or 저장.영문_이름) if 저장 else "",
            "검토_사유": (저장.검토_사유 if 저장 else ""),
            "남은 대기": len(store.pending_jobs())}


def _cv_upload(me: User, args: dict) -> dict:
    파일명 = safe_filename(str(args.get("파일명") or ""))
    try:
        내용 = base64.b64decode(str(args.get("내용_base64") or ""), validate=True)
    except ValueError as exc:
        raise 도구오류("내용_base64 가 base64 가 아닙니다.") from exc
    if not 내용:
        raise 도구오류("파일 내용이 비었습니다.")
    끝 = Path(파일명).suffix.lower()
    if 끝 not in SUPPORTED_SUFFIXES:
        raise 도구오류(f"지원하지 않는 형식: {끝 or '(확장자 없음)'} — {', '.join(sorted(SUPPORTED_SUFFIXES))}")
    cid = f"CV-{uuid.uuid4().hex[:8].upper()}"
    저장명 = store.store_file(cid, 파일명, 내용)
    _enqueue(파일명, cid, 저장명)
    _기록(me, cid, f"사내 클코가 CV 올림: {파일명}")
    return {"지원자_ID": cid, "파일명": 파일명,
            "다음": "cv_task 로 분석하세요" if 에이전트모드() else "서버 LLM 이 분석합니다"}


# --- 과제 매칭 -----------------------------------------------------------------------
def _과제들() -> list:
    목록, 오류 = 과제목록()
    if 오류:
        raise 도구오류(f"과제 파일 오류: {오류}")
    if not 목록:
        raise 도구오류("과제가 없습니다. 관리자가 과제 정보를 먼저 올려야 합니다.")
    return 목록


def _지원자(cid: str):
    rec = store.get(cid)
    if rec is None:
        raise 도구오류(f"없는 지원자입니다: {cid}")
    return rec


def _match_pending(me: User, args: dict) -> dict:
    _과제들()
    매칭된 = set(store.top_matches())
    return {"대기": [
        {"지원자_ID": r.지원자_ID, "이름": r.한글_이름 or r.영문_이름, "현재_소속": r.to_row().get("현재_소속", "")}
        for r in store.list_all() if r.지원자_ID not in 매칭된]}


def _match_task(me: User, args: dict) -> dict:
    rec = _지원자(_cid(args))
    return {"지원자_ID": rec.지원자_ID,
            **matchingmod.에이전트_요청(candidate_profile(rec, registry), _과제들())}


def _match_submit(me: User, args: dict) -> dict:
    rec = _지원자(_cid(args))
    결과 = args.get("result")
    try:
        나온것 = matchingmod.에이전트_결과로(candidate_profile(rec, registry), _과제들(),
                                    결과 if isinstance(결과, dict) else {}, _embed_client())
    except ValueError as exc:
        raise 도구오류(str(exc)) from exc
    store.save_matches(rec.지원자_ID, 나온것)
    if 나온것:
        _기록(me, rec.지원자_ID, "사내 클코가 과제 매칭", 항목="과제 매칭",
              새값=f"{나온것[0].과제명} ({나온것[0].등급})")
    빠진 = [m.과제명 for m in 나온것 if not m.평가됨]
    return {"지원자_ID": rec.지원자_ID,
            "상위": [{"과제": m.과제명, "등급": m.등급} for m in 나온것[:3]],
            **({"채점 안 된 과제": 빠진,
                "알림": "빠진 과제가 있습니다. 전부 넣어 다시 보내세요."} if 빠진 else {})}


# --- 조회 · 수정 ----------------------------------------------------------------------
_요약열 = ("한글_이름", "영문_이름", "현재_신분", "현재_소속", "박사_학교", "박사_전공",
         "연구분야_키워드", "검토_필요")


def _candidates_search(me: User, args: dict) -> dict:
    말 = str(args.get("검색어") or "").strip().lower()
    try:
        개수 = max(1, min(500, int(args.get("개수") or 50)))
    except (TypeError, ValueError):
        개수 = 50
    상위 = store.top_matches()
    나온것 = []
    for r in store.list_all():
        row = r.to_row(registry)
        if 말 and 말 not in " ".join(str(v) for v in row.values()).lower():
            continue
        줄 = {"지원자_ID": r.지원자_ID, **{k: row.get(k, "") for k in _요약열}}
        if r.지원자_ID in 상위:
            m = 상위[r.지원자_ID]
            줄["매칭_1순위"] = f"{m['과제명']} ({점수등급(m['점수'], m['평가됨'])})"
        나온것.append(줄)
        if len(나온것) >= 개수:
            break
    return {"개수": len(나온것), "지원자": 나온것}


def _candidate_get(me: User, args: dict) -> dict:
    rec = _지원자(_cid(args))
    return {"지원자": {"지원자_ID": rec.지원자_ID,
                     **{k: v for k, v in rec.to_row(registry).items() if v not in ("", None)}},
            "논문": [{"제목": p.제목, "제출처": p.제출처, "주저자": p.주저자}
                   for p in (getattr(rec, "논문", []) or [])][:100],
            "과제매칭": [{"과제": m["과제명"], "등급": 점수등급(m["점수"], m["평가됨"]), "사유": m["사유"]}
                     for m in store.matches(rec.지원자_ID)]}


def _candidate_update(me: User, args: dict) -> dict:
    rec = _지원자(_cid(args))
    항목 = str(args.get("항목") or "").strip()
    if not 항목:
        raise 도구오류("항목 이름을 주세요 (candidate_get 의 키).")
    try:
        전, 후 = edit_field(rec, 항목, str(args.get("새값") or ""),
                          기대_이전값=str(args.get("이전값") or ""), registry=registry,
                          긴글=항목 in store.긴글열())
    except (ConflictError, ValidationError) as exc:
        raise 도구오류(str(exc)) from exc
    if 전 != 후:
        store.save(rec)
        audit.record(me.아이디, "지원자", rec.지원자_ID, 항목=항목, 이전값=전, 새값=후,
                     비고="사내 클코가 수정")
    return {"지원자_ID": rec.지원자_ID, "항목": 항목, "이전값": 전, "새값": 후, "바뀜": 전 != 후}


def _projects_list(me: User, args: dict) -> dict:
    return {"과제": [{"과제키": p.키, "과제명": p.이름, "번호": p.번호, "키워드": p.키워드,
                    "담당": p.담당, "설명": p.설명} for p in _과제들()]}


# --- 도구 표 -------------------------------------------------------------------------
도구들: dict[str, 도구] = {t.이름: t for t in (
    도구("guide", "이 시스템을 에이전트로 쓰는 방법 (처음에 한 번 읽는다).", _입력(), "지원자_목록",
       lambda me, a: {"설명서": 설명서}),
    도구("status", "지금 상태: AI 분석 모드 · CV 분석 대기 수 · 매칭 대기 수 · 지원자/과제 수.",
       _입력(), "지원자_목록", _status),
    도구("cv_pending", "분석을 기다리는 CV 목록.", _입력(), "지원자_등록", _cv_pending),
    도구("cv_task", "CV 분석 일감 하나 (지시문 · CV 원문 · 결과 스키마). 지원자_ID 를 안 주면 맨 앞 것.",
       _입력(지원자_ID=_글("cv_pending 의 지원자_ID (생략하면 맨 앞)")), "지원자_등록", _cv_task),
    도구("cv_submit", "CV 분석 결과를 보낸다. 시스템이 검사 · 정리해 지원자로 등록한다.",
       _입력(("지원자_ID", "result"), 지원자_ID=_글("cv_task 의 지원자_ID"),
           result={"type": "object", "description": "cv_task 의 schema 모양 JSON (basic · education · research · career)"}),
       "지원자_등록", _cv_submit),
    도구("cv_upload", "CV 파일을 분석 대기열에 올린다 (pdf · docx · txt 등).",
       _입력(("파일명", "내용_base64"), 파일명=_글("확장자 포함 파일 이름"),
           내용_base64=_글("파일 내용을 base64 로")), "지원자_등록", _cv_upload),
    도구("match_pending", "과제 매칭을 아직 안 한 지원자 목록.", _입력(), "과제매칭_조회", _match_pending),
    도구("match_task", "지원자 한 명의 과제 매칭 일감 (지시문 · 지원자 요약 · 과제 전부 · 결과 스키마).",
       _입력(("지원자_ID",), 지원자_ID=_글("지원자_ID")), "과제매칭_조회", _match_task),
    도구("match_submit", "과제 매칭 결과를 보낸다. 시스템이 검사 · 정렬해 저장한다.",
       _입력(("지원자_ID", "result"), 지원자_ID=_글("match_task 의 지원자_ID"),
           result={"type": "object", "description": 'match_task 의 schema 모양 {"결과": [...]}'}),
       "지원자_등록", _match_submit),
    도구("candidates_search", "지원자 찾기 (이름 · 소속 · 학교 · 키워드 등 아무 글자). 요약 열만.",
       _입력(검색어=_글("찾을 글자 (비우면 전체)"), 개수={"type": "integer", "description": "최대 몇 명 (기본 50)"}),
       "지원자_목록", _candidates_search),
    도구("candidate_get", "지원자 한 명의 전체 정보 · 논문 · 과제 매칭.",
       _입력(("지원자_ID",), 지원자_ID=_글("지원자_ID")), "지원자_목록", _candidate_get),
    도구("candidate_update", "지원자 항목 하나를 고친다 (사람이 시킨 것만). 이전값이 지금 값과 같아야 한다.",
       _입력(("지원자_ID", "항목", "새값", "이전값"), 지원자_ID=_글("지원자_ID"),
           항목=_글("candidate_get 의 키 (예: 현재_소속)"), 새값=_글("새 값"),
           이전값=_글("지금 값 (candidate_get 에서 읽은 그대로, 비어 있으면 빈 문자열)")),
       "지원자_수정", _candidate_update),
    도구("projects_list", "회사 연구 과제 목록 (매칭에 쓰는 것).", _입력(), "과제매칭_조회", _projects_list),
)}


def 부르기(me: User, 이름: str, args: dict | None) -> dict:
    """도구 하나를 부른다. 잘못 부른 것은 도구오류."""
    t = 도구들.get(이름)
    if t is None:
        raise 도구오류(f"없는 도구입니다: {이름}")
    if not can(me, t.권한):
        raise 도구오류(f"이 토큰으로는 {이름} 을(를) 쓸 수 없습니다.")
    if not isinstance(args or {}, dict):
        raise 도구오류("arguments 는 JSON 객체여야 합니다.")
    return t.처리(me, args or {})

