"""에이전트(사내 코워크 등)가 쓰는 도구들 — MCP 와 JSON API 가 같이 쓴다.

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
from .. import review as reviewmod
from .jobs import (
    _embed_client, _enqueue, _set_status, 검토표시, 과제목록, 등록마무리, 에이전트모드, 채우기메모,
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
설명서 = """# 지원자 관리 시스템 — 에이전트(사내 코워크) 사용 설명서

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

## 우수 학회·저널 리스트 (사람이 «리스트에 ○○ 넣어줘 / 정리해줘» 라고 하면)
- 최우수 · 우수는 **이 리스트에 있는 학회·저널만** 된다. 나머지는 일반 · 제외(미분류)뿐이다.
- `venue_list` 로 리스트를, `venue_names` 로 지원자 CV 에 실제로 나온 학회·저널과 지금 등급을 본다.
- `venue_list_save` 로 넣거나 고친다 (이름이 같으면 고침). 약어 · 풀네임이 다르면 별칭에 같이 적는다.
  사람이 정해 준 등급만 넣는다 — 스스로 판단해 최우수 · 우수를 매기지 않는다 (제안은 사람에게 먼저 보여 준다).
- `venue_list_remove` 로 뺀다. 빼면 그 학회·저널은 미분류가 된다.

## 과제 올리기 (사람이 «이 엑셀로 과제 추가 · 갱신해줘» 라고 하면)
- 엑셀(.xlsx · .csv) 은 `내용_base64` 로, 이미 읽은 표는 `항목`(과제 하나 = 객체 하나) 으로 준다.
  첫 줄 = 항목 이름(과제번호 · 과제명 · 부서 · 키워드 · 핵심기술 …), 한 줄 = 과제 하나.
- 먼저 `projects_import(..., 반영=false)` 로 미리 보고(추가 · 고침 · 그대로 · 열을 무엇으로 읽었는지)
  사람에게 보여 준 뒤, 괜찮다고 하면 같은 내용으로 `반영=true` 로 다시 부른다.
- 과제번호가 같으면 값이 있는 칸만 고치고, 없는 번호는 추가한다. 지우지 않는다.
  과제를 바꾼 뒤 이미 맞춰본 지원자도 다시 맞추려면 `match_task` · `match_submit` 을 다시 돈다.

## 명칭 관리 (사람이 «명칭 정리해줘 / ○○ 를 △△ 로 묶어줘» 라고 하면)
- 소속 · 학회·저널 · 전공 이름이 CV 마다 다르게 적힌다(포항공대 / POSTECH). 표기마다 «표에 보일 이름» 을 정한다.
- `names_list(종류)` 로 이름별 표기 · 분류 · 확인 여부를 본다 (안본것만=true 면 아직 사람이 안 본 것만).
- `names_rename(종류, 이름, 새이름)` — 그 이름을 쓰는 표기 전부의 이름을 바꾼다 (이미 있는 이름이면 합쳐진다).
- `names_move(종류, 표기_id들, 새이름)` — 고른 표기만 다른 이름으로 옮긴다 (잘못 묶인 것 떼기 · 몇 개 합치기).
- `names_classify(종류="학회·저널", 이름, 등급/국내해외/유형/IF)` — 등급은 일반 · 제외 · 미분류만.
  최우수 · 우수는 우수 학회·저널 리스트(`venue_list_save`)에서만 정한다.
- `names_confirm(종류, 이름 또는 표기_id들)` — 사람이 봤다는 표시.
- 같은 곳인지 확실하지 않으면 묶지 말고 사람에게 먼저 묻는다. 바꾼 것은 변경 이력에 남는다.

## 검토 필요 (사람이 «검토 필요 정리해줘 / ○○ 확인함으로 해줘» 라고 하면)
- `review_list` 로 검토가 남은 지원자를, `review_list(지원자_ID)` 로 그 사람의 검토 항목을 본다.
- `review_mark(지원자_ID, 번호들)` 로 항목을 «확인함» 으로 한다 (되돌리려면 확인=false).
  남은 항목이 없으면 그 사람의 «검토 필요» 가 저절로 내려간다.
