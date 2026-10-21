"""검증 자료 정리 — 웹 화면 (표준 라이브러리만).

  python -m verifysum            → http://127.0.0.1:8795

  VERIFYSUM_PORT       포트 (기본 8795)
  VERIFYSUM_HOST       기본 0.0.0.0 (다른 PC 에서도 열림). 이 PC 에서만 쓰려면 127.0.0.1
  VERIFYSUM_PASSWORD   있으면 브라우저가 묻는다 (아이디는 아무거나)
  VERIFYSUM_WORKERS    동시에 LLM 에 보낼 사람 수 (기본 2)
  VERIFYSUM_DATA_DIR   학습 결과·결과 파일 둘 곳 (기본 ~/.cvtool/verifysum)
  VERIFYSUM_LLM_TIMEOUT  LLM 한 번 기다리는 시간(초, 기본 600 — 추론 모델이라 길게)
  VERIFYSUM_RETRIES    타임아웃·연결 끊김·서버 5xx 때 알아서 다시 하는 횟수 (기본 3)

결과는 돌릴 때마다 <데이터>/결과/<날짜-시각>/ 에 남는다 — 화면 '지난 결과' 에서 다시 받는다.

LLM 서버 설정은 지원자 관리와 같은 .env (CVTOOL_LLM_*) 를 그대로 읽는다.
위 VERIFYSUM_* 도 .env 에 적어 두면 된다.
올린 후보자 원본 파일은 디스크에 남기지 않는다 (결과 파일만 남는다).
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from email.parser import BytesParser
from email.policy import default as 기본정책
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cvtool import dotenv as _dotenv  # noqa: F401 - .env 를 먼저 읽는다 (아래 설정보다 앞서)

from . import docxio as D
from . import fit, generate, learn

폴더 = Path(os.environ.get("VERIFYSUM_DATA_DIR")
          or Path(os.environ.get("CVTOOL_DATA_DIR", Path.home() / ".cvtool")) / "verifysum")
결과_1장 = "검증의견_1장_통합.docx"
결과_요약 = "개인별_검증결과_요약.docx"
결과_목록 = "결과.json"
결과_폴더 = "결과"
_번호꼴 = re.compile(r"^\d{8}-\d{6}(-\d+)?$")


def _클라이언트():
    """LLM 클라이언트. 지원자 관리와 같은 설정(.env)을 쓰고, 기다리는 시간만 길게."""
    import httpx

    from cvtool.clients.llm import LLMClient

    return LLMClient(client=httpx.Client(timeout=float(os.environ.get("VERIFYSUM_LLM_TIMEOUT", "600"))))


def _일시오류(exc: Exception) -> bool:
    """다시 하면 될 수도 있는 오류 — 타임아웃 · 연결 끊김 · 서버 바쁨(429 · 5xx)."""
    글 = f"{type(exc).__name__} {exc}".lower()
    return ("timeout" in 글 or "timed out" in 글 or "요청 실패" in 글 or "connect" in 글
            or bool(re.search(r"오류 (429|5\d\d)", 글)))


class 다시하는LLM:
    """LLM 클라이언트를 감싸 일시 오류면 조금 쉬었다 다시 부른다."""

    쉼 = (5, 20, 60)

    def __init__(self, c, 횟수: int | None = None):
        self.c = c
        self.횟수 = int(os.environ.get("VERIFYSUM_RETRIES", "3")) if 횟수 is None else 횟수
        self.알림 = lambda n, exc: None          # (몇 번째 다시, 오류) — 화면 메모에 띄운다

    def _부름(self, 이름: str, *a, **k):
        for n in range(self.횟수 + 1):
            try:
                return getattr(self.c, 이름)(*a, **k)
            except Exception as exc:  # noqa: BLE001
                if n >= self.횟수 or not _일시오류(exc):
                    raise
                self.알림(n + 1, exc)
                time.sleep(self.쉼[min(n, len(self.쉼) - 1)])

    def chat_text(self, *a, **k):
        return self._부름("chat_text", *a, **k)

    def chat_json(self, *a, **k):
        return self._부름("chat_json", *a, **k)

    def close(self):
        getattr(self.c, "close", lambda: None)()


# --- 작업 상태 -----------------------------------------------------------------------
_잠금 = threading.Lock()
작업: dict = {"돌고있음": False, "줄": [], "시작": 0.0, "끝": 0.0, "오류": "", "번호": ""}
# 이번 작업의 원본 · 결과 — 메모리에만 둔다 ('다시 하기' 용. 원본은 디스크에 남기지 않는다)
_자료들: list = []
_결과들: list = []


def _줄바꾸기(i: int, **값) -> None:
    with _잠금:
        작업["줄"][i].update(값)


def _자연정렬(이름: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", 이름)]


def _결과자리(번호: str) -> Path:
    return 폴더 / 결과_폴더 / 번호


def _새번호() -> str:
    번호 = time.strftime("%Y%m%d-%H%M%S")
    k = 1
    while _결과자리(번호).exists():
        k += 1
        번호 = time.strftime("%Y%m%d-%H%M%S") + f"-{k}"
    return 번호


def 작업시작(파일들: list[tuple[str, bytes]], 학습: dict) -> None:
    파일들 = sorted(파일들, key=lambda f: _자연정렬(f[0]))
    자료들: list = []
    with _잠금:
        작업.update(돌고있음=True, 줄=[], 시작=time.time(), 끝=0.0, 오류="", 번호=_새번호())
        for 이름, 내용 in 파일들:
            try:
                자료 = D.검증자료_읽기(내용, 이름)
                기대 = len(학습["원본"]["항목"])
                읽음 = f"항목 {len(자료.항목)}개 · {sum(len(b) for _a, b in 자료.항목)}자"
                메모 = (f"예시는 항목이 {기대}개인데 {len(자료.항목)}개를 읽었습니다."
                      if len(자료.항목) != 기대 else "")
                작업["줄"].append({"파일": 이름, "후보자": 자료.제목, "상태": "대기",
                                "읽음": 읽음, "메모": 메모, "앞메모": 메모})
            except D.형식오류 as exc:
                자료 = None
                작업["줄"].append({"파일": 이름, "후보자": "", "상태": "실패", "메모": str(exc)})
            자료들.append(자료)
        _자료들[:] = 자료들
        _결과들[:] = [None] * len(자료들)
    threading.Thread(target=_돌리기, args=(list(range(len(자료들))), 학습), daemon=True).start()


def 다시하기(번호들: list[int] | None, 학습: dict) -> int:
    """실패한 사람(또는 고른 사람)만 다시. 다시 돌리는 사람 수."""
    with _잠금:
        if 작업["돌고있음"]:
            return -1
        고른 = [i for i in (번호들 if 번호들 is not None else range(len(_자료들)))
              if 0 <= i < len(_자료들) and _자료들[i] is not None
              and (번호들 is not None or 작업["줄"][i]["상태"] == "실패")]
        if not 고른:
            return 0
        for i in 고른:
            작업["줄"][i].update(상태="대기", 메모=작업["줄"][i].get("앞메모") or "")
        작업.update(돌고있음=True, 끝=0.0, 오류="")
    threading.Thread(target=_돌리기, args=(고른, 학습), daemon=True).start()
    return len(고른)


def _돌리기(번호들: list[int], 학습: dict) -> None:
    로컬 = threading.local()
    만든것: list = []

    def 하나(i: int) -> None:
        자료 = _자료들[i]
        if 자료 is None:
            return
        if not hasattr(로컬, "c"):
            로컬.c = 다시하는LLM(_클라이언트())
            만든것.append(로컬.c)
        로컬.c.알림 = lambda n, exc: _줄바꾸기(
            i, 메모=f"LLM 응답이 없어 다시 시도 중 ({n}/{로컬.c.횟수}) — {type(exc).__name__}")
        _줄바꾸기(i, 상태="처리중")
        try:
            r = generate.한사람(자료, 학습, 로컬.c)
            _결과들[i] = r
            앞메모 = 작업["줄"][i].get("앞메모") or ""
            경고 = ([앞메모] if 앞메모 else []) + r.경고
            _줄바꾸기(i, 상태="확인필요" if 경고 else "완료", 메모=" / ".join(경고),
                    전문성=r.전문성, 역량=r.역량, 작성자=" / ".join(r.작성자), 원답=r.원답)
        except Exception as exc:  # noqa: BLE001 - 한 사람 실패가 전체를 멈추면 안 된다
            _결과들[i] = None
            왜 = ("LLM 이 응답하지 않았습니다 (다시 시도해도 안 됨). '다시 하기' 를 눌러 주세요. "
                 if _일시오류(exc) else "")
            _줄바꾸기(i, 상태="실패", 메모=f"{왜}{type(exc).__name__}: {str(exc)[:200]}")

    try:
        일꾼 = max(1, int(os.environ.get("VERIFYSUM_WORKERS", "2")))
        with ThreadPoolExecutor(일꾼) as 풀:
            list(풀.map(하나, 번호들))
        _파일만들기(학습)
    except Exception as exc:  # noqa: BLE001
        with _잠금:
            작업["오류"] = f"결과 파일을 만들지 못했습니다: {type(exc).__name__}: {exc}"
    finally:
        for c in 만든것:
            c.close()
        with _잠금:
            작업.update(돌고있음=False, 끝=time.time())
            _기록하기()


def _파일만들기(학습: dict) -> None:
    된것 = [r for r in _결과들 if r is not None]
    자리 = _결과자리(작업["번호"])
    자리.mkdir(parents=True, exist_ok=True)
    if not 된것:
        return
    (자리 / 결과_1장).write_bytes(generate.한장파일(된것, (폴더 / learn.틀_1장).read_bytes()))
    (자리 / 결과_요약).write_bytes(generate.요약파일(된것, (폴더 / learn.틀_요약).read_bytes(), 학습))


def _기록하기() -> None:
    """진행 표를 결과 폴더에 남긴다 (잠금 안에서 부른다)."""
    if not 작업.get("번호"):
        return
    자리 = _결과자리(작업["번호"])
    자리.mkdir(parents=True, exist_ok=True)
    (자리 / 결과_목록).write_text(json.dumps(작업, ensure_ascii=False), encoding="utf-8")


def 지난결과() -> list[dict]:
    """[{번호, 때, 사람, 셈, 파일있음}] 최근 것부터."""
    나온것 = []
    for d in sorted((폴더 / 결과_폴더).glob("*"), reverse=True):
        if not (d.is_dir() and _번호꼴.match(d.name)):
            continue
        try:
            기록 = json.loads((d / 결과_목록).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            기록 = {"줄": []}
        셈: dict[str, int] = {}
        for x in 기록.get("줄", []):
            셈[x.get("상태", "")] = 셈.get(x.get("상태", ""), 0) + 1
        나온것.append({"번호": d.name, "때": time.strftime("%Y-%m-%d %H:%M", time.localtime(기록.get("시작") or d.stat().st_mtime)),
                     "사람": len(기록.get("줄", [])), "셈": 셈,
                     "파일있음": (d / 결과_1장).exists() and (d / 결과_요약).exists()})
    return 나온것


def _마지막_불러오기() -> None:
    """프로그램을 다시 켜도 마지막 진행 표가 보이게 (원본이 없으니 '다시 하기' 는 안 된다)."""
    지난 = 지난결과()
    if not 지난:
        return
    try:
        기록 = json.loads((_결과자리(지난[0]["번호"]) / 결과_목록).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    기록["돌고있음"] = False
    for x in 기록.get("줄", []):
        if x.get("상태") in ("대기", "처리중"):
            x.update(상태="실패", 메모="프로그램이 꺼져 끝나지 못했습니다.")
    작업.update(기록)


# --- 화면 ----------------------------------------------------------------------------
CSS = """
:root{--bg:#f4f6fa;--card:#fff;--line:#e3e8ef;--txt:#1b2b4b;--muted:#5b6b85;--acc:#2563eb;--acc2:#e3edfe;
--ok:#15803d;--warn:#b45309;--bad:#b91c1c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font:15px/1.6 'Malgun Gothic','Apple SD Gothic Neo',sans-serif}
header{background:#fff;border-bottom:1px solid var(--line);padding:14px 28px;font-weight:700;font-size:17px}
main{max-width:1100px;margin:24px auto;padding:0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px 26px;margin-bottom:18px}
h2{margin:0 0 4px;font-size:18px}h2 .n{display:inline-block;width:26px;height:26px;border-radius:50%;
background:var(--acc);color:#fff;text-align:center;font-size:14px;line-height:26px;margin-right:8px}
.muted{color:var(--muted);font-size:13.5px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:14px 0}
.drop{border:1.5px dashed #c9d6ea;border-radius:10px;padding:14px;background:#fafcff}
.drop b{display:block;margin-bottom:6px}button,.btn{background:var(--acc);color:#fff;border:0;border-radius:8px;
padding:9px 18px;font-size:14.5px;cursor:pointer;text-decoration:none;display:inline-block}
button.sec,.btn.sec{background:#fff;color:var(--acc);border:1px solid #c9d6ea}button:disabled{opacity:.45;cursor:default}
table{width:100%;border-collapse:collapse;margin-top:10px;font-size:14px}th,td{border-bottom:1px solid var(--line);
padding:7px 8px;text-align:left;vertical-align:top}th{background:#f7f9fc;font-weight:600;color:var(--muted)}
.pill{display:inline-block;white-space:nowrap;border-radius:999px;padding:1px 10px;font-size:12.5px;font-weight:600;background:#eef2f7}
.s-완료{background:#dcfce7;color:var(--ok)}.s-확인필요{background:#fef3c7;color:var(--warn)}
.s-실패{background:#fee2e2;color:var(--bad)}.s-처리중{background:var(--acc2);color:var(--acc)}
textarea{width:100%;min-height:260px;font:13.5px/1.6 'Malgun Gothic',sans-serif;border:1px solid var(--line);
border-radius:8px;padding:10px}.kv td:first-child{width:180px;color:var(--muted)}
.msg{padding:10px 14px;border-radius:8px;margin-bottom:14px}.msg.ok{background:#dcfce7}.msg.bad{background:#fee2e2}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px}
"""


def _쪽(본문: str, 알림: str = "", 나쁨: bool = False) -> bytes:
    알 = f"<div class='msg {'bad' if 나쁨 else 'ok'}'>{html.escape(알림)}</div>" if 알림 else ""
    return ("<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>검증 자료 정리</title><style>{CSS}</style></head><body>"
            "<header>검증 자료 정리 <span class='muted' style='font-weight:400'>· 1장 통합본 + 요약표</span></header>"
            f"<main>{알}{본문}</main>{_JS}</body></html>").encode("utf-8")


def _학습칸(학습: dict | None) -> str:
    파일칸 = "".join(
        f"<div class='drop'><b>{제목}</b><input type='file' name='{이름}' accept='.docx' required>"
        f"<div class='muted'>{설명}</div></div>"
        for 이름, 제목, 설명 in (
            ("원본", "예시 검증 파일", "한 사람의 원본 (2장짜리)"),
            ("한장", "예시 1장 결과", "그 사람을 1장으로 줄인 것"),
            ("요약", "예시 요약 결과", "개인별 검증결과 요약 표")))
    양식 = ("<form method='post' action='/learn' enctype='multipart/form-data'>"
          f"<div class='grid'>{파일칸}</div>"
          "<div class='row'><button>분석하기</button><span class='muted'>"
          "예시를 보고 문체·분량·칸 너비를 파악해 작성 지침을 만듭니다 (1~2분).</span></div></form>")
    if not 학습:
        return ("<div class='card'><h2><span class='n'>1</span>예시로 방식 익히기</h2>"
                "<p class='muted'>세 파일을 넣으면 어떻게 줄이고 어떤 말투로 쓰는지 정리합니다.</p>"
                f"{양식}</div>")
    칸 = 학습["칸"]
    전 = 칸["전문성"]
    한줄 = 전["폭_pt"] / 전["크기_pt"]
    항목 = " · ".join(a for a, _b in 학습["원본"]["항목"])
    정보 = (
        "<table class='kv'>"
        f"<tr><td>예시 후보자</td><td>{html.escape(학습['원본']['제목'])} "
        f"<span class='muted'>({html.escape(학습['원본']['소속'])})</span></td></tr>"
        f"<tr><td>항목 {len(학습['원본']['항목'])}개</td><td>{html.escape(항목)}</td></tr>"
        f"<tr><td>분량</td><td>원본 {sum(학습['분량']['원본글자'])}자 → 1장 "
        f"{sum(학습['분량']['1장글자'])}자 (이 분량을 한 쪽 기준으로 씁니다)</td></tr>"
        f"<tr><td>요약 칸</td><td>너비 {전['폭_pt'] / 72 * 2.54 + 0.38:.1f}cm · "
        f"{html.escape(전.get('글꼴') or '')} {전['크기_pt']:g}pt → 한 줄에 한글 약 {int(한줄)}자, "
        f"두 줄 약 {fit.줄에_맞는_글자수(한줄)}자로 씁니다</td></tr>"
        f"<tr><td>분석한 때</td><td>{html.escape(학습['만든때'])}</td></tr></table>")
    return (
        "<div class='card'><h2><span class='n'>1</span>예시로 익힌 방식</h2>" + 정보 +
        "<h3 style='margin:18px 0 6px;font-size:15px'>작성 지침 <span class='muted'>"
        "(결과를 만들 때 LLM 에 그대로 넘깁니다. 고쳐서 저장할 수 있어요)</span></h3>"
        "<form method='post' action='/guide'><textarea name='지침'>"
        f"{html.escape(학습.get('지침') or '')}</textarea>"
        "<div class='row'><button class='sec'>지침 저장</button></div></form>"
        + _들여다보기(학습) +
        "<form method='post' action='/relearn' class='row' onsubmit=\"this.querySelector('button').disabled=true;"
        "this.querySelector('button').textContent='분석 중… (1~2분)'\">"
        "<button class='sec'>올려 둔 예시로 다시 분석</button><span class='muted'>파일을 다시 올리지 않고 "
        "지침을 새로 만듭니다 (프로그램이 바뀌었을 때). 위에서 손으로 고친 지침은 새 것으로 바뀝니다.</span></form>"
        "<details style='margin-top:14px'><summary class='muted'>다른 예시로 다시 분석하기</summary>"
        f"{양식}</details></div>")


def _들여다보기(학습: dict) -> str:
    """예시 원본에서 무엇을 읽었는지, LLM 에 실제로 무엇을 보내는지 그대로 보여 준다."""
    원 = 학습["원본"]
    항목줄 = "".join(
        f"<tr><td>{html.escape(a)}<br><span class='muted'>{len(b)}자</span></td>"
        f"<td style='white-space:pre-wrap'>{html.escape(b)}</td></tr>" for a, b in 원["항목"])
    예 = D.검증자료(제목=원["제목"], 소속=원["소속"], 항목=[tuple(x) for x in 원["항목"]],
                 끝글=원.get("끝글", ""))
    메시지 = generate.프롬프트(예, 학습)
    보냄 = "\n\n".join(f"──── {m['role']} ────\n{m['content']}" for m in 메시지)
    return (
        "<details style='margin-top:14px'><summary class='muted'>예시 원본에서 읽은 내용 보기 "
        f"(항목 {len(원['항목'])}개 · {sum(len(b) for _a, b in 원['항목'])}자 — 2쪽까지 다 읽혔는지 확인)"
        f"</summary><table>{항목줄}</table>"
        f"<p class='muted'>표 뒤 끝부분: {html.escape(원.get('끝글') or '(없음)')}</p></details>"
        "<details style='margin-top:8px'><summary class='muted'>LLM 에 실제로 보내는 내용 보기 "
        "(예시 후보자를 넣었을 때 — 지침 말고도 규칙·분량·예시 원문이 같이 갑니다)</summary>"
        f"<pre style='white-space:pre-wrap;font-size:12.5px;background:#f7f9fc;padding:12px;"
        f"border-radius:8px;max-height:480px;overflow:auto'>{html.escape(보냄)}</pre></details>")


def _실행칸(학습: dict | None) -> str:
    with _잠금:
        돈다 = 작업["돌고있음"]
    막음 = " disabled" if (not 학습 or 돈다) else ""
    return ("<div class='card'><h2><span class='n'>2</span>후보자 검증 파일 넣기</h2>"
            "<p class='muted'>30~40개를 한 번에 골라 넣으세요. 파일 이름 순서대로 결과에 들어갑니다. "
            "원본 파일은 저장하지 않습니다.</p>"
            "<form method='post' action='/run' enctype='multipart/form-data'>"
            "<div class='drop'><input type='file' name='files' accept='.docx' multiple required></div>"
            f"<div class='row'><button{막음}>결과 만들기</button>"
            + ("" if 학습 else "<span class='muted'>먼저 1단계를 해 주세요.</span>")
            + "</div></form></div>")


def _결과칸() -> str:
    return ("<div class='card' id='진행'><h2><span class='n'>3</span>진행 · 결과</h2>"
            "<div id='진행몸'><p class='muted'>아직 돌린 작업이 없습니다.</p></div></div>")


def _지난결과칸() -> str:
    지난 = 지난결과()
    if not 지난:
        return ""
    줄 = "".join(
        f"<tr><td>{html.escape(x['때'])}</td><td>{x['사람']}명</td><td>"
        + " ".join(f"<span class='pill s-{html.escape(k)}'>{html.escape(k)} {v}</span>" for k, v in x["셈"].items())
        + "</td><td>"
        + (f"<a href='/past/{x['번호']}/1'>1장 통합본</a> · <a href='/past/{x['번호']}/2'>요약표</a> · "
           if x["파일있음"] else "<span class='muted'>파일 없음</span> · ")
        + f"<a href='/past/{x['번호']}'>진행 표</a></td></tr>" for x in 지난)
    return ("<div class='card'><h2>지난 결과</h2>"
            f"<p class='muted'>돌릴 때마다 여기 남습니다 · 저장 위치: {html.escape(str(폴더 / 결과_폴더))}</p>"
            f"<table><tr><th>돌린 때</th><th>사람</th><th>상태</th><th>받기</th></tr>{줄}</table></div>")


def _지난표(번호: str) -> str:
    기록 = json.loads((_결과자리(번호) / 결과_목록).read_text(encoding="utf-8"))
    줄 = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(x.get(k) or ''))}</td>"
                         for k in ("파일", "후보자", "상태", "전문성", "역량", "작성자", "메모")) + "</tr>"
        for x in 기록.get("줄", []))
    return (f"<div class='card'><h2>{html.escape(번호)} 진행 표</h2><p><a href='/'>← 처음으로</a> · "
            f"<a href='/past/{번호}/1'>1장 통합본</a> · <a href='/past/{번호}/2'>요약표</a></p>"
            "<table><tr><th>파일</th><th>후보자</th><th>상태</th><th>전문성/성과</th><th>연구전문가 역량</th>"
            f"<th>작성자</th><th>메모</th></tr>{줄}</table></div>")


_JS = """<script>
function esc(s){return String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function poll(){
  let r=await fetch('/status'); let s=await r.json(); let el=document.getElementById('진행몸'); if(!el) return;
  if(!s.줄.length){ setTimeout(poll, 3000); return; }
  let n=s.줄.length, done=s.줄.filter(x=>['완료','확인필요','실패'].includes(x.상태)).length;
  let h='<p>'+(s.돌고있음?'<b>처리 중</b> ':'<b>끝남</b> ')+done+' / '+n+'명</p>';
  if(s.오류) h+='<div class="msg bad">'+esc(s.오류)+'</div>';
  if(!s.돌고있음 && s.파일있음) h+='<div class="row"><a class="btn" href="/download/1">1장 통합본 받기</a>'+
     '<a class="btn" href="/download/2">요약표 받기</a><span class="muted">확인필요 표시된 사람은 워드에서 한 번 봐 주세요. '+
     '파일은 아래 「지난 결과」에도 남습니다.</span></div>';
  let 실패=s.줄.filter(x=>x.상태==='실패').length;
  if(!s.돌고있음 && 실패 && s.다시가능) h+='<form method="post" action="/retry" class="row"><button>실패한 '+실패+
     '명 다시 하기</button><span class="muted">LLM 타임아웃 등으로 실패한 사람만 다시 돌리고 결과 파일을 새로 만듭니다.</span></form>';
  if(!s.돌고있음 && 실패 && !s.다시가능) h+='<p class="muted">프로그램을 다시 켜서 원본이 없습니다. 실패한 사람은 파일을 다시 넣어 주세요.</p>';
  h+='<table><tr><th>파일</th><th>후보자</th><th>읽은 내용</th><th>상태</th><th>전문성/성과</th><th>연구전문가 역량</th><th>작성자</th><th>메모</th></tr>';
  for(const x of s.줄){ h+='<tr><td>'+esc(x.파일)+'</td><td>'+esc(x.후보자)+'</td><td class="muted">'+esc(x.읽음)+'</td><td><span class="pill s-'+esc(x.상태)+'">'+esc(x.상태)+
     '</span></td><td>'+esc(x.전문성)+'</td><td>'+esc(x.역량)+'</td><td>'+esc(x.작성자)+'</td><td class="muted">'+esc(x.메모)+
     (!s.돌고있음 && s.다시가능 && x.상태!=='대기' && x.읽음 ? '<form method="post" action="/retry" style="margin-top:4px">'+
       '<input type="hidden" name="i" value="'+s.줄.indexOf(x)+'"><button class="sec" style="padding:2px 10px;font-size:12.5px">이 사람 다시</button></form>' : '')+
     (x.원답?'<details><summary>LLM 답 보기</summary><pre style="white-space:pre-wrap;font-size:12px;max-width:420px">'+esc(x.원답)+'</pre></details>':'')+'</td></tr>'; }
  el.innerHTML=h+'</table>';
  if(s.돌고있음) setTimeout(poll, 2000);
}
poll();
</script>"""


def _폼(본문: bytes, 형식: str) -> tuple[dict[str, str], dict[str, list[tuple[str, bytes]]]]:
    """multipart 를 (글 필드, {이름: [(파일명, 내용)]}) 로."""
    msg = BytesParser(policy=기본정책).parsebytes(
        b"Content-Type: " + 형식.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + 본문)
    글, 파일 = {}, {}
    for part in msg.iter_parts():
        이름 = part.get_param("name", header="content-disposition") or ""
        파일명 = part.get_filename()
        if 파일명 is not None:
            내용 = part.get_payload(decode=True) or b""
            if 내용:
                파일.setdefault(이름, []).append((Path(파일명).name, 내용))
        else:
            글[이름] = part.get_content()
    return 글, 파일


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 조용히
        pass

    def _보냄(self, 본문: bytes, 형식: str = "text/html; charset=utf-8", 코드: int = 200, 머리=None):
        self.send_response(코드)
        self.send_header("Content-Type", 형식)
        self.send_header("Content-Length", str(len(본문)))
        for k, v in (머리 or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(본문)

    def _문지기(self) -> bool:
        비번 = os.environ.get("VERIFYSUM_PASSWORD", "")
        if not 비번:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                if base64.b64decode(h[6:]).decode().split(":", 1)[1] == 비번:
                    return True
            except Exception:  # noqa: BLE001
                pass
        self._보냄(b"", 코드=401, 머리={"WWW-Authenticate": 'Basic realm="verifysum"'})
        return False

    def _본문(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _첫화면(self, 알림: str = "", 나쁨: bool = False):
        학습 = learn.불러오기(폴더)
        self._보냄(_쪽(_학습칸(학습) + _실행칸(학습) + _결과칸() + _지난결과칸(), 알림, 나쁨))

    def do_GET(self):
        if not self._문지기():
            return
        경로 = urllib.parse.urlparse(self.path).path
        if 경로 == "/":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._첫화면((q.get("msg") or [""])[0], bool(q.get("bad")))
        if 경로 == "/status":
            with _잠금:
                s = dict(작업)
                s["다시가능"] = bool(_자료들)
            자리 = _결과자리(s["번호"]) if s.get("번호") else None
            s["파일있음"] = bool(자리 and (자리 / 결과_1장).exists() and (자리 / 결과_요약).exists() and s["끝"])
            return self._보냄(json.dumps(s, ensure_ascii=False).encode(), "application/json")
        m = re.fullmatch(r"/past/([0-9-]+)(?:/([12]))?", 경로)
        if m and _번호꼴.match(m.group(1)) and _결과자리(m.group(1)).is_dir():
            if not m.group(2):
                try:
                    return self._보냄(_쪽(_지난표(m.group(1))))
                except (OSError, ValueError):
                    return self._보냄("진행 표가 없습니다".encode(), 코드=404)
            경로, 번호 = "/download/" + m.group(2), m.group(1)
        else:
            with _잠금:
                번호 = 작업.get("번호") or ""
        if 경로 in ("/download/1", "/download/2") and 번호:
            이름 = 결과_1장 if 경로.endswith("1") else 결과_요약
            p = _결과자리(번호) / 이름
            if not p.exists():
                return self._보냄("아직 결과가 없습니다".encode(), 코드=404)
            return self._보냄(p.read_bytes(),
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            머리={"Content-Disposition":
                                  "attachment; filename*=UTF-8''" + urllib.parse.quote(이름)})
        self._보냄(b"not found", 코드=404)

    def _돌아가기(self, 알림: str, 나쁨: bool = False):
        self._보냄(b"", 코드=303, 머리={"Location": "/?msg=" + urllib.parse.quote(알림)
                                         + ("&bad=1" if 나쁨 else "")})

    def do_POST(self):
        if not self._문지기():
            return
        경로 = urllib.parse.urlparse(self.path).path
        형식 = self.headers.get("Content-Type", "")
        본문 = self._본문()
        if 경로 == "/learn":
            _글, 파일 = _폼(본문, 형식)
            try:
                원본, 한장, 요약 = (파일[k][0][1] for k in ("원본", "한장", "요약"))
            except (KeyError, IndexError):
                return self._돌아가기("세 파일을 모두 넣어 주세요.", True)
            try:
                학습 = learn.분석(원본, 한장, 요약)
            except D.형식오류 as exc:
                return self._돌아가기(str(exc), True)
            c = _클라이언트()
            try:
                학습["지침"] = learn.지침_만들기(학습, c)
            finally:
                getattr(c, "close", lambda: None)()
            learn.저장(폴더, 학습, 한장, 요약, 원본)
            return self._돌아가기("예시를 분석했습니다. 지침을 확인하고 2단계로 넘어가세요.")
        if 경로 == "/relearn":
            c = _클라이언트()
            try:
                learn.다시분석(폴더, c)
            except D.형식오류 as exc:
                return self._돌아가기(str(exc), True)
            finally:
                getattr(c, "close", lambda: None)()
            return self._돌아가기("올려 둔 예시로 다시 분석했습니다. 지침을 확인해 주세요.")
        if 경로 == "/guide":
            학습 = learn.불러오기(폴더)
            if not 학습:
                return self._돌아가기("먼저 예시를 분석해 주세요.", True)
            글 = urllib.parse.parse_qs(본문.decode("utf-8", "replace")).get("지침", [""])[0]
            학습["지침"] = 글.strip()
            learn.저장(폴더, 학습)
            return self._돌아가기("지침을 저장했습니다.")
        if 경로 == "/run":
            학습 = learn.불러오기(폴더)
            if not 학습:
                return self._돌아가기("먼저 예시를 분석해 주세요.", True)
            with _잠금:
                if 작업["돌고있음"]:
                    return self._돌아가기("이미 돌고 있는 작업이 있습니다.", True)
            _글, 파일 = _폼(본문, 형식)
            목록 = 파일.get("files", [])
            if not 목록:
                return self._돌아가기("파일을 골라 주세요.", True)
            작업시작(목록, 학습)
            return self._돌아가기(f"{len(목록)}개 파일을 정리하기 시작했습니다.")
        if 경로 == "/retry":
            학습 = learn.불러오기(폴더)
            if not 학습:
                return self._돌아가기("먼저 예시를 분석해 주세요.", True)
            if not _자료들:
                return self._돌아가기("프로그램을 다시 켜서 원본이 없습니다. 파일을 다시 넣어 주세요.", True)
            값 = urllib.parse.parse_qs(본문.decode("utf-8", "replace")).get("i", [""])[0]
            n = 다시하기([int(값)] if 값.isdigit() else None, 학습)
            if n < 0:
                return self._돌아가기("이미 돌고 있는 작업이 있습니다.", True)
            if n == 0:
                return self._돌아가기("다시 할 사람이 없습니다.", True)
            return self._돌아가기(f"{n}명을 다시 돌립니다. 끝나면 결과 파일을 새로 만듭니다.")
        self._보냄(b"not found", 코드=404)


def _내_ip() -> str:
    """다른 PC 에서 칠 주소. 밖으로 나가는 길의 내 주소 (실제로 보내지는 않는다)."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "이-PC-IP"


def main() -> None:
    # 지원자 관리(CVTOOL_WEB_HOST 기본 0.0.0.0)와 같게 — 다른 PC 에서도 들어온다
    호스트 = os.environ.get("VERIFYSUM_HOST", "0.0.0.0")
    포트 = int(os.environ.get("VERIFYSUM_PORT", "8795"))
    if 호스트 in ("0.0.0.0", "::"):
        print(f"검증 자료 정리: 이 PC http://127.0.0.1:{포트}  ·  다른 PC http://{_내_ip()}:{포트}")
        if not os.environ.get("VERIFYSUM_PASSWORD"):
            print("  (같은 망의 누구나 열 수 있습니다. 막으려면 .env 에 VERIFYSUM_PASSWORD=비밀번호)")
    else:
        print(f"검증 자료 정리: http://{호스트}:{포트}")
    print(f"  데이터: {폴더}  (결과 파일은 {폴더 / 결과_폴더})", flush=True)
    _마지막_불러오기()
    ThreadingHTTPServer((호스트, 포트), Handler).serve_forever()