- **사람이 확인하라고 한 것만, 또는 CV 원문 · 사전과 대조해 실제로 맞는지 본 것만** 확인함으로 한다.
  값이 틀렸으면 확인함으로 넘기지 말고 `candidate_update` 로 고치거나 사람에게 알린다.

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
        "AI 분석 모드": "사내 코워크(에이전트)" if 에이전트모드() else "서버 LLM",
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
    _기록(me, cid, "사내 코워크가 CV 분석" if j.get("메모") != 채우기메모 else "사내 코워크가 빈 항목 채우기")
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
    _기록(me, cid, f"사내 코워크가 CV 올림: {파일명}")
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
        _기록(me, rec.지원자_ID, "사내 코워크가 과제 매칭", 항목="과제 매칭",
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
                     비고="사내 코워크가 수정")
    return {"지원자_ID": rec.지원자_ID, "항목": 항목, "이전값": 전, "새값": 후, "바뀜": 전 != 후}


def _projects_list(me: User, args: dict) -> dict:
    return {"과제": [{"과제키": p.키, "과제명": p.이름, "번호": p.번호, "키워드": p.키워드,
                    "담당": p.담당, "설명": p.설명} for p in _과제들()]}


# --- 과제 올리기 ----------------------------------------------------------------------
def _projects_import(me: User, args: dict) -> dict:
    from .. import projects as projectsmod
    from .pages.match import _가져오기_합치기, 과제_반영, 저장오류말

    파일명 = str(args.get("파일명") or "코워크").strip() or "코워크"
    try:
        if args.get("내용_base64"):
            try:
                내용 = base64.b64decode(str(args["내용_base64"]), validate=True)
            except ValueError as exc:
                raise 도구오류("내용_base64 가 base64 가 아닙니다.") from exc
            새것, 열들 = projectsmod.표를_과제로(projectsmod.표_읽기(파일명, 내용))
        elif isinstance(args.get("항목"), list) and args["항목"]:
            머리 = list(dict.fromkeys(str(k) for x in args["항목"] if isinstance(x, dict) for k in x))
            표 = [머리] + [[str((x or {}).get(k, "") or "") for k in 머리] for x in args["항목"]]
            새것, 열들 = projectsmod.표를_과제로(표)
        else:
            raise 도구오류("내용_base64(엑셀 · csv) 나 항목(과제 목록) 중 하나를 주세요.")
    except projectsmod.ProjectsError as exc:
        raise 도구오류(str(exc)) from exc
    열 = [{"열": h, "쓰임": projectsmod.열역할(h)} for h in 열들]
    if not args.get("반영"):
        r, _다듬r, _버림, 알림 = _가져오기_합치기(새것)
        return {"미리보기": True, "읽은_과제": len(새것), "열": 열,
                "추가": r.추가, "고침": [{"과제": a, "바뀐_항목": b} for a, b in r.갱신],
                "그대로": len(r.같음), "반영뒤_전체": len(r.과제들), "알림": 알림,
                "다음": "사람에게 보여 주고 괜찮다면 같은 내용으로 반영=true 로 부르세요."}
    try:
        r, 알림 = 과제_반영(새것, 파일명, me.아이디)
    except OSError as exc:
        raise 도구오류(저장오류말(exc)) from exc
    return {"반영": True, "추가": len(r.추가), "고침": len(r.갱신), "그대로": len(r.같음),
            "전체": len(r.과제들), "알림": 알림,
            "다음": "이미 맞춰본 지원자에 반영하려면 match_task · match_submit 을 다시 하세요."}


# --- 명칭 관리 ------------------------------------------------------------------------
def _종류(args: dict) -> str:
    from ..names import canonical_kind, KINDS

    종류 = canonical_kind(str(args.get("종류") or "").strip())
    if 종류 not in KINDS:
        raise 도구오류(f"종류는 {' · '.join(KINDS)} 중 하나입니다.")
    return 종류


def _표기들(종류: str, 이름: str) -> list:
    return [n for n in registry.list_all(종류) if n.표시명 == 이름]


def _names_list(me: User, args: dict) -> dict:
    from ..names import GRADED_KINDS

    종류 = _종류(args)
    말 = str(args.get("검색어") or "").strip().lower()
    안본것만 = bool(args.get("안본것만"))
    묶음: dict[str, list] = {}
    for n in registry.list_all(종류):
        묶음.setdefault(n.표시명, []).append(n)
    나온것 = []
    for 이름, 표기들 in 묶음.items():
        if 안본것만 and all(x.확인 for x in 표기들):
            continue
        if 말 and 말 not in (이름 + " " + " ".join(x.원표기 for x in 표기들)).lower():
            continue
        d = {"이름": 이름, "표기": [{"id": x.id, "원표기": x.원표기, "발견횟수": x.발견횟수,
                                  "확인": x.확인} for x in 표기들]}
        if 종류 in GRADED_KINDS:
            대 = 표기들[0]
            d.update(등급=대.등급, 국내해외=대.국내해외, 유형=대.유형, IF=대.IF,
                     우수리스트=bool(registry.우수등급(이름)))
        나온것.append(d)
    try:
        개수 = max(1, min(1000, int(args.get("개수") or 300)))
    except (TypeError, ValueError):
        개수 = 300
    return {"종류": 종류, "이름_수": len(나온것), "이름": 나온것[:개수]}


def _names_rename(me: User, args: dict) -> dict:
    종류 = _종류(args)
    옛 = str(args.get("이름") or "").strip()
    새 = str(args.get("새이름") or "").strip()
    표기들 = _표기들(종류, 옛)
    if not 표기들:
        raise 도구오류(f"그 이름을 쓰는 표기가 없습니다: {옛} (names_list 의 이름 그대로)")
    if not 새:
        raise 도구오류("새이름을 주세요.")
    합침 = bool(_표기들(종류, 새)) and 새 != 옛
    for n in 표기들:
        audit.record(me.아이디, "명칭", f"{종류}:{n.원표기}", 항목="표에 보일 이름", 이전값=옛, 새값=새)
    바뀐수 = registry.rename_group(종류, 옛, 새)
    for n in _표기들(종류, 새):
        if not n.확인:
            registry.confirm(n.id, 사람=me.아이디)
    return {"종류": 종류, "이름": 새, "바뀐_표기": 바뀐수, "합쳐짐": 합침}


def _names_move(me: User, args: dict) -> dict:
    종류 = _종류(args)
    새 = str(args.get("새이름") or "").strip()
    try:
        ids = [int(x) for x in (args.get("표기_id들") or [])]
    except (TypeError, ValueError) as exc:
        raise 도구오류("표기_id들은 names_list 의 id 숫자들입니다.") from exc
    if not ids or not 새:
        raise 도구오류("표기_id들과 새이름을 주세요.")
    이전 = {}
    for i in ids:
        n = registry.get(i)
        if n is None or n.종류 != 종류:
            raise 도구오류(f"{종류} 에 없는 표기 id 입니다: {i}")
        이전[i] = n
    바뀐 = registry.merge(ids, 새)
    for i in 바뀐:
        audit.record(me.아이디, "명칭", f"{종류}:{이전[i].원표기}", 항목="표에 보일 이름",
                     이전값=이전[i].표시명, 새값=새)
    for i in ids:
        registry.confirm(i, 사람=me.아이디)
    return {"종류": 종류, "이름": 새, "옮긴_표기": [이전[i].원표기 for i in 바뀐]}


def _names_classify(me: User, args: dict) -> dict:
    from ..names import GRADED_KINDS, 리스트등급오류

    종류 = _종류(args)
    if 종류 not in GRADED_KINDS:
        raise 도구오류("분류(등급 · 국내해외 · 유형 · IF)는 학회·저널만 있습니다.")
    이름 = str(args.get("이름") or "").strip()
    if not _표기들(종류, 이름):
        raise 도구오류(f"그 이름을 쓰는 표기가 없습니다: {이름}")
    값 = {k: args[k] for k in ("등급", "국내해외", "유형", "IF") if k in args and args[k] is not None}
    고를것 = {"국내해외": ("불명", "국내", "해외"), "유형": ("학회", "저널", "불명")}
    for k, 허용 in 고를것.items():
        if k in 값 and str(값[k]) not in 허용:
            raise 도구오류(f"{k} 는 {' · '.join(허용)} 중 하나입니다.")
    if "등급" in 값 and str(값["등급"]) not in registry.tier_names():
        raise 도구오류(f"등급은 {' · '.join(registry.tier_names())} 중 하나입니다.")
    전 = registry.class_of(종류, 이름)
    try:
        registry.set_class(종류, 이름, **{k: str(v) for k, v in 값.items()})
    except 리스트등급오류 as exc:
        raise 도구오류(str(exc) + " (venue_list_save 를 쓰세요)") from exc
    후 = registry.class_of(종류, 이름)
    바뀐 = {k: [전[k], 후[k]] for k in ("등급", "국내해외", "유형", "IF") if 전[k] != 후[k]}
    for k, (a, b) in 바뀐.items():
        audit.record(me.아이디, "명칭", f"{종류}:{이름}", 항목={"유형": "학회/저널"}.get(k, k), 이전값=a, 새값=b)
    if 바뀐:
        for n in _표기들(종류, 이름):
            if not n.확인:
                registry.confirm(n.id, 사람=me.아이디)
    return {"이름": 이름, "분류": {k: 후[k] for k in ("등급", "국내해외", "유형", "IF")}, "바뀜": 바뀐}


def _names_confirm(me: User, args: dict) -> dict:
    종류 = _종류(args)
    이름 = str(args.get("이름") or "").strip()
    if 이름:
        대상 = _표기들(종류, 이름)
    else:
        try:
            대상 = [registry.get(int(x)) for x in (args.get("표기_id들") or [])]
        except (TypeError, ValueError) as exc:
            raise 도구오류("표기_id들은 숫자입니다.") from exc
        대상 = [n for n in 대상 if n is not None and n.종류 == 종류]
    if not 대상:
        raise 도구오류("확인할 표기가 없습니다 (이름 또는 표기_id들).")
    한것 = 0
    for n in 대상:
        if not n.확인:
            registry.confirm(n.id, 사람=me.아이디)
            audit.record(me.아이디, "명칭", f"{종류}:{n.원표기}", 항목="확인", 이전값="", 새값="확인함")
            한것 += 1
    return {"확인": 한것, "남은_안본것": registry.unconfirmed_count(종류)}


# --- 검토 필요 ------------------------------------------------------------------------
def _review_list(me: User, args: dict) -> dict:
    cid = str(args.get("지원자_ID") or "").strip()
    if cid:
        rec = _지원자(cid)
        끝낸 = store.review_done(cid)
        return {"지원자_ID": cid, "이름": rec.한글_이름 or rec.영문_이름, "검토_필요": rec.검토_필요 == "Y",
                "항목": [{"번호": i, "사유": x["글"], "확인함": x["완료"], "관련열": x["열"]}
                       for i, x in enumerate(reviewmod.items(rec.검토_사유, 끝낸), 1)]}
    끝낸맵 = store.review_done_map()
    나온것 = []
    for r in store.list_all():
        남은 = reviewmod.remaining(r.검토_사유, 끝낸맵.get(r.지원자_ID, set()))
        if 남은:
            나온것.append({"지원자_ID": r.지원자_ID, "이름": r.한글_이름 or r.영문_이름,
                        "남은_항목": len(남은), "항목": [reviewmod.short(x, 80) for x in 남은[:5]]})
    return {"개수": len(나온것), "지원자": 나온것,
            "알림": "자세한 항목 · 번호는 review_list(지원자_ID) 로 봅니다."}


def _review_mark(me: User, args: dict) -> dict:
    rec = _지원자(_cid(args))
    항목들 = reviewmod.split(rec.검토_사유)
    번호들 = args.get("번호들")
    if args.get("전부"):
        고른 = list(항목들)
    else:
        if not isinstance(번호들, list) or not 번호들:
            raise 도구오류("번호들(review_list 의 번호) 을 주거나 전부=true 로 부르세요.")
        try:
            고른 = [항목들[int(n) - 1] for n in 번호들]
        except (ValueError, TypeError, IndexError) as exc:
            raise 도구오류(f"없는 번호가 있습니다 (1~{len(항목들)}): {번호들}") from exc
    확인 = args.get("확인", True) is not False
    for 사유 in 고른:
        검토표시(rec.지원자_ID, 사유, 확인, me.아이디)
    남은 = reviewmod.remaining(rec.검토_사유, store.review_done(rec.지원자_ID))
    return {"지원자_ID": rec.지원자_ID, "처리": len(고른), "확인함" if 확인 else "되돌림": True,
            "남은_항목": len(남은), "검토_필요": bool(남은)}


# --- 우수 학회·저널 리스트 -------------------------------------------------------------
def _venue_list(me: User, args: dict) -> dict:
    return {"리스트": [{k: x[k] for k in ("이름", "등급", "학회저널", "비고", "고친때", "고친이")}
                    | {"별칭": [a for a in x["별칭"].split("\n") if a], "CV에서_걸린_이름": x["사전이름"]}
                    for x in registry.우수목록()],
            "규칙": "최우수 · 우수는 이 리스트에 있는 학회·저널만. 나머지는 일반 · 제외(미분류)."}


def _venue_names(me: User, args: dict) -> dict:
    말 = str(args.get("검색어") or "").strip().lower()
    묶음: dict[str, dict] = {}
    for n in registry.list_all("학회·저널"):
        d = 묶음.setdefault(n.표시명, {"이름": n.표시명, "등급": n.등급, "학회저널": n.유형,
                                    "표기들": [], "발견횟수": 0,
                                    "리스트": bool(registry.우수등급(n.표시명))})
        d["표기들"].append(n.원표기)
        d["발견횟수"] += n.발견횟수
    나온것 = [d for d in 묶음.values()
            if not 말 or 말 in (d["이름"] + " " + " ".join(d["표기들"])).lower()]
    나온것.sort(key=lambda d: -d["발견횟수"])
    return {"개수": len(나온것), "학회저널": 나온것[:300]}


def _venue_list_save(me: User, args: dict) -> dict:
    from .pages.names import 우수리스트_기록

    항목들 = args.get("항목")
    if not isinstance(항목들, list) or not 항목들:
        raise 도구오류('항목은 [{"이름": ..., "등급": "최우수|우수", "별칭": [...], "학회저널": "학회|저널", "비고": ...}] 입니다.')
    전부 = {x["이름"]: x for x in registry.우수목록()}
    결과, 실패 = [], []
    for x in 항목들:
        if not isinstance(x, dict):
            실패.append({"항목": x, "이유": "객체가 아닙니다"})
            continue
        별칭 = x.get("별칭") or ""
        if isinstance(별칭, list):
            별칭 = "\n".join(str(a) for a in 별칭)
        이름 = str(x.get("이름") or "").strip()
        전 = 전부.get(이름)
        try:
            후 = registry.우수_저장(이름, str(x.get("등급") or ""), 별칭=str(별칭),
                               학회저널=str(x.get("학회저널") or ""), 비고=str(x.get("비고") or ""),
                               누가=me.아이디, id=전["id"] if 전 else None)
        except ValueError as exc:
            실패.append({"이름": 이름, "이유": str(exc)})
            continue
        우수리스트_기록(me.아이디, 전, 후)
        결과.append({"이름": 후["이름"], "등급": 후["등급"], "처리": "고침" if 전 else "넣음"})
    return {"저장": 결과, **({"실패": 실패} if 실패 else {})}


def _venue_list_remove(me: User, args: dict) -> dict:
    from .pages.names import 우수리스트_기록

    이름 = str(args.get("이름") or "").strip()
    x = next((x for x in registry.우수목록() if x["이름"] == 이름), None)
    if x is None:
        raise 도구오류(f"리스트에 없는 이름입니다: {이름} (venue_list 의 이름 그대로)")
    뺀것 = registry.우수_지우기(x["id"])
    우수리스트_기록(me.아이디, 뺀것, None)
    return {"뺌": 이름, "알림": "이 학회·저널은 이제 미분류입니다."}


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
    도구("projects_import", "엑셀(.xlsx · .csv) 이나 과제 목록으로 과제를 추가 · 갱신 (과제번호 기준, 지우지 않음). "
       "반영=false 로 먼저 미리 보고, 사람이 괜찮다고 하면 반영=true.",
       _입력(파일명=_글("엑셀 파일 이름 (확장자 포함 — .xlsx · .csv)"),
           내용_base64=_글("엑셀 · csv 파일 내용을 base64 로"),
           항목={"type": "array", "items": {"type": "object"},
                "description": "과제마다 {과제번호, 과제명, 부서, 키워드, 핵심기술, ...} — 엑셀 대신"},
           반영={"type": "boolean", "description": "false(기본) 면 미리보기만, true 면 저장"}),
       "지원자_등록", _projects_import),
    도구("names_list", "명칭 사전: 이름별 표기(id · 원표기 · 발견횟수 · 확인) · 학회·저널이면 등급 · 국내해외 · 유형 · IF.",
       _입력(("종류",), 종류={"type": "string", "enum": ["소속", "학회·저널", "전공"]},
           안본것만={"type": "boolean", "description": "true 면 아직 사람이 안 본 이름만"},
           검색어=_글("찾을 글자"), 개수={"type": "integer", "description": "최대 이름 수 (기본 300)"}),
       "명칭_관리", _names_list),
    도구("names_rename", "이름을 바꾼다 — 그 이름을 쓰는 표기 전부. 새이름이 이미 있으면 합쳐진다.",
       _입력(("종류", "이름", "새이름"), 종류={"type": "string", "enum": ["소속", "학회·저널", "전공"]},
           이름=_글("names_list 의 이름 그대로"), 새이름=_글("표에 보일 새 이름")),
       "명칭_관리", _names_rename),
    도구("names_move", "고른 표기만 다른 이름으로 옮긴다 (잘못 묶인 것 떼기 · 몇 개 합치기).",
       _입력(("종류", "표기_id들", "새이름"), 종류={"type": "string", "enum": ["소속", "학회·저널", "전공"]},
           표기_id들={"type": "array", "items": {"type": "integer"}}, 새이름=_글("옮길 이름")),
       "명칭_관리", _names_move),
    도구("names_classify", "학회·저널 이름의 분류를 바꾼다. 등급은 일반 · 제외 · 미분류만 (최우수 · 우수는 venue_list_save).",
       _입력(("종류", "이름"), 종류={"type": "string", "enum": ["학회·저널"]}, 이름=_글("names_list 의 이름"),
           등급=_글("일반 · 제외 · 미분류"), 국내해외={"type": "string", "enum": ["불명", "국내", "해외"]},
           유형={"type": "string", "enum": ["학회", "저널", "불명"]}, IF=_글("임팩트 팩터 (빈 문자열이면 지움)")),
       "명칭_관리", _names_classify),
    도구("names_confirm", "사람이 봤다는 표시 (이름의 표기 전부, 또는 표기_id들).",
       _입력(("종류",), 종류={"type": "string", "enum": ["소속", "학회·저널", "전공"]},
           이름=_글("이름 (그 이름의 표기 전부)"), 표기_id들={"type": "array", "items": {"type": "integer"}}),
       "명칭_관리", _names_confirm),
    도구("review_list", "검토가 남은 지원자 목록. 지원자_ID 를 주면 그 사람의 검토 항목(번호 · 사유 · 확인함 여부).",
       _입력(지원자_ID=_글("지원자_ID (비우면 검토가 남은 사람 전체)")), "지원자_목록", _review_list),
    도구("review_mark", "검토 항목을 «확인함» 으로 (확인=false 면 되돌림). 남은 게 없으면 «검토 필요» 가 내려간다. "
       "사람이 시킨 것 · CV 원문과 대조해 본 것만.",
       _입력(("지원자_ID",), 지원자_ID=_글("지원자_ID"),
           번호들={"type": "array", "items": {"type": "integer"}, "description": "review_list 의 번호들"},
           전부={"type": "boolean", "description": "true 면 그 사람의 모든 항목"},
           확인={"type": "boolean", "description": "기본 true. false 면 확인함을 되돌린다"}),
       "지원자_수정", _review_mark),
    도구("venue_list", "우수 학회·저널 리스트 (최우수 · 우수는 여기 있는 것만).", _입력(), "명칭_관리", _venue_list),
    도구("venue_names", "지원자 CV 에 나온 학회·저널 이름 · 지금 등급 · 리스트 여부 (많이 나온 순).",
       _입력(검색어=_글("찾을 글자 (비우면 전체)")), "명칭_관리", _venue_names),
    도구("venue_list_save", "우수 학회·저널 리스트에 넣거나 고친다 (이름이 같으면 고침). 사람이 정해 준 것만.",
       _입력(("항목",), 항목={"type": "array", "description": "넣거나 고칠 항목들", "items": {
           "type": "object", "required": ["이름", "등급"], "properties": {
               "이름": {"type": "string"}, "등급": {"type": "string", "enum": ["최우수", "우수"]},
               "별칭": {"type": "array", "items": {"type": "string"}, "description": "약어 · 다른 표기"},
               "학회저널": {"type": "string", "enum": ["", "학회", "저널"]},
               "비고": {"type": "string"}}}}),
       "명칭_관리", _venue_list_save),
    도구("venue_list_remove", "우수 학회·저널 리스트에서 뺀다 (그 학회·저널은 미분류가 된다).",
       _입력(("이름",), 이름=_글("venue_list 의 이름 그대로")), "명칭_관리", _venue_list_remove),
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

