"""대시보드 — 블록 · 수식 · 시트 · 거르개 · 누구인지.
"""

from __future__ import annotations

import contextvars
import html
import json
import re
import threading
import urllib.parse

from ... import dash_draft, expr, formula as F, profile_form as P, review, sheet as S
from ...auth import can, User
from ...dashboards import (
    AXIS_SOURCES, BLOCK_KINDS, CELL_FORMATS, format_cell, render_list, render_profile,
    render_sheet, render_table, ROW_TARGET, SHEET_BORDER_STYLES, SHEET_FONTS, WIDTHS,
    시트_다듬기, 시트_채우기, 시트_행열,
)
from ...edit import CHOICE_FIELDS
from ...export import build_sheet_xlsx, col_letter
from ...recruit import STAGES
from ...sheet import SheetError, 계산 as 수식계산
from ...timeutil import now_kst

from ..state import audit, auth, boards, mailing, recruit, registry, store
from ..columns import MAIL_COLUMN, 덧값, 머리글, 모든열, 수식열채우기, 열폭
from ..layout import _page, _알림, _없는주소, _정적JS, 함수설명자료
from ..router import 라우트


# ---------------------------------------------------------------------------
# 대시보드가 셀 데이터 — 화면들이 쓰는 것과 **같은 것**을 넘긴다.
# 새 질의 계층을 만들면 화면 숫자와 대시보드 숫자가 어긋난다.
# ---------------------------------------------------------------------------
def 대시보드_행() -> F.Rows:
    """지원자(인재 Pool 전체) / 채용(채용 시작한 사람) 두 묶음."""
    진행맵 = recruit.all()
    부서명 = {d["id"]: d["이름"] for d in auth.departments()}
    과제명 = {p["id"]: p["이름"] for p in auth.projects()}
    사용자값 = store.custom_map()
    관리값 = store.meta_map()
    시작한사람 = recruit.started()
    상위매칭 = store.top_matches()
    끝낸검토 = store.review_done_map()
    보낸것맵 = mailing.sent_summary()
    # **쓸 수 있다고 알려 준 열은 모든 줄에 있어야 한다.** 추가한 열은
    # `custom_map()` 이 값을 적은 사람만 돌려주므로, 안 적은 사람의 줄에는
    # 그 열쇠가 아예 없었다. 그러면 수식이 "이 사람은 비어 있다" 를
    # "그런 열은 없다" 로 말하게 되고 (`expr` 이 모르는 이름을 막는다),
    # 화면에는 빈칸 대신 `?` 가 뜬다. 자동완성·저장 검사가 쓰는 그 목록으로
    # 빈칸을 먼저 깔아 두면 목록과 줄이 어긋날 수가 없다.
    빈칸 = {c: "" for c in 대시보드_열()}
    수식들 = store.field_formulas()
    채용중열 = store.field("채용중") is not None

    지원자행, 채용행 = [], []
    for rec in store.list_all():
        cid = rec.지원자_ID
        행 = {**빈칸, **rec.to_row(registry)}
        행["지원자_ID"] = cid
        # 표·엑셀과 같은 규칙: 확인한 사유는 세지 않는다.
        행["검토_사유"] = review.display(rec.검토_사유, 끝낸검토.get(cid, set()))
        행.update(관리값.get(cid, {}))
        행.update(사용자값.get(cid, {}))
        p = 진행맵.get(cid)
        행["부서"] = 부서명.get(p.부서_id, "") if p else ""
        행["과제"] = 과제명.get(p.project_id, "") if p else ""
        행["최종상태"] = p.최종상태 if p else "미시작"
        행["채용_비고"] = p.채용_비고 if p else ""
        for 단계 in STAGES:
            행[단계] = (p.단계상태.get(단계, "") if p else "")
        행[MAIL_COLUMN] = 보낸것맵.get(cid, "")
        # 엑셀 모양 수식에서 «채용 중인 사람만» 을 거는 열 (`COUNTIFS(채용중, "Y", …)`)
        덧값(행, cid, 시작한사람, 상위매칭, 채용중열)
        수식열채우기(행, 사용자값.get(cid, {}), 수식들, 빈칸)
        지원자행.append(행)
        if cid in 시작한사람:
            채용행.append(행)
    return F.Rows(지원자=지원자행, 채용=채용행)


#: 미리보기용 줄 묶음 캐시. 수식 칸에 글자를 칠 때마다 미리보기를 부르는데,
#: 그때마다 전원의 줄을 새로 만들면 사람이 수천 명일 때 입력이 버벅인다.
#: 몇 초 묵은 값이어도 **미리보기**에는 충분하다 (보기 화면은 늘 새로 만든다).
_행캐시: dict = {"때": 0.0, "값": None}
_행캐시_잠금 = threading.Lock()
행캐시_초 = 5.0


def 대시보드_행_잠깐() -> F.Rows:
    """`대시보드_행()` 을 몇 초 동안 재사용한다. 미리보기·시트 재계산 전용."""
    import time

    with _행캐시_잠금:
        if _행캐시["값"] is not None and time.monotonic() - _행캐시["때"] < 행캐시_초:
            return _행캐시["값"]
    값 = 대시보드_행()
    with _행캐시_잠금:
        _행캐시.update(때=time.monotonic(), 값=값)
    return 값


def 대시보드_열() -> set[str]:
    """수식·문장 틀에서 쓸 수 있는 열 이름 전부."""
    return 모든열()


def 대시보드_축() -> dict[str, list[str]]:
    """축으로 쓸 수 있는 값 목록. 조직·설정이 바뀌면 표가 알아서 따라 늘어난다."""
    return {
        "부서": [d["이름"] for d in auth.departments()],
        "과제": [p["이름"] for p in auth.projects()],
        "단계": list(STAGES),
        "최종상태": [s for s in recruit.statuses() if s],
        "등록년도": store.years(),
        "현재_신분": [v for v in CHOICE_FIELDS.get("현재_신분", []) if v],
    }


# ---------------------------------------------------------------------------
# 대시보드 화면
# ---------------------------------------------------------------------------
#: 처음 만들 때 넣어 주는 프로필 양식. 빈 화면에서 시작하면 아무도 안 만든다.
#: 새 프로필 블록의 기본 줄. **`=` 수식**으로 적는다 — 목록·시트와 같은 문법이라
#: 하나만 배우면 된다. 예전 `{열}` 틀도 그대로 돌아간다 (저장된 블록 호환).
#: 값이 비면 줄째 사라지는 건 같다: 수식 결과가 빈칸이면 그 줄은 안 나온다.
기본_프로필틀 = [
    ["학력", '=TEXTJOIN(" ", TRUE, 박사_학교, 박사_전공 '
            '& WRAP(PERIOD(박사_시작, 박사_졸업), "(", ")"), 박사_학위상태)'],
    ["", '=TEXTJOIN(" ", TRUE, 석사_학교, 석사_전공 '
         '& WRAP(PERIOD(석사_시작, 석사_졸업), "(", ")"))'],
    ["현재", '=TEXTJOIN(" ", TRUE, 현재_소속, 현재_소속_상세)'],
    ["경력", '=IF(ISBLANK(경력_회사), "", 경력_회사 & WRAP(직책, "/") '
            '& WRAP(PERIOD(경력_시작, 경력_종료), "(", ")"))'],
    ["실적", '=IF(COUNTA(저널_수, 학회_수, 특허_등록_국내_수, 특허_등록_해외_수)=0, "", '
            '"저널 " & N(저널_수) & "편(주저자 " & N(저널_주저자_수) & ") · 학회 " '
            '& N(학회_수) & "편(주저자 " & N(학회_주저자_수) & ") · 특허 등록 국내 " '
            '& N(특허_등록_국내_수) & "/해외 " & N(특허_등록_해외_수))'],
    ["채용", '=TEXTJOIN(" ", TRUE, 부서, 과제, WRAP(최종상태, "— "))'],
    ["매칭", '=IF(ISBLANK(매칭_과제), "", 매칭_과제 & WRAP(매칭_점수, " (", "점)"))'],
]

#: 새 프로필 블록의 머리 (이름 줄)
기본_프로필머리 = '=한글_이름 & WRAP(현재_신분, " (", ")")'


def _프로필값(cid: str) -> dict[str, str]:
    """문장 틀에 넣을 한 사람의 값. 표에 보이는 것과 같은 값을 쓴다."""
    rec = store.get(cid)
    if rec is None:
        return {}
    # 목록 표와 **같은 목록**으로 빈칸을 깐다. 두 길이 다르면 미리보기와
    # 실제 표가 어긋난다 (`대시보드_행()` 의 같은 주석을 보라).
    행 = {c: "" for c in 대시보드_열()}
    행.update({k: str(v or "") for k, v in rec.to_row(registry).items()})
    행["지원자_ID"] = cid
    행["검토_사유"] = review.display(rec.검토_사유, store.review_done(cid))
    행.update(store.meta_map().get(cid, {}))
    행.update(store.custom_values(cid))
    p = recruit.get(cid)
    부서명 = {d["id"]: d["이름"] for d in auth.departments()}
    과제명 = {pr["id"]: pr["이름"] for pr in auth.projects()}
    행["부서"] = 부서명.get(p.부서_id, "")
    행["과제"] = 과제명.get(p.project_id, "")
    행["최종상태"] = p.최종상태
    행["채용_비고"] = p.채용_비고
    for 단계 in STAGES:
        행[단계] = p.단계상태.get(단계, "")
    행[MAIL_COLUMN] = mailing.sent_summary().get(cid, "")
    덧값(행, cid, recruit.started(), store.top_matches())
    수식열채우기(행, store.custom_values(cid))
    return 행


def _쉼표목록(글: str) -> list[str]:
    return [x.strip() for x in (글 or "").split(",") if x.strip()]


def _수식검사(수식: str, 아는열: set[str]) -> str:
    """수식이면 검사하고, 틀리면 사람이 읽을 오류를 돌려준다. 괜찮으면 빈 문자열.

    **저장 단계에서 막는 게 핵심이다.** 없는 열 이름이 그대로 저장되면 화면에는
    그냥 0 이 뜨고 아무도 틀린 줄 모른다.
    """
    수식 = (수식 or "").strip()
    if not 수식 or not expr.is_formula(수식):
        return ""
    try:
        # 그리는 것과 **같은 길**로 본다. 안 그러면 `=COUNT(지원자)/2` 가
        # 화면에서는 도는데 저장에서 막힌다.
        # 검사에는 몇 초 묵은 줄이면 충분하다 (값이 아니라 문법·열 이름을 본다).
        수식계산(수식, 대시보드_행_잠깐(), 아는열)
    except (F.FormulaError, expr.ExprError, SheetError, ValueError) as exc:
        return f"{수식} → {exc}"
    return ""


def _열너비설정(설정: dict, data: dict) -> None:
    """열 너비 칸에서 온 값. {열 이름: px}

    이름과 값을 **짝지어** 보내므로 열 개수가 바뀌어도 안 밀린다.
    빈 칸·0·글자는 «안 정함» 으로 떨어뜨린다 (담아 두면 표가 사라진다).
    """
    if "colwname" not in data:
        return                              # 이 폼이 열 너비를 안 담고 있었다
    이름들 = data.get("colwname") or []
    값들 = (data.get("colw") or []) + [""] * len(이름들)
    설정["열너비"] = {이름: str(_px(값)) for 이름, 값 in zip(이름들, 값들)
                   if _px(값)}


def _표모양설정(설정: dict, data: dict) -> None:
    """표 모양 고르개에서 온 값. 안 보낸 칸은 건드리지 않는다."""
    테두리 = (data.get("border") or [""])[0]
    if 테두리 in ("가로줄", "격자", "없음"):
        설정["테두리"] = 테두리
    폭 = (data.get("tablewidth") or [""])[0]
    if 폭 in ("창에 맞춤", "내용에 맞춤"):
        설정["표너비"] = 폭
    _열너비설정(설정, data)
    if "border" in data:            # 이 폼이 표 모양을 담고 있었다는 뜻
        설정["줄무늬"] = bool(data.get("zebra"))
        설정["촘촘히"] = bool(data.get("tight"))
        # '기본색' 을 켜 두면 색을 저장하지 않는다 — 화면 테마를 따라가게.
        색 = (data.get("headbg") or [""])[0].strip()
        설정["머리배경"] = "" if data.get("headbgoff") else 색


def _블록설정(b, data: dict) -> tuple[dict, str]:
    """폼에서 온 값을 블록 설정으로. (설정, 오류메시지)"""
    아는열 = 대시보드_열()
    설정 = dict(b.설정)
    _표모양설정(설정, data)
    형식 = (data.get("format") or [""])[0]
    if 형식 in CELL_FORMATS:
        설정["형식"] = 형식

    if b.종류 == "글":
        설정["글"] = (data.get("text") or [""])[0]
        return 설정, ""

    if b.종류 == "숫자":
        수식 = (data.get("formula") or [""])[0].strip()
        오류 = _수식검사(수식, 아는열)
        if 오류:
            return 설정, 오류
        설정["수식"] = 수식
        return 설정, ""

    if b.종류 == "축표":
        설정["행축"] = (data.get("rowaxis") or [""])[0]
        설정["열축"] = (data.get("colaxis") or [""])[0]
        설정["행"] = _쉼표목록((data.get("rows") or [""])[0])
        설정["열"] = _쉼표목록((data.get("cols") or [""])[0])
        칸수식 = (data.get("cellformula") or [""])[0].strip()
        # {행}{열} 을 실제 값으로 한 번 바꿔 놓고 검사한다. 그대로 검사하면
        # '{행}' 이 열 이름인 줄 알고 엉뚱한 오류가 난다.
        축값 = 대시보드_축()
        본보기 = (칸수식.replace("{행}", (축값.get(설정["행축"]) or 설정["행"] or [""])[0])
                       .replace("{열}", (축값.get(설정["열축"]) or 설정["열"] or [""])[0]))
        오류 = _수식검사(본보기, 아는열)
        if 오류:
            return 설정, 오류.replace(본보기, 칸수식)
        설정["칸수식"] = 칸수식
        return 설정, ""

    if b.종류 == "목록":
        설정["목록대상"] = ((data.get("listtarget") or ["지원자"])[0]
                        if (data.get("listtarget") or ["지원자"])[0] in ("지원자", "채용")
                        else "지원자")
        설정["목록조건"] = (data.get("listwhere") or [""])[0].strip()
        설정["목록정렬"] = (data.get("listsort") or [""])[0].strip()
        설정["목록내림차순"] = bool(data.get("listdesc"))
        try:
            설정["목록최대"] = max(0, int((data.get("listmax") or ["0"])[0] or 0))
        except ValueError:
            설정["목록최대"] = 0
        머리들 = data.get("colhead") or []
        식들 = data.get("colformula") or []
        폭들 = (data.get("colwidth") or []) + [""] * len(식들)
        열 = [[머리.strip(), 식.strip(), 폭.strip()]
             for 머리, 식, 폭 in zip(머리들, 식들, 폭들) if 식.strip()]
        if not 열:
            return 설정, "열이 하나도 없습니다. 머리글과 수식을 적으세요."
        # 문법부터 틀렸으면 저장을 막는다. 조용히 넘어가면 화면에서 ? 만 보인다.
        볼것 = [("행 고르기", 설정["목록조건"]), ("정렬", 설정["목록정렬"])]
        볼것 += [(머리 or 식, 식) for 머리, 식, _폭 in 열]
        for 자리, 식 in 볼것:
            if not expr.is_formula(식):
                continue
            try:
                expr.validate(식, 아는열)
            except expr.ExprError as exc:
                return 설정, f"'{자리}' 수식이 잘못됐습니다 — {exc}"
        설정["목록열"] = 열

        # 값에 따라 칠하기. 조건도 수식이라 저장 전에 검사한다.
        조건들 = data.get("cfwhen") or []
        어디들 = (data.get("cfwhere") or []) + [ROW_TARGET] * len(조건들)
        배경들 = (data.get("cfbg") or []) + [""] * len(조건들)
        # 색 고르개는 항상 값을 보내므로, '기본' 체크는 그 자리의 색을 지우는 뜻이다.
        # 체크박스는 켠 것만 오기 때문에 순서로 짝을 지을 수 없다 — 그래서 글자색은
        # 켠 규칙 수만큼만 받고, 나머지는 기본색으로 둔다.
        글자들 = (data.get("cffg") or []) + [""] * len(조건들)
        글자쓰나 = (data.get("cffgmode") or []) + ["기본"] * len(조건들)
        서식 = []
        for i, 조건 in enumerate(조건들):
            조건 = 조건.strip()
            if not 조건:
                continue
            if not expr.is_formula(조건):
                조건 = "=" + 조건
            try:
                expr.validate(조건, 아는열)
            except expr.ExprError as exc:
                return 설정, f"색칠 조건이 잘못됐습니다 — {exc}"
            서식.append({
                "조건": 조건, "대상": 어디들[i], "배경": 배경들[i],
                "글자": 글자들[i] if 글자쓰나[i] == "직접" else "",
            })
        설정["조건서식"] = 서식
        return 설정, ""

    if b.종류 == "프로필":
        대상 = (data.get("target") or [""])[0].strip() or "=LIST(지원자)"
        오류 = _수식검사(대상, 아는열)
        if 오류:
            return 설정, 오류
        설정["대상"] = 대상
        설정["머리"] = (data.get("head") or [""])[0]
        for 열쇠, 이름 in (("pmax", "최대"), ("popen", "펼침")):
            if 열쇠 in data:
                try:
                    설정[이름] = max(0, int((data.get(열쇠) or ["0"])[0] or 0))
                except ValueError:
                    pass
        라벨들 = data.get("label") or []
        틀들 = data.get("line") or []
        줄 = [[라벨.strip(), 틀.strip()]
             for 라벨, 틀 in zip(라벨들, 틀들) if 틀.strip()]
        # 수식이 문법부터 틀렸으면 **저장을 막는다.** 조용히 넘어가면 나중에
        # 화면에서 빈칸으로 나오는데, 값이 없는 건지 잘못 쓴 건지 알 수가 없다.
        for 라벨, 틀 in 줄 + [["머리", 설정["머리"]]]:
            if not expr.is_formula(틀):
                continue
            try:
                expr.parse(틀)
            except expr.ExprError as exc:
                자리 = f"'{라벨}' 줄" if 라벨 else "문장"
                return 설정, f"{자리}의 수식이 잘못됐습니다 — {exc}"
        모르는 = sorted({c for _l, 틀 in 줄 for c in P.columns(틀)
                       if c and c not in 아는열}
                      | {c for c in P.columns(설정["머리"]) if c and c not in 아는열})
        if 모르는:
            return 설정, ("표에 없는 열입니다: " + ", ".join(모르는)
                        + " — 표 항목 탭에 있는 이름을 그대로 쓰세요.")
        설정["줄"] = 줄
        return 설정, ""

    # 자유 표
    행 = _쉼표목록((data.get("rows") or [""])[0])
    열 = _쉼표목록((data.get("cols") or [""])[0])
    칸값 = data.get("cell") or []
    칸 = {}
    if 칸값 and len(칸값) == len(b.행이름) * len(b.열이름):
        # 폼은 옛 행·열 순서대로 왔다. 그 짝으로 읽고 새 행·열에 맞춰 남긴다.
        i = 0
        for r in b.행이름:
            for c in b.열이름:
                if 칸값[i].strip():
                    칸[_칸키(r, c)] = 칸값[i].strip()
                i += 1
    else:
        칸 = dict(b.칸)
    for 키, 수식 in 칸.items():
        오류 = _수식검사(수식, 아는열)
        if 오류:
            return 설정, f"[{키.replace(chr(9), ' / ')}] {오류}"
    설정["행"], 설정["열"], 설정["칸"] = 행, 열, 칸
    return 설정, ""


#: 블록 추가 버튼의 한 줄 설명 — «이럴 때 쓰세요».
BLOCK_HELP = {
    "목록": "한 사람이 한 줄. 열을 내가 정합니다 (이름·학력·논문 수…). 가장 많이 씁니다.",
    "축표": "행×열 교차표. 부서×단계 인원처럼, 축 값이 늘면 표가 알아서 늘어납니다.",
    "표": "행·열 이름을 직접 적고 칸마다 수식을 따로 적는 작은 표.",
    "숫자": "큰 숫자 하나 (채용 중 12명). 누르면 누구인지 보입니다.",
    "글": "제목·안내문 같은 그냥 글.",
    "프로필": "한 사람을 문장 틀로 한 장씩 (학력·경력·실적 요약).",
    "시트": "엑셀처럼 칸마다 자유롭게. 칸끼리 참조(=B2/B3), 서식·테두리·병합.",
}


def _예시블록(did: int) -> None:
    """새 대시보드에 바로 볼 수 있는 블록을 넣어 준다.

    빈 화면에서 시작하면 아무도 안 만든다. 예시가 곧 사용법이다.
    """
    boards.add_block(did, "숫자", 제목="채용 중",
                     설정={"수식": "=COUNT(채용)", "형식": "명"})
    boards.add_block(did, "축표", 제목="부서 × 단계",
                     설정={"행축": "부서", "열축": "단계",
                          "칸수식": '=COUNT(채용, 부서="{행}", {열}="합격")',
                          "형식": "그대로"})
    boards.add_block(did, "축표", 제목="등록년도 × 현재 신분",
                     설정={"행축": "등록년도", "열축": "현재_신분",
                          "칸수식": '=COUNT(지원자, 등록년도="{행}", 현재_신분="{열}")'})
    boards.add_block(did, "프로필", 제목="채용 중인 사람",
                     설정={"대상": "=LIST(채용)",
                          "머리": 기본_프로필머리,
                          "줄": 기본_프로필틀})


def _블록기록(b) -> str:
    """변경 이력에 남길 블록 모습 (제목 + 설정 JSON). 없으면 빈 글.

    이 글이 곧 되돌릴 거리다 — 잘못 고친 수식을 이력에서 보고 되살릴 수 있다.
    """
    if b is None:
        return ""
    return json.dumps({"제목": b.제목, "설정": b.설정}, ensure_ascii=False, sort_keys=True)


def _대시_지울수있나(me: User, d) -> bool:
    """대시보드를 지울(휴지통에 넣을·완전히 지울) 수 있나 — **만든 사람과 관리자만.**

    채용담당자라면 누구나 남의 대시보드를 지울 수 있었다. 만든 사람이 기록되지
    않은 옛 대시보드는 관리자만 지운다.
    """
    if me is None or d is None:
        return False
    return me.역할 == "관리자" or (bool(d.만든이) and d.만든이 == me.아이디)


def _dash_list_page(me: User, error: str = "", msg: str = "") -> bytes:
    보드 = boards.all()
    편집 = can(me, "대시보드_조회")
    rows = "".join(
        f"<tr><td><a href='/dash/view?id={d.id}'>{html.escape(d.이름)}</a></td>"
        f"<td class='muted' title='{html.escape(d.설명)}'>{html.escape(d.설명)}</td>"
        f"<td class='muted'>{len(boards.blocks(d.id))}개</td>"
        f"<td class='muted'>{html.escape(d.수정일시)}</td>"
        f"<td><a class='btn' href='/dash/view?id={d.id}'>보기</a> "
        + (f"<a class='btn sec' href='/dash/edit?id={d.id}'>편집</a> "
           "<form method='post' action='/dash/copy' style='display:inline'>"
           f"<input type='hidden' name='id' value='{d.id}'>"
           "<button class='sec'>복제</button></form> "
           + ("<form method='post' action='/dash/delete' style='display:inline'"
              " onsubmit=\"return confirm('이 대시보드를 휴지통으로 보냅니다. 휴지통에서 되살릴 수 있습니다.')\">"
              f"<input type='hidden' name='id' value='{d.id}'>"
              "<button class='danger'>삭제</button></form>"
              if _대시_지울수있나(me, d) else "")
           if 편집 else "")
        + "</td></tr>"
        for d in 보드
    ) or "<tr><td colspan='5' class='muted'>아직 만든 대시보드가 없습니다.</td></tr>"
    휴지통 = boards.trash()
    휴지통줄 = "".join(
        f"<tr><td>{html.escape(d.지운이름 or d.이름)}</td>"
        f"<td class='muted'>{html.escape(d.지운일시)} · {html.escape(d.지운이 or '-')}</td>"
        f"<td class='muted'>{html.escape(d.만든이 or '-')}</td><td>"
        "<form method='post' action='/dash/restore' style='display:inline'>"
        f"<input type='hidden' name='id' value='{d.id}'><button class='sec'>되살리기</button></form> "
        + ("<form method='post' action='/dash/purge' style='display:inline'"
           " onsubmit=\"return confirm('완전히 지웁니다. 되돌릴 수 없습니다.')\">"
           f"<input type='hidden' name='id' value='{d.id}'>"
           "<button class='danger ghost'>완전히 지우기</button></form>"
           if _대시_지울수있나(me, d) else "")
        + "</td></tr>"
        for d in 휴지통)
    휴지통칸 = (
        f"<div class='card'><details><summary><b>휴지통</b> <span class='muted'>{len(휴지통)}개</span>"
        "</summary><div class='scroll' style='margin-top:8px'><table><tr><th>이름</th>"
        "<th>지운 때 · 지운 사람</th><th>만든 사람</th><th></th></tr>"
        + 휴지통줄 + "</table></div></details></div>"
        if 휴지통 and 편집 else "")

    알림 = _알림(msg=msg)
    오류 = _알림(err=error)
    return _page(
        "대시보드",
        알림
        + "<div class='card'><h2>대시보드 만들기</h2>" + 오류
        + "<form method='post' action='/dash/add' style='display:flex;gap:8px;flex-wrap:wrap'>"
        "<input type='text' name='name' placeholder='이름 (예: 주간 채용 현황판)'"
        " required style='width:260px'>"
        "<input type='text' name='desc' placeholder='설명 (선택)' style='width:320px'>"
        "<label class='muted'><input type='checkbox' name='sample' value='1' checked>"
        " 예시 블록 넣기</label>"
        "<button type='submit'>만들기</button></form>"
        "<p class='muted'>만든 뒤 <b>편집</b> 에서 블록을 쌓습니다. "
        "예시 블록을 켜 두면 바로 볼 수 있는 것부터 들어갑니다.</p></div>"
        + f"<div class='card'><h2>대시보드 {len(보드)}개</h2><div class='scroll'>"
        "<table data-name='대시보드'><tr><th>이름</th><th>설명</th><th>블록</th>"
        "<th>수정</th><th></th></tr>" + rows + "</table></div>"
        "<p class='muted'>삭제는 <b>만든 사람과 관리자만</b> 할 수 있고, 지운 것은 "
        "휴지통으로 가서 되살릴 수 있습니다.</p></div>" + 휴지통칸,
        me=me,
    )


#: 시트 열 너비를 안 정했을 때 (px). 편집기 JS 의 `기본열너비` 와 같아야 한다.
시트_기본열너비 = 90
#: 행 번호(1·2·3) 칸 너비
시트_머리열너비 = 44


def _시트표(b, rows, 아는열, *, 편집: bool = False) -> tuple[str, list[str]]:
    """시트 격자를 그린다. (표 HTML, 오류들)

    보기 화면과 편집 화면이 **같은 함수**를 쓴다. 둘이 갈라지면 "편집에서는
    이랬는데 저장하니 다르다" 가 된다. 편집일 때는 칸마다 주소를 달아 두어
    도구막대가 짚을 수 있게 한다.
    """
    # 보기 화면은 **보일 범위**만 그린다. 편집은 늘 전체 (범위 밖 칸도 고쳐야 한다).
    결과 = render_sheet(b, rows, 아는열, 잘라보기=not 편집)
    문맥 = _드릴문맥.get()
    칸읽기 = S.칸읽기(결과.계산값, b.시트칸, b.시트행수, b.시트열수)

    def _원래주소(주소글: str) -> str:
        """잘라 그린 칸의 주소(A1 부터) → 시트의 원래 주소."""
        r, c = S.자리(주소글)
        return S.주소(결과.시작행 + r, 결과.시작열 + c)

    # 편집일 때는 머리글에 자리를 달아 둔다 — 끌어서 너비·높이를 바꾼다.
    # 잘라 그렸으면 머리글은 **원래 자리**로 적는다 (C3:F9 면 C·D·E·F, 3·4…).
    머리 = "".join(
        f"<th data-col='{col_letter(c)}'>{col_letter(결과.시작열 + c)}</th>"
        for c in range(결과.열수)
    )
    줄들 = []
    for r, 줄 in enumerate(결과.행):
        높이 = 결과.행높이.get(str(r + 1))
        칸들 = []
        for 주소글, 값, 스타일, 가로, 세로 in 줄:
            속성 = f" style='{html.escape(스타일)}'" if 스타일 else ""
            if 가로 > 1:
                속성 += f" colspan='{가로}'"
            if 세로 > 1:
                속성 += f" rowspan='{세로}'"
            if 편집:
                속성 += f" data-cell='{주소글}' tabindex='0'"
            속 = "<br>".join(html.escape(x) for x in str(값).split("\n"))
            if not 편집 and 문맥 is not None:
                # 보기 화면 — 사람을 세는 칸은 눌러서 누구인지, 사람 이름은 상세로.
                원주소 = _원래주소(주소글)
                주소 = _드릴주소(b, (결과.칸서식.get(주소글) or {}).get("글", ""),
                             칸=원주소, 값찾기=칸읽기)
                이름 = _이름칸(str(값))
                if 이름:
                    속 = 이름
                elif 주소 and str(값) not in ("", "?"):
                    속 = (f"<a class='drill' href='{주소}' title='누구인지 보기'>"
                         f"{html.escape(str(값))}</a>")
            칸들.append(f"<td{속성}>{속}</td>")
        # 보기 화면에는 A·B·1·2 머리글을 **아예 안 그린다** — 표만 보인다.
        # (CSS 로 가리면 열 너비도 같이 사라져 칸이 다 좁아졌다.) 머리글이
        # 있으면 표 가장자리 테두리가 머리글의 연한 선에 져서 지워지기도 했다.
        맨몸 = not 편집
        줄스타일 = f" style='height:{html.escape(높이)}px'" if 높이 else ""
        번호 = 결과.시작행 + r + 1
        줄머리 = "" if 맨몸 else (
            f"<th data-row='{r + 1}' style='height:{html.escape(높이)}px'>{번호}</th>"
            if 높이 else f"<th data-row='{r + 1}'>{번호}</th>")
        줄들.append(f"<tr{줄스타일}>{줄머리}{''.join(칸들)}</tr>")
    # 열 너비는 <colgroup> 으로 **모든 열에** 건다 (안 정한 열은 기본 너비).
    # 표 폭은 그 합이다. 예전에는 표 폭이 «알아서» 라 브라우저가 열들을 화면
    # 폭에 맞춰 눌렀고, 칸에 최소 90px 이 걸려 있어서 — 끌어도 좁아지지 않았고,
    # 열이 많아 화면을 채우면 넓어지지도 않았다. 넘치면 가로로 스크롤한다.
    맨몸 = not 편집
    폭들 = [_px(결과.열너비.get(col_letter(c))) or 시트_기본열너비
           for c in range(결과.열수)]
    열묶음 = ("" if 맨몸 else f"<col style='width:{시트_머리열너비}px'>") + "".join(
        f"<col style='width:{w}px'>" for w in 폭들)
    표폭 = sum(폭들) + (0 if 맨몸 else 시트_머리열너비)
    # `data-name` 을 안 붙인다. 그걸 붙이면 표 위에 «찾기 · 엑셀 내려받기» 막대가
    # 저절로 달라붙는데, 그 내려받기는 화면 글자를 TSV 로 긁어 만드는 길이라
    # 색도 병합도 안 실린다. 시트에는 서버가 만드는 제 내려받기가 따로 있다.
    반 = "sheet" + (" editing" if 편집 else (" plain" if b.시트격자숨김 else ""))
    머리줄 = "" if 맨몸 else f"<tr><th class='corner'></th>{머리}</tr>"
    표 = (f"<div class='scroll'><table class='{반}' style='width:{표폭}px'>"
         f"<colgroup>{열묶음}</colgroup>"
         f"{머리줄}{''.join(줄들)}</table></div>")
    return 표, 결과.오류


#: 시트 편집기. 격자를 **JSON 한 덩어리**로 주고받는다 — 칸마다 폼 입력을
#: 만들면 10x6 만 해도 숨은 칸이 수백 개가 되고, 선택·병합은 폼으로는 못 한다.
#:
#: 서버는 이 JSON 을 믿지 않는다 (`dashboards.시트_다듬기` 가 다시 거른다).
#:
#: **서식은 브라우저가 바로 칠하고, 값이 바뀌는 일만 서버에 다시 묻는다.**
#: 색 한 번 누를 때마다 화면이 새로 뜨면 못 쓰겠고, 그렇다고 수식까지 여기서
#: 계산하면 계산기가 둘이 되어 언젠가 둘의 답이 갈린다.
_SHEET_JS = _정적JS("sheet_editor.js")


def _블록그리기(b, rows, 축값, 아는열) -> str:
    """블록 하나를 보기 화면용 HTML 로."""
    if b.종류 == "시트":
        표, 오류 = _시트표(b, rows, 아는열)
        경고 = "".join(f"<p class='flag'>{html.escape(x)}</p>" for x in 오류)
        받기 = (f"<a class='btn sec' href='/dash/sheet.xlsx?block={b.id}'>"
              "엑셀 내려받기</a>")
        return (f"<div class='card'><h2>{html.escape(b.제목)}</h2>{경고}"
                f"<p>{받기} <span class='muted'>복사(Ctrl+C)는 값만 옮깁니다. "
                "색·굵기·병합까지 그대로 받으려면 엑셀로 받으세요.</span></p>"
                f"{표}</div>")

    if b.종류 == "글":
        본문 = "<br>".join(html.escape(x) for x in (b.글 or "").splitlines())
        return (f"<div class='card'><h2>{html.escape(b.제목)}</h2>{본문}</div>"
                if (b.제목 or 본문) else "")

    if b.종류 == "숫자":
        try:
            글, 값 = 수식계산(b.수식, rows, 아는열)
            보임 = format_cell(글, 값, b.설정.get("형식") or "그대로")
            # 수식은 보는 사람에게 필요 없다. 궁금하면 마우스를 올리면 된다.
            아래 = ""
        except (F.FormulaError, expr.ExprError, SheetError, ValueError) as exc:
            보임, 아래 = "?", f"<div class='flag'>{html.escape(str(exc))}</div>"
        주소 = _드릴주소(b, b.수식)
        # 이름이 나오면(LIST) 이름마다 상세로, 아니면 세는 숫자는 누구인지로.
        숫자 = _이름칸(보임) or (
            f"<a class='drill' href='{주소}' title='누구인지 보기 · {html.escape(b.수식)}'>"
            f"{html.escape(보임)}</a>" if 주소 else html.escape(보임))
        return (
            f"<div class='card'><h2>{html.escape(b.제목)}</h2>"
            f"<div style='font-size:38px;font-weight:800;line-height:1.2'"
            f" title='{html.escape(b.수식)}'>{숫자}</div>"
            f"{아래}</div>"
        )

    if b.종류 == "목록":
        결과 = render_list(b, rows, 아는열)
        경고 = "".join(f"<p class='flag'>{html.escape(x)}</p>" for x in 결과.오류)

        def 폭스타일(i: int) -> str:
            """정한 너비가 있으면 그 폭으로 못박는다. 없으면 열 이름으로 짐작."""
            정한것 = 결과.폭[i] if i < len(결과.폭) else ""
            if 정한것:
                return f" style='width:{html.escape(정한것)}px'"
            return ""

        def 폭클래스(i: int) -> str:
            if i < len(결과.폭) and 결과.폭[i]:
                return ""                       # 직접 정했으면 짐작하지 않는다
            return 열폭(결과.머리[i] if i < len(결과.머리) else "")

        # 편집 화면 미리보기에서 머리글 경계를 끌어 너비를 바꾼다 (`col_resize.js`).
        # 열쇠는 **설정의 몇 번째 열**인가 — 수식이 빈 줄은 표에 안 나오므로 건너뛴다.
        원번호 = [n for n, (_h, 식, _w) in enumerate(b.목록열) if str(식).strip()]
        머리 = "".join(
            f"<th class='{폭클래스(i)}'{폭스타일(i)}"
            f" data-wkey='{원번호[i] if i < len(원번호) else i}'>{머리글(c)}</th>"
            for i, c in enumerate(결과.머리)
        )

        def 칸(줄번호: int, i: int, v: str) -> str:
            # 줄바꿈(CHAR(10))을 일부러 넣은 칸은 **줄을 바꿔서** 보여준다.
            # 다른 칸은 그대로 한 줄로 잘린다 — 줄 높이가 들쭉날쭉해지면 표를
            # 훑기 어려우니, 바꾸는 건 그러라고 적은 칸뿐이다.
            cls = 폭클래스(i) + (" multi" if "\n" in v else "")
            속 = ("<br>".join(html.escape(줄) for 줄 in v.split("\n"))
                 if "\n" in v else html.escape(v))
            # 그 줄 사람의 이름이 **들어 있으면** 그 이름에 상세 링크 (동명이인이어도
            # 줄의 사람이 분명하다). `=한글_이름&" "&생년월일` 처럼 붙인 칸도 된다.
            이름 = ""
            if _드릴문맥.get() and 줄번호 < len(결과.ids) and 결과.ids[줄번호]:
                이름 = next((n for n in sorted(결과.이름들[줄번호], key=len, reverse=True)
                           if n and n in v), "")
            if 이름:
                앞, _, 뒤 = v.partition(이름)
                링크 = (f"<a class='person' href='{_사람주소(결과.ids[줄번호])}'"
                      f" title='상세 보기'>{html.escape(이름)}</a>")
                감싸 = lambda x: "<br>".join(html.escape(줄) for 줄 in x.split("\n"))
                속 = 감싸(앞) + 링크 + 감싸(뒤)
            elif "\n" not in v:
                속 = _이름칸(v) or 속
            # 조건서식. 칸 규칙이 줄 규칙을 이긴다 — 더 좁게 가리킨 쪽이 이긴다.
            #
            # 줄 색도 **칸마다** 칠한다. <tr> 에 걸고 물려받게 하면 얼룩말 무늬나
            # hover 가 덮어써서 어떤 줄은 칠해지고 어떤 줄은 안 칠해진다.
            칸색 = ""
            if 줄번호 < len(결과.칸색) and i < len(결과.칸색[줄번호]):
                칸색 = 결과.칸색[줄번호][i]
            줄색 = 결과.행색[줄번호] if 줄번호 < len(결과.행색) else ""
            색 = 칸색 or 줄색
            폭 = 폭스타일(i)
            안쪽 = ";".join(x for x in (색, 폭[8:-1] if 폭 else "") if x)
            스타일 = f" style='{안쪽}'" if 안쪽 else ""
            칠함 = " painted" if 색 else ""
            return (f"<td class='{(cls + 칠함).strip()}'{스타일}"
                    f" title='{html.escape(v)}'>{속}</td>")

        몸 = "".join(
            "<tr>" + "".join(칸(n, i, v) for i, v in enumerate(칸들)) + "</tr>"
            for n, 칸들 in enumerate(결과.행)
        ) or (f"<tr><td colspan='{max(1, len(결과.머리))}' class='muted'>"
              "조건에 맞는 사람이 없습니다.</td></tr>")
        센것 = (f"{len(결과.행)}줄"
              + (f" <span class='muted'>/ 전체 {결과.전체}</span>"
                 if 결과.전체 != len(결과.행) else ""))
        return (
            f"<div class='card'><h2>{html.escape(b.제목)} "
            f"<span class='muted'>{센것}</span></h2>{경고}"
            f"<div class='scroll'><table {_표모양(b, 결과.폭)}"
            f" data-wblock='{b.id}' data-name='{html.escape(b.제목 or '목록')}'>"
            f"<tr>{머리}</tr>{몸}</table></div></div>"
        )

    if b.종류 == "프로필":
        # 값은 **이미 만든 줄**에서 찾는다. 사람마다 `_프로필값` 을 부르면 그때마다
        # 표 전체(관리정보·매칭·메일 이력)를 다시 읽어서 사람 수의 제곱으로 느려졌다.
        행맵 = {r.get("지원자_ID"): r for r in rows.지원자}
        결과 = render_profile(b, rows, lambda cid: 행맵.get(cid) or {}, 아는열)
        경고 = "".join(f"<p class='flag'>{html.escape(x)}</p>" for x in 결과.오류)
        라벨폭 = _px(b.열너비.get("", "")) or 80
        펼침 = b.프로필펼침
        카드 = []
        for n, (머리, 줄들) in enumerate(결과.사람):
            줄 = "".join(
                f"<tr><th style='width:{라벨폭}px'>{html.escape(라벨)}</th>"
                f"<td style='white-space:pre-wrap;max-width:none'>{html.escape(값)}</td></tr>"
                for 라벨, 값 in 줄들
            )
            # 사람마다 접을 수 있다. 처음 몇 명만 펼쳐 두고 나머지는 이름 한 줄 —
            # 200명이 전부 펼쳐지면 화면이 50장 길이가 된다.
            상세 = (f" <a class='person plink' href='{_사람주소(결과.ids[n])}'"
                  " onclick='event.stopPropagation()'>상세 ↗</a>"
                  if _드릴문맥.get() and n < len(결과.ids) and 결과.ids[n] else "")
            카드.append(
                f"<details class='pcard'{' open' if n < 펼침 else ''}>"
                f"<summary>{html.escape(머리)}{상세}</summary>"
                f"<table {_표모양(b)}>{줄}</table></details>"
            )
        몸 = "".join(카드) or "<p class='muted'>조건에 맞는 사람이 없습니다.</p>"
        센것 = (f"{len(결과.사람)}명"
              + (f" <span class='muted'>/ 전체 {결과.전체}명 (최대 {b.프로필최대}명)</span>"
                 if b.프로필최대 and 결과.전체 > len(결과.사람) else ""))
        도구 = (
            "<p class='bar pbar'><input type='search' placeholder='이름·내용으로 찾기'"
            " oninput='pcardFind(this)' style='width:240px'>"
            "<button type='button' class='sec' onclick='pcardAll(this,true)'>모두 펼치기</button>"
            "<button type='button' class='sec' onclick='pcardAll(this,false)'>모두 접기</button>"
            "</p>" if len(결과.사람) > 1 else "")
        return (f"<div class='card'><h2>{html.escape(b.제목)} "
                f"<span class='muted'>{센것}</span></h2>{경고}{도구}{몸}</div>")

    결과 = render_table(b, rows, 축값, 아는열)
    경고 = "".join(f"<p class='flag'>{html.escape(x)}</p>" for x in 결과.오류)
    # 맨 왼쪽 줄 이름 칸은 이름이 없으므로 "" 을 열쇠로 쓴다.
    폭맵 = b.열너비
    이름들 = ["", *결과.머리]
    너비들 = [폭맵.get(c, "") for c in 이름들]

    def 폭(i: int) -> str:
        n = _px(너비들[i]) if i < len(너비들) else 0
        return f" style='width:{n}px'" if n else ""

    머리 = f"<th{폭(0)} data-wkey=''></th>" + "".join(
        f"<th{폭(i + 1)} data-wkey='{html.escape(c)}'>{html.escape(c)}</th>"
        for i, c in enumerate(결과.머리))
    def 칸수식(r: str, i: int) -> str:
        c = 결과.머리[i] if i < len(결과.머리) else ""
        if b.종류 == "축표":
            return b.칸수식.replace("{행}", r).replace("{열}", c)
        return b.칸.get(_칸키(r, c), "")

    def 칸속(r: str, i: int, v: str) -> str:
        # 칸이 사람을 세는 집계 하나면 눌러서 **누구인지** 본다.
        주소 = _드릴주소(b, 칸수식(r, i), r, 결과.머리[i] if i < len(결과.머리) else "")
        이름 = _이름칸(v)
        if 이름:
            return 이름
        if 주소 and v not in ("", "?"):
            return f"<a class='drill' href='{주소}' title='누구인지 보기'>{html.escape(v)}</a>"
        return html.escape(v)

    몸 = "".join(
        f"<tr><th style='text-align:left{';width:' + str(_px(너비들[0])) + 'px' if _px(너비들[0]) else ''}'>"
        f"{html.escape(r)}</th>"
        + "".join(f"<td{폭(i + 1)}>{칸속(r, i, v)}</td>"
                  for i, v in enumerate(칸들)) + "</tr>"
        for r, 칸들 in 결과.행
    ) or (f"<tr><td colspan='{len(결과.머리) + 1}' class='muted'>"
          f"{_빈축까닭(b, 축값)}</td></tr>")
    return (
        f"<div class='card'><h2>{html.escape(b.제목)}</h2>{경고}"
        f"<div class='scroll'><table {_표모양(b, 너비들)}"
        f" data-wblock='{b.id}' data-name='{html.escape(b.제목 or '표')}'>"
        f"<tr>{머리}</tr>{몸}</table></div></div>"
    )


#: 누르면 «누구인지» 를 보여 줄 수 있는 집계. 사람 묶음을 고르는 것들이다.
_드릴함수 = ("COUNT", "PCT", "LIST", "AVG", "SUM", "MIN", "MAX")


def _드릴대상(수식: str, 값찾기=None):
    """수식이 **통째로 집계 하나**면 그 Formula, 아니면 None.

    `=COUNT(채용, 부서="A")` 는 누구를 셌는지가 분명하다. `=COUNT(…)/COUNT(…)`
    처럼 섞은 식은 어느 쪽 사람을 보여줄지 정할 수 없으므로 누를 수 없게 둔다.
    """
    from ...sheet import _칸값넣기, 고정떼기

    글 = (수식 or "").strip()
    if not expr.is_formula(글):
        return None
    # 계산기와 **같은 잣대**로 «사람을 세는 집계» 인지 본다. 모양만 보면
    # `=SUM(1,2)` 도 집계(SUM)로 읽혀 누를 수 있게 됐다.
    부름 = F._CALL_RE.match(고정떼기(글))
    아는열 = (_드릴문맥.get() or {}).get("열") or 대시보드_열()
    try:
        f = (F.parse(_칸값넣기(고정떼기(글), 값찾기))
             if 부름 and S._집계인가(부름.group(1).upper(),
                                   F._split_args(부름.group(2)), 아는열) else None)
    except (F.FormulaError, expr.ExprError, SheetError, ValueError):
        f = None
    if f is not None:
        return f if f.함수 in _드릴함수 else None
    # 엑셀 모양 `=COUNTIFS(부서, "A", 과제, "B")` — 센 사람이 분명하다.
    return 글 if S.조건집계짜임(글) is not None else None


def _드릴주소(b, 수식: str, 행: str = "", 열: str = "", 칸: str = "",
          값찾기=None) -> str:
    """보기 화면에서만 주소를 만든다 (편집 화면의 결과 미리보기에서는 안 누른다).

    시트는 `칸`(원래 주소)을 넘긴다 — 조건 값에 다른 칸을 썼으면(`부서=A3`)
    `값찾기` 로 그 칸 값을 풀어 본다.
    """
    문맥 = _드릴문맥.get()
    if not 문맥 or _드릴대상(수식, 값찾기) is None:
        return ""
    쿼리 = {"id": b.id, "r": 행, "c": 열, "cell": 칸}
    for 키, 열이름, _축 in 대시거르개_목록:
        if 열이름 in 문맥["거르개"]:
            쿼리[키] = 문맥["거르개"][열이름]
    return "/dash/who?" + urllib.parse.urlencode(
        {k: v for k, v in 쿼리.items() if v != ""})


def _dash_who_page(me: User, params: dict) -> bytes:
    """숫자·칸을 눌렀을 때 — 그 칸이 센 사람들."""
    try:
        b = boards.block(int((params.get("id") or ["0"])[0]))
    except ValueError:
        b = None
    if b is None:
        return _page("없음", "<div class='card'>블록을 찾을 수 없습니다.</div>", me=me)
    행 = (params.get("r") or [""])[0]
    열 = (params.get("c") or [""])[0]
    거르개 = _대시거르개(params)
    rows, _축 = _거른줄(대시보드_행(), 대시보드_축(), 거르개)
    값찾기 = None
    if b.종류 == "숫자":
        수식 = b.수식
    elif b.종류 == "축표":
        수식 = b.칸수식.replace("{행}", 행).replace("{열}", 열)
    elif b.종류 == "시트":
        칸주소 = (params.get("cell") or [""])[0].strip().upper()
        수식 = (b.시트칸.get(칸주소) or {}).get("글", "")
        # 조건에 다른 칸을 썼을 수 있다 — 보기 화면과 같은 줄로 시트를 계산해 둔다.
        칸값, _오류 = S.값들(b.시트칸, rows, 대시보드_열(),
                         행수=b.시트행수, 열수=b.시트열수)
        값찾기 = S.칸읽기(칸값, b.시트칸, b.시트행수, b.시트열수)
        행 = 칸주소
    else:
        수식 = b.칸.get(_칸키(행, 열), "")
    f = _드릴대상(수식, 값찾기)
    if f is None:
        몸 = "<p class='muted'>이 칸은 사람을 세는 집계 하나가 아니라서 누구인지 보여줄 수 없습니다.</p>"
        사람 = []
    else:
        못찾음 = ""
        보일열 = ["한글_이름", "현재_소속", "부서", "과제", "최종상태"]
        if isinstance(f, str):
            찾은, 까닭 = S.조건줄_까닭(f, rows, set(대시보드_열()), 값찾기,
                                   현재칸=행 if b.종류 == "시트" else None)
            if 찾은 is None:
                # 조용히 «0명» 을 띄우면 정말 0명인지 못 찾은 건지 모른다.
                못찾음 = ("<p class='flag'>명단을 계산하지 못했습니다 — "
                       f"{html.escape(까닭)}</p>")
            사람 = 찾은 or []
            나무, _짝 = S.조건집계짜임(f)
            if 나무.이름 in ("SUMIF", "SUMIFS", "AVERAGEIF", "AVERAGEIFS"):
                값열 = 나무.인자[0] if 나무.이름.endswith("S") else (
                    나무.인자[2] if len(나무.인자) > 2 else 나무.인자[0])
                if isinstance(값열, expr.Col) and 값열.이름 not in 보일열:
                    보일열.append(값열.이름)
        else:
            사람 = [r for r in rows.of(f.대상) if all(c.matches(r) for c in f.조건)]
            if f.열 and f.열 not in 보일열 and f.열 != "지원자_ID":
                보일열.append(f.열)
        줄 = "".join(
            "<tr>" + "".join(
                (f"<td><a href='/candidate?id={urllib.parse.quote(r.get('지원자_ID', ''))}'>"
                 f"{html.escape(str(r.get(c, '')) or r.get('지원자_ID', ''))}</a></td>")
                if c == "한글_이름" else f"<td>{html.escape(str(r.get(c, '')))}</td>"
                for c in 보일열) + "</tr>"
            for r in 사람)
        몸 = 못찾음 + (f"<div class='scroll'><table data-name='{html.escape(b.제목 or '명단')}'>"
              "<tr>" + "".join(f"<th>{html.escape(c)}</th>" for c in 보일열) + "</tr>"
              + (줄 or f"<tr><td colspan='{len(보일열)}' class='muted'>해당하는 사람이 없습니다.</td></tr>")
              + "</table></div>")
    # 시트는 **이 수식이 읽은 칸 값**과 시트에 보였던 값을 같이 적는다. 숫자와
    # 명단 수가 어긋나면 어디서 갈렸는지 화면에서 바로 보인다.
    풀이 = ""
    if b.종류 == "시트" and 값찾기 is not None:
        참조 = S.참조들(S.고정떼기(수식))
        보인값 = 값찾기(행)
        조각 = [f"{a} = «{값찾기(a)}»" for a in 참조[:6]]
        if 조각:
            풀이 += ("<p class='muted'>읽은 칸: " + html.escape(" · ".join(조각)) + "</p>")
        if (f is not None and str(보인값).replace(".0", "").strip().isdigit()
                and re.fullmatch(r"\s*=\s*COUNTIFS?\s*\(.*\)\s*", S.고정떼기(수식),
                                 re.I | re.S)
                and int(float(보인값)) != len(사람)):
            풀이 += (f"<p class='flag'>시트에는 <b>{html.escape(str(보인값))}</b> 으로 "
                   f"보이는데 명단은 {len(사람)}명입니다. 이 화면을 캡처해 알려 주세요.</p>")
    어디 = " × ".join(x for x in (행, 열) if x)
    뒤로 = f"/dash/view?id={b.dashboard_id}" + "".join(
        f"&{키}={urllib.parse.quote(거르개[열이름])}"
        for 키, 열이름, _축 in 대시거르개_목록 if 열이름 in 거르개)
    return _page(
        f"{b.제목 or '명단'} — 누구인지",
        f"<div class='card'><h2>{html.escape(b.제목 or '명단')}"
        + (f" <span class='muted'>{html.escape(어디)}</span>" if 어디 else "")
        + f" <span class='muted'>{len(사람)}명</span></h2>"
        f"<p><a class='btn sec' href='{뒤로}'>← 대시보드로</a> "
        f"<code>{html.escape(수식)}</code>"
        + (f" <span class='muted'>거름: {html.escape(', '.join(f'{k}={v}' for k, v in 거르개.items()))}</span>"
           if 거르개 else "")
        + "</p>"
        + ("<p class='muted'>비율(PCT)은 <b>조건에 맞은 사람</b>(분자)을 보여 줍니다.</p>"
           if f is not None and not isinstance(f, str) and f.함수 == "PCT" else "")
        + 풀이 + 몸 + "</div>",
        me=me,
    )


def _빈축까닭(b, 축값) -> str:
    """축표·자유표에 줄이 하나도 없을 때 **왜 없는지** 말해 준다.

    «줄이 없습니다» 만 보면 수식이 틀렸는지 데이터가 없는지 알 수가 없다.
    """
    어디서 = {"부서": "<a href='/org'>부서·과제</a> 에서 부서를 추가하세요",
            "과제": "<a href='/org'>부서·과제</a> 에서 과제를 추가하세요",
            "등록년도": "지원자가 등록되면 생깁니다",
            "최종상태": "채용 현황 설정에서 상태를 추가하세요"}
    if b.종류 == "축표":
        for 이름, 축 in (("행", b.행축), ("열", b.열축)):
            if 축 == "직접 입력":
                값들 = b.행이름 if 이름 == "행" else b.열이름
                if not 값들:
                    return f"{이름} 축을 «직접 입력» 으로 두었는데 값이 비어 있습니다."
            elif 축 and not 축값.get(축):
                return (f"{이름} 축 «{html.escape(축)}» 에 값이 아직 없습니다"
                        + (f" — {어디서[축]}." if 축 in 어디서 else "."))
    elif not b.행이름:
        return "행 이름을 적고 저장하면 줄이 생깁니다."
    return "줄이 없습니다."


def _px(값) -> int:
    """열 너비로 쓸 수 있는 px 값. 아니면 0 (= 안 정함).

    0·음수·글자를 그대로 담으면 표가 사라지거나 뒤집힌다.
    """
    try:
        n = int(float(str(값 or "").strip()))
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _열너비합(너비들) -> int:
    """열 너비의 합. **하나라도 안 정했으면 0** (= 합으로 안 세운다)."""
    값들 = [_px(w) for w in 너비들]
    return sum(값들) if 값들 and all(값들) else 0


def _표모양(b, 너비들=()) -> str:
    """블록에 정한 모양을 표 태그의 class·style 로.

    기본은 가로줄만 있는 조용한 표다. 그런데 **줄이 길어지면 칸 구분이 안 된다**
    — 이름 옆의 학력이 어디까지인지 눈으로 못 자른다. 그럴 때 격자를 켠다.
    """
    cls = ["dtbl", f"b-{ {'격자': 'grid', '없음': 'none'}.get(b.테두리, 'row') }"]
    if b.줄무늬:
        cls.append("zebra")
    if b.촘촘히:
        cls.append("tight")
    if b.표너비 != "창에 맞춤":
        cls.append("fit")
    style = ""
    # 열 너비를 **전부** 정했으면 표 폭은 그 합이다 (엑셀과 같다). 절반만 정해
    # 놓고 합을 박으면 나머지 열이 갈 자리가 없어 글자가 뭉개지므로, 하나라도
    # 비어 있으면 «남은 자리를 나눠 가진다» 는 지금 규칙을 그대로 둔다.
    합 = _열너비합(너비들)
    if 합:
        cls.append("fixed")
        style += f";width:{합}px"
    if b.머리배경:
        # 머리글 배경은 CSS 변수로 넘긴다 (인라인 스타일은 th 에 못 닿는다)
        style += f";--headbg:{b.머리배경}"
    style = style.strip(";")
    return (f"class='{' '.join(cls)}'"
            + (f" style='{html.escape(style)}'" if style else ""))


#: 지금 그리는 대시보드 보기의 문맥 (몇 번 대시보드, 어떤 거르개). 숫자·축표
#: 칸을 누르면 «누구인지» 로 가는 주소를 만들 때 쓴다. 편집 화면에서는 비어 있다.
_드릴문맥: contextvars.ContextVar = contextvars.ContextVar("드릴문맥", default=None)


def _이름맵(rows) -> dict[str, list[str]]:
    """{이름: [지원자_ID…]} — 한글·영문 이름 둘 다. 보기 화면에서 이름에 링크를 건다."""
    맵: dict[str, list[str]] = {}
    for r in rows.지원자:
        cid = str(r.get("지원자_ID") or "")
        for 키 in ("한글_이름", "영문_이름"):
            n = str(r.get(키) or "").strip()
            if n and cid and cid not in 맵.get(n, []):
                맵.setdefault(n, []).append(cid)
    return 맵


def _사람주소(cid: str) -> str:
    return f"/candidate?id={urllib.parse.quote(cid)}"


def _이름칸(글: str) -> str | None:
    """칸 글이 **사람 이름**(또는 `가, 나` 처럼 이름 나열)이면 링크를 건 HTML, 아니면 None.

    보기 화면에서만 건다 (편집 화면의 미리보기에서는 누르면 편집을 떠난다).
    같은 이름이 여럿이면 그 이름으로 인재 Pool 을 찾아 준다 — 아무나 고르면 틀린
    사람에게 간다.
    """
    문맥 = _드릴문맥.get()
    맵 = (문맥 or {}).get("이름")
    t = str(글 or "").strip()
    if not 맵 or not t or "\n" in t:
        return None

    def 하나(n: str) -> str:
        ids = 맵[n]
        주소 = (_사람주소(ids[0]) if len(ids) == 1
              else "/?q=" + urllib.parse.quote(n))
        뜻 = "상세 보기" if len(ids) == 1 else f"같은 이름 {len(ids)}명 — 인재 Pool 에서 찾기"
        return f"<a class='person' href='{주소}' title='{뜻}'>{html.escape(n)}</a>"

    if t in 맵:
        return 하나(t)
    조각 = [x.strip() for x in t.split(",")]
    if len(조각) > 1 and all(x in 맵 for x in 조각):
        return ", ".join(하나(x) for x in 조각)
    # 이름 뒤에 다른 값을 붙인 칸 (`홍길동(1990.01)`, `홍길동 19900101`) — 앞의
    # 이름에만 링크를 건다. 이름 바로 뒤가 글자면 다른 낱말이다 (`가나` ≠ `가`+`나`).
    글 = str(글 or "")
    앞공백 = len(글) - len(글.lstrip())
    for n in sorted((n for n in 맵 if t.startswith(n)), key=len, reverse=True):
        다음 = t[len(n):len(n) + 1]
        if 다음 and (다음.isalpha() or 다음 == "_"):
            continue
        return (html.escape(글[:앞공백]) + 하나(n)
                + html.escape(글[앞공백 + len(n):]))
    return None


#: 대시보드 전체에 거는 거르개. (주소 열쇠, 줄의 열 이름, 축 이름)
대시거르개_목록 = (("y", "등록년도", "등록년도"), ("dept", "부서", "부서"),
               ("proj", "과제", "과제"))


def _대시거르개(params: dict) -> dict[str, str]:
    """주소(`?y=2025&dept=공정`)에서 거르개를 읽는다. {열 이름: 값}"""
    return {열: (params.get(키) or [""])[0].strip()
            for 키, 열, _축 in 대시거르개_목록 if (params.get(키) or [""])[0].strip()}


def _거른줄(rows, 축값: dict, 거르개: dict):
    """거르개를 **모든 블록에** 먹인다. (거른 줄, 좁힌 축값)

    블록마다 수식에 `등록년도="2025"` 를 박아 두면 해가 바뀔 때마다 대시보드를
    복제해 수식을 하나하나 고쳐야 했다. 줄을 먼저 거르면 수식은 그대로다.
    축도 좁힌다 — 부서를 하나 골랐는데 다른 부서 줄이 0 으로 늘어서 있으면 읽기 어렵다.
    """
    if not 거르개:
        return rows, 축값
    맞나 = lambda r: all(str(r.get(열, "")) == 값 for 열, 값 in 거르개.items())
    좁힌축 = dict(축값)
    for _키, 열, 축 in 대시거르개_목록:
        if 열 in 거르개:
            좁힌축[축] = [거르개[열]]
    return (F.Rows(지원자=[r for r in rows.지원자 if 맞나(r)],
                   채용=[r for r in rows.채용 if 맞나(r)]), 좁힌축)


def _dash_view_page(did: int, me: User, 거르개: dict | None = None) -> bytes:
    d = boards.get(did)
    if d is None:
        return _page("없음", "<div class='card'>대시보드를 찾을 수 없습니다.</div>", me=me)
    거르개 = 거르개 or {}
    전체줄 = 대시보드_행()
    전체축 = 대시보드_축()
    rows, 축값 = _거른줄(전체줄, 전체축, 거르개)
    아는열 = 대시보드_열()
    블록들 = boards.blocks(did)
    _드릴문맥.set({"did": did, "거르개": 거르개, "이름": _이름맵(전체줄), "열": 아는열})
    몸 = "".join(_블록그리기(b, rows, 축값, 아는열) for b in 블록들)
    if not 블록들:
        몸 = ("<div class='card'><p class='muted'>블록이 없습니다. "
              f"<a href='/dash/edit?id={did}'>편집</a> 에서 추가하세요.</p></div>")

    def 고르개(키: str, 열: str, 축: str) -> str:
        값들 = 전체축.get(축) or []
        지금 = 거르개.get(열, "")
        opts = "<option value=''>전체</option>" + "".join(
            f"<option{' selected' if v == 지금 else ''}>{html.escape(v)}</option>"
            for v in 값들)
        return (f"<label class='rt-lbl'>{html.escape(열)} "
                f"<select name='{키}' onchange='this.form.submit()'>{opts}</select></label>")
    거르개폼 = (
        "<form method='get' action='/dash/view' class='bar' style='margin-top:8px'>"
        f"<input type='hidden' name='id' value='{did}'>"
        + "".join(고르개(*x) for x in 대시거르개_목록)
        + (f"<a class='btn sec' href='/dash/view?id={did}'>거르개 풀기</a>" if 거르개 else "")
        + "<span class='muted'>모든 블록에 걸립니다. 주소를 저장해 두면 그 조건으로 다시 열립니다.</span>"
        "</form>"
    )
    기준 = (f"인재 Pool {len(rows.지원자)}명 · 채용 중 {len(rows.채용)}명 기준"
          + (f" <b>(거름: {html.escape(', '.join(f'{k}={v}' for k, v in 거르개.items()))})</b>"
             if 거르개 else ""))
    return _page(
        d.이름,
        f"<div class='card'><h2>{html.escape(d.이름)}"
        f"<span class='muted'> {html.escape(d.설명)}</span></h2>"
        f"<p><a class='btn sec' href='/dash'>목록</a> "
        f"<a class='btn sec' href='/dash/edit?id={did}'>편집</a> "
        f"<span class='muted'>{기준} · {html.escape(now_kst().strftime('%Y-%m-%d %H:%M'))}"
        "</span></p>" + 거르개폼 + "</div>" + 몸,
        me=me,
        폭=d.폭,
    )


def _칸키(행: str, 열: str) -> str:
    """자유 표 칸 하나를 가리키는 키. 탭으로 잇는다 (열 이름에 쉼표가 있어도 안전)."""
    return f"{행}\t{열}"


def _수식도움() -> str:
    """수식을 처음 보는 사람이 읽을 안내. 화면에 늘 붙여 둔다."""
    return (
        "<details><summary class='muted'>수식 쓰는 법 (누르면 펼쳐집니다)</summary>"
        "<div style='margin-top:8px'>"
        "<p class='muted'>모양은 하나뿐입니다 — <code>=함수(대상, 조건...)</code>. "
        "<b>SQL 이 아닙니다.</b> 할 수 있는 일이 정해져 있어 안전합니다.</p>"
        "<table><tr><th>함수</th><th>뜻</th><th>예</th></tr>"
        "<tr><td>COUNT</td><td>몇 명</td><td><code>=COUNT(채용, 부서=\"차세대공정\")</code></td></tr>"
        "<tr><td>PCT</td><td>같은 대상 대비 비율(%)</td><td><code>=PCT(채용, 최종상태~\"*합격\")</code></td></tr>"
        "<tr><td>AVG SUM MIN MAX</td><td>숫자 열을 셈</td><td><code>=AVG(지원자, 저널_수)</code></td></tr>"
        "<tr><td>LIST</td><td>이름 나열</td><td><code>=LIST(채용, 부서=\"소재분석\")</code></td></tr>"
        "</table>"
        "<p class='muted' style='margin-top:8px'><b>대상</b>은 "
        "<code>지원자</code>(인재 Pool 전체) 또는 <code>채용</code>(채용 시작한 사람).<br>"
        "<b>조건</b>은 <code>열=\"값\"</code> <code>열~\"패턴*\"</code> "
        "<code>열!~\"패턴*\"</code> <code>열&gt;숫자</code> <code>열!=\"값\"</code> — "
        "쉼표로 이으면 전부 만족(AND).<br>"
        "<b>열 이름</b>은 <a href='/fields'>표 항목</a> 에 있는 그대로 씁니다. "
        "없는 이름을 쓰면 저장할 때 막습니다.</p>"
        "<p class='muted'><b>와일드카드</b>는 <code>*</code>(아무 글자 몇 개든) 와 "
        "<code>?</code>(한 글자) 입니다. <code>=COUNT(채용, 부서=\"*\")</code> 는 "
        "부서를 안 가리고 전부 셉니다. 별표 그 글자를 찾을 때는 "
        "<code>~*</code> 로 적습니다.</p>"
        "<p class='muted'><code>=</code> 로 시작하지 않으면 그냥 글자로 들어갑니다.</p>"
        "<div class='warn' style='background:#eef5ff;border-color:#c9dcf5'>"
        "<b>집계와 계산을 섞어 쓸 수 있습니다.</b> 숫자·축표·자유표·시트 어디서나 "
        "같습니다.<br>"
        "<code>=COUNT(지원자, 부서=\"소재분석\") / COUNT(지원자) * 100</code> · "
        "<code>=ROUND(AVG(지원자, 저널_수), 1)</code> · "
        "<code>=\"합계 \"&COUNT(지원자)&\"명\"</code></div>"
        "<div class='warn' style='background:#eef5ff;border-color:#c9dcf5'>"
        "<b>엑셀과 같은 모양으로도 씁니다.</b> 열 이름이 곧 엑셀의 <b>범위</b>(열 하나 통째)입니다.<br>"
        "<code>=COUNTIFS(부서,\"소재분석\",최종상태,\"합격\")</code> · "
        "<code>=COUNTIF(저널_수,\"&gt;=3\")</code> · "
        "<code>=SUMIFS(저널_수,부서,\"A\")</code> · "
        "<code>=AVERAGEIFS(저널_수,부서,A3)</code><br>"
        "쓸 수 있는 것: <code>COUNTIF(S)</code> <code>SUMIF(S)</code> <code>AVERAGEIF(S)</code> "
        "<code>MAXIFS</code> <code>MINIFS</code> <code>COUNTBLANK</code> "
        "<code>SUMPRODUCT</code> <code>FILTER</code> <code>UNIQUE</code>.<br>"
        "<b>조건</b>은 엑셀과 같습니다 — <code>\"abc\"</code>(같다, 대소문자 안 가림) "
        "<code>\"&gt;3\"</code> <code>\"&lt;&gt;합격\"</code> <code>\"*합격\"</code>(와일드카드) "
        "<code>\"\"</code>(빈칸) <code>\"&gt;\"&amp;A2</code>(칸 값과 잇기).<br>"
        "<b>함수 안에 함수</b>를 마음대로 넣습니다 — "
        "<code>=IF(COUNTIFS(부서,A3)&gt;0,\"있음\",\"없음\")</code> · "
        "<code>=ROUND(AVERAGEIFS(저널_수,부서,A3),1)</code> · "
        "<code>=TEXTJOIN(\", \",TRUE,FILTER(한글_이름,부서=\"A\"))</code> · "
        "<code>=COUNTA(UNIQUE(부서))</code> · "
        "<code>=SUMPRODUCT((부서=\"A\")*(저널_수&gt;2))</code><br>"
        "<b>지원자 DB 에서 찾기</b> — 열 이름이 범위라 엑셀처럼 찾습니다: "
        "<code>=XLOOKUP(B3,한글_이름,박사_학교)</code> · "
        "<code>=INDEX(저널_수,MATCH(\"홍길동\",한글_이름,0))</code> · "
        "<code>=VLOOKUP(B3,HSTACK(한글_이름,박사_학교,저널_수),3,FALSE)</code> "
        "(HSTACK 이 열들을 옆으로 붙여 표를 만듭니다).<br>"
        "<b>채용 중인 사람만</b> 세려면 조건에 <code>채용중,\"Y\"</code> 를 더합니다 — "
        "<code>=COUNTIFS(채용중,\"Y\",부서,\"A\")</code>.<br>"
        "시트 칸 범위도 됩니다 — <code>=COUNTIF(A1:A10,\"합격\")</code>. "
        "<b>아래의 예전 모양</b>(<code>=COUNT(지원자, 부서=\"A\")</code>)도 그대로 됩니다.</div>"
        "<div class='warn' style='background:#eef5ff;border-color:#c9dcf5'>"
        "<b>대상을 안 적으면 «지원자»</b>(인재 Pool 전체)입니다. "
        "<code>=COUNTIF(부서=\"소재분석\")</code> 처럼 짧게 써도 됩니다. "
        "채용 중인 사람만 세려면 <code>=COUNTIF(채용, …)</code> 처럼 적으세요.<br>"
        "<b>시트에서는 조건 값에 칸 주소</b>를 쓸 수 있습니다 — "
        "<code>=COUNTIF(부서=A3)</code> 는 A3 칸에 적힌 부서를 셉니다. "
        "A열에 부서를 늘어놓고 B열에서 세는 식으로 씁니다. "
        "따옴표로 감싸면(<code>부서=\"A3\"</code>) 칸이 아니라 "
        "<b>그 글자</b>를 찾습니다.<br>"
        "<b>조건 값에 함수</b>도 쓸 수 있습니다 — "
        "<code>=COUNTIFS(입사월=MONTH(TODAY()))</code> 는 이번 달을 셉니다. "
        "<b>괄호가 있을 때만</b> 계산합니다 — 그래야 "
        "<code>번호=10-2020-0012345</code> 같은 값이 뺄셈이 되지 않습니다. "
        "괄호 없는 낱말(<code>부서=소재분석</code>)은 <b>글자 그대로</b>입니다."
        "</div>"
        "<p class='muted'><b>한 사람의 한 칸 값</b>을 가져오려면 LIST 의 마지막에 "
        "<b>열 이름</b>을 적습니다.<br>"
        "<code>=LIST(지원자, 한글_이름=\"홍길동\", 박사_학교)</code> → "
        "<code>서울대학교</code>. 표 항목에서 <b>직접 만드신 열</b>도 그대로 "
        "쓸 수 있습니다.</p>"
        "<div class='warn'><b>패턴 하나만 조심하세요.</b> "
        '<code>최종상태~"*합격"</code> 은 <b>불합격도 맞습니다</b> '
        "(글자 그대로 '합격' 으로 끝나니까요). 합격만 세려면 "
        '<code>=COUNT(채용, 최종상태~"*합격", 최종상태!~"*불합격")</code> '
        "처럼 빼는 조건을 같이 쓰세요.</div>"
        "</div></details>"
    )


#: 수식 도움말에 넣을 보기. (수식, 나오는 모양, 설명)
_수식보기: list[tuple[str, str, str]] = [
    ('=ROW()', "1", "<b>몇 번째 줄인지</b> — No. 열에 씁니다"),
    ('=박사_학교 & " " & 박사_전공', "서울대학교 기계공학", "& 로 잇습니다"),
    ('=TEXT(박사_졸업,"\'yy.m")', "'26.2", "m 은 한 자리 — <b>08 이 8 로</b>"),
    ('=TEXT(박사_졸업,"\'yy.mm")', "'26.02", "mm 은 두 자리"),
    ('=TEXT(박사_졸업,"yyyy.mm")', "2026.02", "연도를 네 자리로"),
    ('=TEXT(박사_졸업,"yyyy년 m월")', "2026년 2월", "서식 밖의 글자는 그대로"),
    ('=TEXT(저널_수/학회_수,"0.0")', "1.5", "<b>숫자 서식</b>도 엑셀과 같습니다"),
    ('=TEXT(0.355,"0.0%")', "35.5%", "% · <code>#,##0</code> · <code>000</code> 도 됩니다"),
    ('=TEXT(박사_시작,"\'yy.m") & "~" & TEXT(박사_졸업,"\'yy.m")',
     "'22.2~'26.2", "기간은 두 번 써서 잇습니다"),
    ('=IF(석사_학교="","",석사_학교)', "(석사가 없으면 빈칸)", "IF 로 갈라 씁니다"),
    ('=TEXTJOIN(" / ", TRUE, 박사_학교, 석사_학교, 학사_학교)',
     "서울대학교 / 포항공대", "<b>TRUE 가 빈 값을 건너뜁니다</b>"),
    ('=IF(박사_석박통합="석박통합","석/박)","박)")', "석/박)", "값에 따라 앞말을 바꿉니다"),
    ('=YEAR(TODAY())-VALUE(LEFT(생년월일,4))', "27", "나이"),
    ('=한글_이름 & "(" & 저널_주저자_수 & "편)"', "홍길동(4편)", "숫자도 그냥 이어집니다"),
    ('=부서="*"', "TRUE", "<b>* 는 아무거나</b> — 부서를 안 가리고 전부"),
    ('=최종상태="*합격"', "TRUE", "'합격' 으로 끝나는 것 (불합격도 맞습니다)"),
    ('=한글_이름="김?"', "TRUE", "<b>? 는 딱 한 글자</b> — 김＊ 두 글자 이름"),
    ('=박사_학교 & CHAR(10) & 석사_학교', "서울대학교↵포항공대",
     "<b>줄바꿈은 CHAR(10)</b> — 입력칸이 한 줄이라 엔터는 안 됩니다"),
    ('=TEXTJOIN(CHAR(10), TRUE, 박사_학교, 석사_학교, 학사_학교)',
     "서울대학교↵포항공대", "여러 줄로 쌓되 빈 건 건너뜁니다"),
]


def _틀도움() -> str:
    """수식 쓰는 법. **엑셀 함수 이름 그대로**라 새로 외울 게 없다.

    예전에는 `{열}` 자리표시자 틀뿐이었다. 배우기는 쉬웠지만 형식을 바꾸려면
    그때마다 새 조각(`{날짜2:…}`)을 만들어 붙여야 해서, 쓰는 사람이 스스로
    넓힐 수가 없었다. 옛 틀도 그대로 돌아가니 쓰던 건 안 고쳐도 된다.
    """
    보기 = "".join(
        f"<tr><td><code>{html.escape(수식)}</code></td>"
        f"<td>{html.escape(결과)}</td><td class='muted'>{설명}</td></tr>"
        for 수식, 결과, 설명 in _수식보기
    )
    함수들 = [
        ("글자", "TEXT TEXTJOIN CONCAT LEFT RIGHT MID LEN TRIM "
                "SUBSTITUTE UPPER LOWER REPT CHAR CODE"),
        ("판단", "IF IFS AND OR NOT IFERROR ISBLANK"),
        ("숫자", "VALUE ROUND INT ABS MIN MAX SUM"),
        ("날짜", "TEXT YEAR MONTH DAY TODAY DATEDIF"),
        ("줄", "ROW"),
    ]
    목록 = "".join(
        f"<tr><td>{갈래}</td><td><code>{html.escape(이름들)}</code></td></tr>"
        for 갈래, 이름들 in 함수들
    )
    서식 = "".join(
        f"<tr><td><code>{코드}</code></td><td>{보임}</td></tr>"
        for 코드, 보임 in [("yyyy", "2026"), ("yy", "26"), ("mm", "02"),
                          ("m", "2"), ("dd", "03"), ("d", "3")]
    )
    return (
        "<details><summary class='muted'>수식 쓰는 법 — 엑셀과 같습니다</summary>"
        "<div style='margin-top:10px'>"
        "<p class='muted'><code>=</code> 로 시작하면 <b>엑셀 수식</b>입니다. "
        "함수 이름도 규칙도 엑셀 그대로라 새로 외울 게 없습니다. "
        "열 이름은 <b>표 항목</b> 탭에 있는 그 이름을 그대로 적습니다 "
        "(띄어쓰기가 있으면 <code>[이름]</code> 처럼 대괄호로 — "
        "<code>=[서류 검토]</code>).</p>"
        "<p class='muted'><code>=ROW()</code> 는 <b>몇 번째 줄인지</b>를 냅니다. "
        "거르고 정렬하고 자른 <b>뒤</b>의 차례라 화면에 보이는 순서와 같습니다. "
        "행 고르기·정렬 칸에서는 못 씁니다 — 그때는 아직 줄이 안 정해졌습니다.</p>"
        "<table><tr><th style='width:44%'>이렇게 쓰면</th><th>이렇게 나옵니다</th>"
        f"<th></th></tr>{보기}</table>"
        "<h3 style='font-size:13px;margin:14px 0 6px'>TEXT 서식 코드 "
        "<span class='muted'>202602 기준</span></h3>"
        f"<table><tr><th style='width:80px'>코드</th><th>나오는 모양</th></tr>{서식}</table>"
        "<h3 style='font-size:13px;margin:14px 0 6px'>쓸 수 있는 함수</h3>"
        f"<table><tr><th style='width:80px'>갈래</th><th>이름</th></tr>{목록}</table>"
        "<p class='muted' style='margin-top:10px'><b>줄바꿈은 "
        "<code>CHAR(10)</code> 입니다.</b> 수식 입력칸이 한 줄짜리라 엔터를 칠 수 "
        "없어서, 엑셀과 같이 글자를 번호로 넣습니다. 줄바꿈을 넣은 칸만 표에서 "
        "줄이 바뀌고, 나머지 칸은 한 줄로 잘린 채 둡니다 — 줄 높이가 들쭉날쭉해지면 "
        "표를 훑기 어렵습니다.</p>"
        "<p class='muted'><b>빈 줄은 사라집니다.</b> "
        "한 줄이 통째로 빈 글자면 그 줄은 안 나옵니다 — 석사를 안 한 사람 "
        "프로필에 빈 석사 줄이 남지 않습니다. 줄 안에서 일부만 비게 하려면 "
        "<code>IF</code> 나 <code>TEXTJOIN(…, TRUE, …)</code> 을 쓰세요.</p>"
        "<p class='muted'><b>와일드카드는 <code>*</code> 와 <code>?</code> 입니다.</b> "
        "엑셀과 같습니다 — <code>*</code> 는 아무 글자 몇 개든(0개도), "
        "<code>?</code> 는 딱 한 글자. 행 고르기에 "
        "<code>=부서=&quot;*&quot;</code> 라고 적으면 부서를 안 가리고 전부 "
        "나옵니다(부서가 비어 있어도 나옵니다). 별표나 물음표 <b>그 글자</b>를 "
        "찾을 때는 <code>~*</code> <code>~?</code> 로 적습니다. "
        "대소문자는 가리지 않습니다.</p>"
        "<p class='muted'><b>예전 방식도 그대로 됩니다.</b> "
        "<code>{박사_학교} {박사_전공}({기간:박사_시작~박사_졸업})</code> 처럼 "
        "<code>=</code> 없이 <code>{}</code> 로 적으면 빈 값이 붙은 괄호까지 "
        "알아서 빠집니다. 쓰던 양식은 안 고쳐도 됩니다.</p>"
        "</div></details>"
    )


#: 블록 종류마다 다른 보기. 빈 칸에 "무엇을 적으라는 거지" 가 없어야 한다.
_초안예시들 = {
    "목록": "예) 채용 중인 사람, 이름과 학력과 주저자 논문 수. 논문 많은 순으로",
    "축표": "예) 부서별로 단계마다 몇 명인지",
    "숫자": "예) 최종 합격한 사람 수",
    "프로필": "예) 공정 부서 지원자마다 학력·경력·논문 실적 한 장씩",
    "표": "예) 첫 줄은 전체 인원, 둘째 줄은 합격 인원. 열은 부서별로",
    "글": "예) 이 대시보드는 매주 월요일 채용 회의에 쓴다는 안내",
}


def _초안예시(종류: str) -> str:
    return _초안예시들.get(종류, "예) 무엇을 만들지 적으세요")


def _조건서식편집(b, 미리볼사람: str = "") -> str:
    """값에 따라 칠하기. **엑셀의 조건부 서식과 같은 감각**이다.

    규칙을 여러 개 둘 수 있고 위에서부터 보다가 처음 맞는 것을 쓴다. 칸 규칙이
    줄 규칙을 이긴다 — 더 좁게 가리킨 쪽이 이긴다.
    """
    열이름 = [머리 or 식 for 머리, 식, _폭 in b.목록열 if str(식).strip()]
    고를것 = [ROW_TARGET] + 열이름

    def 줄(r: dict) -> str:
        대상 = r.get("대상") or ROW_TARGET
        옵션 = "".join(
            f"<option{' selected' if v == 대상 else ''}>{html.escape(v)}</option>"
            for v in dict.fromkeys(고를것 + ([대상] if 대상 not in 고를것 else []))
        )
        return (
            "<tr><td><input type='text' name='cfwhen'"
            f" value='{html.escape(r.get('조건') or '')}' style='width:100%'"
            " class='fx' oninput='fxPreview(this)'"
            f" data-cid='{html.escape(미리볼사람)}'"
            ' placeholder=\'=최종상태="불합격"\'>'
            "<span class='fxout muted'></span></td>"
            f"<td class='ctl'><select name='cfwhere'>{옵션}</select></td>"
            "<td class='ctl'><input type='color' name='cfbg'"
            f" value='{html.escape(r.get('배경') or '#fee2e2')}'></td>"
            # 체크박스는 켠 것만 전송돼서 줄과 짝을 지을 수 없다. 고르개는
            # 언제나 값을 보내므로 줄 차례가 그대로 유지된다.
            "<td class='ctl'><select name='cffgmode'>"
            + f"<option{'' if r.get('글자') else ' selected'}>기본</option>"
            + f"<option{' selected' if r.get('글자') else ''}>직접</option>"
            + "</select> <input type='color' name='cffg'"
            f" value='{html.escape(r.get('글자') or '#16191d')}'></td>"
            "<td class='ctl'><button type='button' class='sec tiny'"
            " onclick='rowMove(this,-1)' title='위로'>↑</button> "
            "<button type='button' class='sec tiny' onclick='rowMove(this,1)'"
            " title='아래로'>↓</button> "
            "<button type='button' class='danger ghost tiny'"
            " onclick='rowDrop(this)' title='이 규칙 빼기'>×</button></td></tr>"
        )

    빈줄 = {"조건": "", "대상": ROW_TARGET, "배경": "#fee2e2", "글자": ""}
    return (
        "<details class='draft'><summary>값에 따라 칠하기</summary>"
        "<div class='scroll' style='margin-top:8px'><table>"
        "<tr><th>이 조건이 참이면</th><th class='ctl' style='width:150px'>어디를</th>"
        "<th class='ctl' style='width:70px'>배경</th>"
        "<th class='ctl' style='width:160px'>글자색</th>"
        "<th class='ctl' style='width:120px'></th></tr>"
        + "".join(줄(r) for r in (b.조건서식 + [빈줄]))
        + "</table></div>"
        "<p class='muted'>규칙은 <b>위에서부터</b> 보다가 처음 맞는 것을 씁니다. "
        "칸 규칙이 줄 규칙을 이깁니다. 조건은 문장 수식과 같은 문법이고 "
        "<b>참/거짓</b>을 냅니다 — "
        "<code>=최종상태=&quot;불합격&quot;</code>, "
        "<code>=저널_주저자_수&gt;=5</code>, "
        "<code>=AND(검토_필요=&quot;Y&quot;, 부서=&quot;공정&quot;)</code>. "
        "맨 아래 빈 줄에 적으면 규칙이 늘어납니다.</p>"
        "</details>"
    )


def _표모양편집(b) -> str:
    """표 모양 고르개. 목록·축표·자유표가 함께 쓴다.

    기본은 가로줄만 있는 조용한 표인데, 줄이 길어지면 **칸 구분이 안 된다** —
    이름 옆의 학력이 어디까지인지 눈으로 못 자른다. 그럴 때 격자를 켠다.
    """
    고르기 = lambda 이름, 값들, 지금: (
        f"<select name='{이름}'>" + "".join(
            f"<option{' selected' if v == 지금 else ''}>{html.escape(v)}</option>"
            for v in 값들) + "</select>"
    )
    return (
        "<details class='draft'><summary>표 모양</summary>"
        "<p class='bar' style='margin-top:8px'>"
        "<label class='rt-lbl'>테두리 "
        + 고르기("border", ("가로줄", "격자", "없음"), b.테두리) + "</label>"
        "<label class='rt-lbl'>표 너비 "
        + 고르기("tablewidth", ("창에 맞춤", "내용에 맞춤"), b.표너비) + "</label>"
        "<label class='rt-lbl'><input type='checkbox' name='zebra'"
        + (" checked" if b.줄무늬 else "") + "> 줄무늬</label>"
        "<label class='rt-lbl'><input type='checkbox' name='tight'"
        + (" checked" if b.촘촘히 else "") + "> 촘촘히</label>"
        "<label class='rt-lbl'>머리글 배경 "
        f"<input type='color' name='headbg' value='{html.escape(b.머리배경 or '#f7f8fa')}'>"
        "</label>"
        "<label class='rt-lbl'><input type='checkbox' name='headbgoff'"
        + ("" if b.머리배경 else " checked") + "> 기본색</label>"
        "</p>"
        "<p class='muted'>칸 구분이 안 되면 <b>격자</b>를 켜세요 (진한 검정 실선). "
        "줄이 많으면 <b>줄무늬</b>가, 한 화면에 더 담고 싶으면 <b>촘촘히</b>가 "
        "도움이 됩니다.</p>"
        "<p class='muted'><b>내용에 맞춤</b>은 칸을 억지로 줄이지 않고, 넘치면 "
        "<b>가로로 스크롤</b>합니다 — 열이 많을 때 이쪽이 읽힙니다 (목록은 기본). "
        "<b>창에 맞춤</b>은 화면 폭을 나눠 갖느라 글자가 잘립니다.</p>"
        "</details>"
    )


def _축표열이름(b, 축값) -> list[str]:
    """축표·자유표가 실제로 그릴 열 이름. **그리는 쪽과 같은 규칙**으로 뽑는다.

    (`dashboards.render_table` 이 쓰는 것 그대로 — 두 곳이 갈라지면 편집
    화면에 없는 열이 표에 나오거나 그 반대가 된다.)
    """
    if b.종류 == "축표":
        열들 = 축값.get(b.열축, []) if b.열축 != "직접 입력" else b.열이름
        return [str(c) for c in (열들 or [""])]
    return [str(c) for c in b.열이름]


def _열너비편집(b, 이름들: list[str], 줄이름칸: bool = True) -> str:
    """열 너비를 숫자로 적는 자리. 옆에 **지금 합계**를 보여 준다.

    합계를 안 보여 주면 숫자를 넣어 놓고도 표가 몇 px 인지 알 수가 없어서,
    한 칸 고칠 때마다 저장하고 보러 가는 왕복이 생긴다.
    """
    폭맵 = b.열너비
    칸들 = [("", "(줄 이름)")] if 줄이름칸 else []
    칸들 += [(c, c) for c in 이름들]
    if not 칸들:
        return ""
    줄 = "".join(
        f"<label class='rt-lbl' style='gap:4px'>"
        f"<span class='muted'>{html.escape(보일이름)}</span>"
        f"<input type='hidden' name='colwname' value='{html.escape(키)}'>"
        f"<input type='number' name='colw' value='{html.escape(폭맵.get(키, ''))}'"
        " min='30' max='2000' step='10' style='width:72px' placeholder='자동'"
        " oninput='wsum(this)'></label>"
        for 키, 보일이름 in 칸들
    )
    return (
        "<p class='bar' style='align-items:flex-start;flex-wrap:wrap'>"
        "<b style='padding-top:5px'>열 너비</b>"
        f"<span style='display:flex;flex-wrap:wrap;gap:2px 6px;flex:1'>{줄}</span>"
        "<span class='wsum muted' style='padding-top:5px'></span></p>"
        "<p class='muted' style='margin-top:-4px'>px 단위. <b>전부 채우면</b> 표"
        " 폭이 그 합이 됩니다 — 화면보다 넓으면 가로로 스크롤합니다."
        " 하나라도 비우면 남은 자리를 나눠 갖습니다.</p>"
    )


def _시트모델(b) -> dict:
    """편집기가 들고 다니는 시트 JSON (브라우저 쪽 모양)."""
    return {"행수": b.시트행수, "열수": b.시트열수, "칸": b.시트칸,
            "열너비": b.시트열너비, "행높이": b.시트행높이,
            "격자숨김": 1 if b.시트격자숨김 else 0,
            "보일범위": b.설정.get("보일범위") or ""}


def _시트_받기(b, data: dict):
    """폼으로 온 시트 JSON 을 거르고, 채우기·행열 바꾸기까지 먹인 **설정**.

    저장(`/dash/sheet/save`)과 다시 계산(`/dash/sheet/calc`)이 같은 길을 탄다 —
    두 벌이면 «계산해 보인 것» 과 «저장된 것» 이 조용히 갈라진다.
    """
    try:
        들어온것 = json.loads((data.get("sheet") or ["{}"])[0] or "{}")
    except (ValueError, TypeError):
        들어온것 = {}
    if not isinstance(들어온것, dict):
        들어온것 = {}
    # 자동 채우기·붙여넣기. 브라우저는 «어디를 어디에» 만 보내고, 수식의 칸
    # 참조를 미는 일은 서버가 한다.
    원본 = (data.get("채울원본") or [""])[0].strip().upper()
    대상 = (data.get("채울대상") or [""])[0].strip().upper()
    if 원본 and 대상:
        들어온것 = 시트_채우기(들어온것, 원본, 대상)
    무엇 = (data.get("행열") or [""])[0]
    if 무엇 in ("행삽입", "행삭제", "열삽입", "열삭제"):
        try:
            위치 = int((data.get("위치") or ["0"])[0])
            개수 = int((data.get("개수") or ["1"])[0])
        except ValueError:
            위치, 개수 = 0, 1
        들어온것 = 시트_행열(들어온것, 무엇, 위치, 개수)
    return {**b.설정, **시트_다듬기(들어온것)}


def _시트편집(b) -> str:
    """시트 블록 편집 — 도구막대 + 격자.

    격자는 보기 화면과 **같은 함수**(`_시트표`)로 그린다. 편집에서 보던 모양과
    저장 뒤 모양이 다르면 아무도 안 믿는다.
    """
    rows = 대시보드_행()
    아는열 = 대시보드_열()
    표, 오류 = _시트표(b, rows, 아는열, 편집=True)
    경고 = "".join(f"<p class='flag'>{html.escape(x)}</p>" for x in 오류)

    담긴것 = json.dumps(
        _시트모델(b),
        ensure_ascii=False,
    )

    단추 = lambda 무엇, 글, 도움="": (
        f"<button type='button' class='sec' data-sheet='{무엇}'"
        f" title='{html.escape(도움)}'>{글}</button>")
    크기옵션 = "".join(f"<option value='{n}'>{n}</option>"
                   for n in (10, 11, 12, 13, 14, 16, 18, 20, 24, 28, 32))
    글꼴옵션 = "".join(f"<option value='{'' if f == '기본' else f}'>{f}</option>"
                   for f in SHEET_FONTS)
    테두리옵션 = "".join(
        f"<option value='{v}'>{글}</option>" for v, 글 in (
            ("", "테두리 ▾"), ("모두", "모든 테두리"), ("바깥", "바깥 테두리"),
            ("굵은바깥", "굵은 바깥 테두리"), ("아래", "아래 테두리"), ("위", "위 테두리"),
            ("왼쪽", "왼쪽 테두리"), ("오른쪽", "오른쪽 테두리"),
            ("가로안", "안쪽 가로선"), ("없음", "테두리 없음")))
    선옵션 = "".join(f"<option>{x}</option>" for x in SHEET_BORDER_STYLES)
    도구막대 = (
        "<div class='sheetbar'>"
        "<span class='muted'>고른 칸 <b class='sheetat'>-</b></span>"
        "<span class='sep'></span>"
        + 단추("되돌리기", "↶", "되돌리기 (Ctrl+Z)") + 단추("다시하기", "↷", "다시 하기 (Ctrl+Y)")
        + "<span class='sep'></span>"
        + 단추("굵게", "<b>가</b>", "굵게 (Ctrl+B)")
        + 단추("기울임", "<i>가</i>", "기울임 (Ctrl+I)")
        + 단추("밑줄", "<u>가</u>", "밑줄 (Ctrl+U)")
        + f"<select data-sheet='크기'><option value=''>크기</option>{크기옵션}</select>"
        + f"<select data-sheet='글꼴'>{글꼴옵션}</select>"
        "<span class='sep'></span>"
        "<label class='muted'>글자<input type='color' data-sheet='글자'"
        " value='#333333'></label>"
        "<label class='muted'>배경<input type='color' data-sheet='배경'"
        " value='#fff4cc'></label>"
        "<span class='sep'></span>"
        + 단추("left", "≡ 왼쪽", "왼쪽 맞춤").replace("data-sheet='left'",
                                                "data-sheet='정렬' value='left'")
        + 단추("center", "≡ 가운데", "가운데 맞춤").replace(
            "data-sheet='center'", "data-sheet='정렬' value='center'")
        + 단추("right", "≡ 오른쪽", "오른쪽 맞춤").replace(
            "data-sheet='right'", "data-sheet='정렬' value='right'")
        + "<span class='sep'></span>"
        + f"<select data-sheet='테두리' title='고른 칸에 테두리를 긋습니다'>{테두리옵션}</select>"
        + f"<select data-sheet='선' title='테두리 선 모양'>{선옵션}</select>"
        "<label class='muted' title='테두리 색'>선<input type='color' data-sheet='테두리색'"
        " value='#222222'></label>"
        "<span class='sep'></span>"
        + 단추("병합", "병합",
             "고른 칸을 하나로 합칩니다. 덮인 자리도 같은 값이 됩니다 "
             "(A1·A2 를 합치면 =A1 도 =A2 도 그 값입니다)")
        + 단추("병합해제", "병합 해제")
        + 단추("서식지우기", "서식 지우기", "글은 두고 색·굵기·테두리만 지웁니다")
        + "<span class='sep'></span>"
        + 단추("행삽입", "행 삽입", "고른 행 위에 그 수만큼 끼웁니다 (안 골랐으면 맨 아래)")
        + 단추("행삭제", "행 삭제", "고른 행을 뺍니다. 그 행을 가리키던 수식은 #참조! 가 됩니다")
        + 단추("열삽입", "열 삽입", "고른 열 왼쪽에 끼웁니다 (안 골랐으면 맨 오른쪽)")
        + 단추("열삭제", "열 삭제", "고른 열을 뺍니다")
        + "<span class='sep'></span>"
        + "<label class='muted' title='보기 화면에서 연한 격자선을 숨깁니다."
          " 내가 그은 테두리만 남습니다 (A·B·1·2 머리글은 보기에 늘 안 나옵니다)'>"
          "<input type='checkbox' data-sheet='격자숨김'"
        + (" checked" if b.시트격자숨김 else "") + "> 보기에서 격자선 숨기기</label>"
        + "<span class='sep'></span>"
        + "<label class='muted' title='보기 화면과 엑셀 내려받기에 이 범위만 나갑니다."
          " 비우면 전체. 범위 밖 칸은 계산용으로 써도 됩니다 (편집에서는 흐리게 보입니다)'>"
          "보일 범위 <input type='text' data-sheet='보일범위' style='width:84px'"
          f" placeholder='전체' value='{html.escape(b.설정.get('보일범위') or '')}'></label>"
        + 단추("보일범위고름", "고른 칸으로", "지금 고른 네모를 보일 범위로 정합니다")
        + "</div>"
    )
    return (
        f"<div class='sheetedit' id='b{b.id}'>"
        f"<form method='post' action='/dash/sheet/save' class='sheetform'>"
        f"<input type='hidden' name='id' value='{b.id}'>"
        f"<input type='hidden' class='sheetdata' name='sheet'"
        f" value='{html.escape(담긴것)}'>"
        f"<p><b>시트</b> "
        f"<input type='text' name='title' value='{html.escape(b.제목)}'"
        " placeholder='블록 제목' style='width:280px'> "
        "<button type='submit' name='끝' value='1'>이 블록 저장</button> "
        "<span class='sheetstate muted'>Ctrl+S 로도 저장합니다</span></p>"
        + 도구막대
        # 시트는 집계·행함수·열 이름을 **다 쓴다**(`data-kind='sheet'`).
        # 여러 줄을 담을 수 있어야 한다 (Alt+Enter 로 칸 안 줄바꿈). 그래서
        # <input> 이 아니라 <textarea> 다. 한 줄로 시작해 적는 만큼 늘어난다.
        + "<p><textarea class='sheetfx fx' data-kind='sheet' rows='1'"
          " placeholder='칸을 고르고 바로 치거나 여기에 적으세요 · 칸 안 줄바꿈은 Alt+Enter"
          " (예: =COUNT(지원자, 부서=\"소재분석\") 또는 =B2/B3*100)' disabled></textarea></p>"
        + f"<div class='sheetwarn'>{경고}</div>"
        + f"<div class='sheetgrid' tabindex='0' style='outline:none'>{표}</div>"
        + "<details class='muted'><summary>쓰는 법</summary>"
          "<ul style='margin:6px 0 0 18px'>"
          "<li>칸을 고르고 <b>바로 치면</b> 적기 시작합니다. <b>Alt+Enter</b> 칸 안 줄바꿈 · "
          "<b>Enter</b> 아래로 · "
          "<b>Tab</b> 오른쪽 · <b>Esc</b> 취소 · <b>F2</b>·두 번 누르기 로 고치기 · "
          "<b>Delete</b> 지우기 · 방향키로 옮기고 <b>Shift+방향키</b>·끌기로 여러 칸.</li>"
          "<li><b>수식을 적다가 칸을 누르면</b> 그 칸 주소가 들어갑니다. "
          "<b>Shift+누르기</b> 로 범위(A1:B3).</li>"
          "<li>열 머리(A·B…) 오른쪽 끝·행 머리 아래 끝을 <b>끌면</b> 너비·높이가 바뀝니다. "
          "열 머리를 두 번 누르면 너비를 자동으로 되돌립니다. 머리를 누르면 그 열·행 전체를 고릅니다.</li>"
          "<li><b>Ctrl+Z / Ctrl+Y</b> 되돌리기 · <b>Ctrl+C/V</b> 복사·붙여넣기 — 엑셀·구글 시트·"
          "웹 페이지에서 복사한 표도 <b>병합·굵기·색·테두리까지</b> 붙습니다 (여기서 복사해 엑셀에 "
          "붙여도 그렇습니다) · "
          "<b>Ctrl+D</b> 아래로 채우기 · <b>Ctrl+R</b> 오른쪽으로 · 고른 네모의 <b>■</b> 를 끌어 채우기.</li>"
          "<li>채우기·복사는 엑셀과 같습니다 — <code>=SUM(A1:C1)</code> 을 한 줄 아래로 채우면 "
          "<code>=SUM(A2:C2)</code>. 안 밀리게 하려면 <b>$</b>(<code>=SUM($A$1:$C$1)</code>). "
          "행·열을 끼우거나 빼면 수식의 참조도 따라 옮겨집니다.</li>"
          "<li>칸 자리 함수: <code>ROW()</code> <code>COLUMN()</code> <code>ROW(B7)</code> "
          "<code>ROWS(A1:A9)</code> <code>COLUMNS(A1:C1)</code> "
          "<code>INDEX(A1:C9, 2, 3)</code> <code>MATCH(\"감\", A1:A9, 0)</code> "
          "<code>VLOOKUP(\"감\", A1:C9, 3, FALSE)</code> <code>CHOOSE(2, \"가\", \"나\")</code></li>"
          "<li>고치는 동안은 <b>저장되지 않습니다</b> — 값은 그 자리에서 다시 계산해 보여 줄 뿐입니다. "
          "다 하고 <b>이 블록 저장</b>(Ctrl+S).</li>"
          "<li><b>병합한 칸은 덮인 자리도 같은 값</b>입니다. 그래서 병합된 칸을 범위로 묶으면 "
          "그 값이 여러 번 더해집니다(엑셀은 한 번만 셉니다).</li></ul></details>"
        + "</form>"
        "<p><form method='post' action='/dash/block/move' style='display:inline'>"
        f"<input type='hidden' name='id' value='{b.id}'>"
        "<button class='sec' name='dir' value='-1'>↑</button> "
        "<button class='sec' name='dir' value='1'>↓</button></form> "
        "<form method='post' action='/dash/block/copy' style='display:inline'>"
        f"<input type='hidden' name='id' value='{b.id}'>"
        "<button class='sec' title='이 블록을 바로 아래에 하나 더 만듭니다 (저장한 내용을 복제합니다)'>블록 복제</button></form> "
        "<form method='post' action='/dash/block/delete' style='display:inline'"
        " onsubmit=\"return confirm('이 블록을 지웁니다.')\">"
        f"<input type='hidden' name='id' value='{b.id}'>"
        "<button class='danger'>블록 삭제</button></form></p>"
        + _수식도움() + _열목록도움() + "</div>"
    )


def _블록편집(b, 축값, 미리볼사람: str = "", 지금모양: str = "") -> str:
    """블록 하나의 설정 폼.

    문장 칸에는 **실제 지원자 한 명의 값으로 미리보기**가 붙는다. 저장하고
    대시보드로 가서 확인하는 왕복이 없으면 수식을 고칠 엄두가 안 난다.
    """
    옵션 = lambda 값들, 지금: "".join(
        f"<option{' selected' if v == 지금 else ''}>{html.escape(v)}</option>"
        for v in 값들
    )
    # 빈 화면에서 시작하는 건 어느 블록이든 부담스럽다. 초안이 있으면 **고치는
    # 일**이 되고, 고치는 건 훨씬 쉽다. 그래서 **모든 종류**에 붙인다.
    #
    # 이 폼은 블록 폼 **밖에** 둔다. 폼 안에 폼을 넣으면 브라우저가 안쪽을
    # 버리면서 바깥 폼도 그 자리에서 끊겨, 아래 칸들이 통째로 안 넘어간다.
    앞머리 = (
        "<details class='draft'><summary>말로 적어서 초안 만들기</summary>"
        "<form method='post' action='/dash/block/draft' style='margin-top:8px'>"
        f"<input type='hidden' name='id' value='{b.id}'>"
        "<p><input type='text' name='말' style='width:100%'"
        f" placeholder='{html.escape(_초안예시(b.종류))}'></p>"
        "<p class='muted' style='margin-bottom:4px'>예시 표 "
        "<span class='muted'>(안 넣어도 됩니다) — 엑셀에서 복사해 그대로 "
        "붙여넣거나 <code>|</code> 로 칸을 나눠 적으세요. "
        "<b>값은 예시로만 보고</b> 그 모양이 나오는 수식을 만듭니다.</span></p>"
        "<p><textarea name='예시' rows='4' style='width:100%'"
        " placeholder='지원자 | 이력&#10;"
        "홍길동(27세)/Neurips 1저자 | 경)서울대 포닥 (&#39;25.2~&#39;26.8)'>"
        "</textarea></p>"
        "<p><button type='submit'>초안 만들기</button> "
        "<span class='muted'>지금 내용을 <b>덮어씁니다.</b> 만든 뒤 보고 고쳐서 "
        "저장하세요 — 저장하기 전에는 아무것도 바뀌지 않습니다.</span></p>"
        "</form></details>"
    )
    머리 = (
        f"<form method='post' action='/dash/block/save' id='bf{b.id}'>"
        f"<input type='hidden' name='id' value='{b.id}'>"
        f"<p><b>{html.escape(b.종류)}</b> "
        f"<input type='text' name='title' value='{html.escape(b.제목)}'"
        " placeholder='블록 제목' style='width:280px'></p>"
    )
    꼬리 = (
        "<p><button type='submit'>이 블록 저장</button> "
        "<button type='button' class='sec' onclick='blockPreview(this)'"
        " title='저장하지 않고, 지금 적은 설정으로 아래에 다시 그려 봅니다'>"
        "저장 없이 미리보기</button></form> "
        "<form method='post' action='/dash/block/move' style='display:inline'>"
        f"<input type='hidden' name='id' value='{b.id}'>"
        "<button class='sec' name='dir' value='-1'>↑</button> "
        "<button class='sec' name='dir' value='1'>↓</button></form> "
        "<form method='post' action='/dash/block/copy' style='display:inline'>"
        f"<input type='hidden' name='id' value='{b.id}'>"
        "<button class='sec' title='이 블록을 바로 아래에 하나 더 만듭니다 (저장한 내용을 복제합니다)'>블록 복제</button></form> "
        "<form method='post' action='/dash/block/delete' style='display:inline'"
        " onsubmit=\"return confirm('이 블록을 지웁니다.')\">"
        f"<input type='hidden' name='id' value='{b.id}'>"
        "<button class='danger'>블록 삭제</button></form></p>"
    )

    if b.종류 == "시트":
        # 시트는 다른 블록과 **폼이 따로**다. 격자·서식·병합이 JSON 한 덩어리로
        # 오가므로, 제목·수식 칸들과 같은 폼에 담으면 서로 밟는다.
        return _시트편집(b)

    if b.종류 == "글":
        가운데 = (f"<textarea name='text' rows='4' style='width:100%'>"
               f"{html.escape(b.글)}</textarea>")
    elif b.종류 == "숫자":
        가운데 = (
            f"<p><input type='text' name='formula' value='{html.escape(b.수식)}'"
            " style='width:100%' class='fx' data-kind='agg'"
            " oninput='fxPreview(this)' placeholder='=COUNT(채용)'>"
            "<span class='fxout muted'></span></p>"
            f"<p>형식 <select name='format'>{옵션(CELL_FORMATS, b.설정.get('형식') or '그대로')}"
            "</select></p>"
        )
    elif b.종류 == "축표":
        가운데 = (
            f"<p>행 축 <select name='rowaxis'>{옵션(AXIS_SOURCES, b.행축)}</select> "
            f"열 축 <select name='colaxis'>{옵션(AXIS_SOURCES, b.열축)}</select> "
            f"형식 <select name='format'>{옵션(CELL_FORMATS, b.설정.get('형식') or '그대로')}"
            "</select></p>"
            "<p class='muted'>축을 <b>직접 입력</b> 으로 두면 아래 칸에 쉼표로 적은 값을 씁니다.</p>"
            f"<p><input type='text' name='rows' value='{html.escape(', '.join(b.행이름))}'"
            " placeholder='행 (직접 입력일 때만)' style='width:48%'> "
            f"<input type='text' name='cols' value='{html.escape(', '.join(b.열이름))}'"
            " placeholder='열 (직접 입력일 때만)' style='width:48%'></p>"
            f"<p>칸 수식<br><input type='text' name='cellformula'"
            f" value='{html.escape(b.칸수식)}' style='width:100%'"
            f" class='fx' data-kind='agg' data-bid='{b.id}'"
            " oninput='fxPreview(this)'"
            " placeholder='=COUNT(채용, 부서=\"{행}\", 최종상태~\"{열}*\")'>"
            "<span class='fxout muted'></span></p>"
            "<p class='muted'><code>{행}</code> <code>{열}</code> 이 축 값으로 바뀝니다. "
            "칸을 하나하나 안 적어도 되고, 부서가 늘면 표가 알아서 늘어납니다.</p>"
            + _열너비편집(b, _축표열이름(b, 축값))
            + _표모양편집(b)
        )
    elif b.종류 == "목록":
        # **한 사람이 한 줄, 열은 만드는 사람이 정한다.** 축표(피벗)로는 만들 수
        # 없는 표가 대부분인데, 사람들이 실제로 만들려는 건 대개 이 목록이다.
        열줄 = "".join(
            f"<tr><td><input type='text' name='colhead' value='{html.escape(머리)}'"
            " style='width:100%' placeholder='머리글'></td>"
            f"<td><input type='text' name='colformula' value='{html.escape(식)}'"
            " style='width:100%' class='fx' oninput='fxPreview(this);wsum(this)'"
            f" data-cid='{html.escape(미리볼사람)}' placeholder='=한글_이름'>"
            "<span class='fxout muted'></span></td>"
            f"<td class='ctl'><input type='number' name='colwidth'"
            f" value='{html.escape(폭)}' min='30' max='2000' step='10'"
            " style='width:72px' placeholder='자동' oninput='wsum(this)'></td>"
            "<td class='ctl'><button type='button' class='sec tiny'"
            " onclick='rowMove(this,-1)' title='위로'>↑</button> "
            "<button type='button' class='sec tiny' onclick='rowMove(this,1)'"
            " title='아래로'>↓</button> "
            "<button type='button' class='danger ghost tiny' onclick='rowDrop(this)'"
            " title='이 열 빼기'>×</button></td></tr>"
            for 머리, 식, 폭 in (b.목록열 + [("", "", "")])
        )
        가운데 = (
            "<p class='bar'>누구를 "
            f"<select name='listtarget'>{옵션(('지원자', '채용'), b.목록대상)}</select>"
            "<input type='text' name='listwhere' class='fx' oninput='fxPreview(this)'"
            f" data-cid='{html.escape(미리볼사람)}'"
            f" value='{html.escape(b.목록조건)}' style='flex:1;min-width:280px'"
            " placeholder=\'=최종상태=&quot;최종 합격&quot;  (*로 아무거나, 비우면 전부)\'>"
            "<span class='fxout muted'></span></p>"
            "<p class='bar'>정렬 "
            "<input type='text' name='listsort' class='fx' oninput='fxPreview(this)'"
            f" data-cid='{html.escape(미리볼사람)}'"
            f" value='{html.escape(b.목록정렬)}' style='flex:1;min-width:240px'"
            " placeholder='=저널_주저자_수  (비우면 그대로)'>"
            "<label class='rt-lbl'><input type='checkbox' name='listdesc'"
            + (" checked" if b.목록내림차순 else "")
            + "> 큰 값부터</label>"
            "<label class='rt-lbl'>최대 <input type='number' name='listmax'"
            f" value='{b.목록최대 or ''}' min='0' style='width:70px'"
            " placeholder='전부'>줄</label>"
            "<span class='fxout muted'></span></p>"
            "<div class='scroll'><table><tr><th style='width:150px'>머리글</th>"
            "<th>열 수식</th>"
            "<th class='ctl' style='width:110px'"
            " title='칸 너비 (px). 비우면 알아서'>너비"
            "<span class='wsum muted' style='display:block;font-weight:400'></span>"
            "</th>"
            f"<th class='ctl' style='width:120px'></th></tr>"
            f"{열줄}</table></div>"
            "<p class='muted'>맨 아래 빈 줄에 적으면 열이 늘어납니다. "
            "머리글을 비우면 수식이 그대로 머리글이 됩니다. "
            "<b>수식 대신 그냥 글자</b>를 적으면 모든 줄에 그 글자가 들어갑니다.</p>"
            "<p class='muted'>너비를 <b>전부 채우면</b> 표 폭이 그 합이 됩니다 — "
            "화면보다 넓으면 가로로 스크롤합니다. 하나라도 비우면 남은 자리를 "
            "나눠 갖습니다.</p>"
            + _표모양편집(b) + _조건서식편집(b, 미리볼사람)
        )
    elif b.종류 == "프로필":
        줄 = "".join(
            f"<tr><td><input type='text' name='label' value='{html.escape(라벨)}'"
            " style='width:90px'></td>"
            f"<td><input type='text' name='line' value='{html.escape(틀)}'"
            " style='width:100%' class='fx' oninput='fxPreview(this)'"
            f" data-cid='{html.escape(미리볼사람)}'>"
            "<span class='fxout muted'></span></td></tr>"
            for 라벨, 틀 in (b.줄틀 + [("", "")])
        )
        가운데 = (
            f"<p>누구를 <input type='text' name='target'"
            f" value='{html.escape(b.대상조건)}' style='width:100%'"
            " class='fx' data-kind='agg' oninput='fxPreview(this)'"
            " placeholder='=LIST(채용, 부서=\"차세대공정\")'>"
            "<span class='fxout muted'></span></p>"
            "<p class='bar'>"
            "<label class='rt-lbl'>최대 <input type='number' name='pmax'"
            f" value='{b.프로필최대 or ''}' min='0' style='width:70px'"
            " placeholder='전부'>명</label>"
            "<label class='rt-lbl'>처음에 펼칠 사람 <input type='number' name='popen'"
            f" value='{b.프로필펼침}' min='0' style='width:70px'>명</label>"
            "<span class='muted'>나머지는 이름 한 줄로 접혀 있고 누르면 펼쳐집니다.</span></p>"
            "<p class='muted'>조건에 맞는 사람마다 아래 양식이 한 장씩 나옵니다.</p>"
            f"<p>머리 <input type='text' name='head' value='{html.escape(b.머리틀)}'"
            " style='width:100%' class='fx' oninput='fxPreview(this)'"
            f" data-cid='{html.escape(미리볼사람)}'"
            " placeholder='=한글_이름 &amp; WRAP(현재_신분, &quot; (&quot;, &quot;)&quot;)'>"
            "<span class='fxout muted'></span></p>"
            "<div class='scroll'><table><tr><th style='width:90px'>라벨</th>"
            f"<th>문장 틀</th></tr>{줄}</table></div>"
            "<p class='muted'>빈 줄은 저장할 때 없어집니다. 맨 아래 빈 칸에 적으면 줄이 늘어납니다.</p>"
            + _열너비편집(b, [])
            + _표모양편집(b)
        )
    else:  # 자유 표
        칸입력 = []
        for r in b.행이름:
            칸 = "".join(
                "<td><input type='text' name='cell' value='"
                + html.escape(b.칸.get(_칸키(r, c), ""))
                + "' style='width:150px'></td>"
                for c in b.열이름
            )
            칸입력.append(f"<tr><th style='text-align:left'>{html.escape(r)}</th>{칸}</tr>")
        머리칸 = "<th></th>" + "".join(f"<th>{html.escape(c)}</th>" for c in b.열이름)
        가운데 = (
            f"<p><input type='text' name='rows' value='{html.escape(', '.join(b.행이름))}'"
            " placeholder='행 이름 (쉼표로 구분)' style='width:48%'> "
            f"<input type='text' name='cols' value='{html.escape(', '.join(b.열이름))}'"
            " placeholder='열 이름 (쉼표로 구분)' style='width:48%'></p>"
            f"<p>형식 <select name='format'>{옵션(CELL_FORMATS, b.설정.get('형식') or '그대로')}"
            "</select> <span class='muted'>행·열을 먼저 저장하면 아래 칸이 생깁니다.</span></p>"
            + (f"<div class='scroll'><table><tr>{머리칸}</tr>"
               + "".join(칸입력) + "</table></div>" if b.행이름 and b.열이름 else "")
            + _열너비편집(b, _축표열이름(b, 축값))
            + _표모양편집(b)
        )
    # 이 블록이 **지금 어떻게 보이는지**를 바로 아래에 붙인다. 보기 화면으로
    # 오가며 확인하던 왕복을 없앤다. «저장 없이 미리보기» 가 이 자리를 갈아 끼운다.
    결과칸 = (
        "<details class='blockout' open><summary class='muted'>"
        "<b>이렇게 보입니다</b> <span class='bo-note'>(저장된 설정)</span>"
        + (" <span class='muted'>· 머리글 오른쪽 경계를 끌면 열 너비가 바로 저장됩니다</span>"
           if b.종류 in ("목록", "축표", "표") else "")
        + "</summary>"
        f"<div class='bo-body'>{지금모양}</div></details>"
    )
    return f"<div class='card' id='b{b.id}'>{앞머리}{머리}{가운데}{꼬리}{결과칸}</div>"


def _지운블록칸(did: int) -> str:
    지운것 = boards.deleted_blocks(did)
    if not 지운것:
        return ""
    줄 = "".join(
        f"<tr><td>{html.escape(b.종류)}</td><td>{html.escape(b.제목 or '(제목 없음)')}</td>"
        f"<td class='muted'>{html.escape(때)}</td><td>"
        "<form method='post' action='/dash/block/restore' style='display:inline'>"
        f"<input type='hidden' name='id' value='{b.id}'>"
        "<button class='sec'>되살리기</button></form></td></tr>"
        for b, 때 in 지운것)
    return (f"<div class='card'><details><summary><b>지운 블록</b> "
            f"<span class='muted'>{len(지운것)}개</span></summary>"
            "<table style='margin-top:8px'><tr><th>종류</th><th>제목</th><th>지운 때</th><th></th></tr>"
            f"{줄}</table><p class='muted'>되살리면 맨 아래에 붙습니다.</p></details></div>")


def _dash_edit_page(did: int, me: User, error: str = "", msg: str = "") -> bytes:
    d = boards.get(did)
    if d is None:
        return _page("없음", "<div class='card'>대시보드를 찾을 수 없습니다.</div>", me=me)
    축값 = 대시보드_축()
    블록들 = boards.blocks(did)
    종류단추 = "".join(
        f"<button name='kind' value='{k}' title='{html.escape(BLOCK_HELP.get(k, ''))}'>"
        f"{k}</button> " for k in BLOCK_KINDS
    )
    # 무엇을 골라야 할지가 처음 쓰는 사람의 가장 큰 벽이다. 버튼 옆에 한 줄씩.
    종류설명 = (
        "<details><summary class='muted'>어떤 블록을 고를까요?</summary>"
        "<table style='margin-top:6px'>"
        + "".join(f"<tr><th style='width:80px'>{k}</th>"
                  f"<td>{html.escape(BLOCK_HELP.get(k, ''))}</td></tr>" for k in BLOCK_KINDS)
        + "</table></details>"
    )
    알림 = _알림(msg=msg)
    오류 = _알림(err=error)

    줄묶음 = 대시보드_행_잠깐()
    아는열 = 대시보드_열()

    # 수식 미리보기에 쓸 사람. 실제 값이 보여야 형식을 고칠 수 있다.
    사람들 = store.list_all()[:50]
    미리볼사람 = 사람들[0].지원자_ID if 사람들 else ""
    미리보기고르기 = (
        "<p class='muted'>미리보기 기준 "
        "<select id='fxwho' onchange='fxWhoChanged(this)'>"
        + "".join(
            f"<option value='{html.escape(r.지원자_ID)}'"
            f"{' selected' if r.지원자_ID == 미리볼사람 else ''}>"
            f"{html.escape(r.한글_이름 or r.영문_이름 or r.지원자_ID)}</option>"
            for r in 사람들
        )
        + "</select> — 문장 칸 아래에 <b>이 사람의 값으로</b> 결과가 바로 뜹니다.</p>"
        if 사람들 else
        "<p class='muted'>지원자가 없어 미리보기를 보여줄 수 없습니다.</p>"
    )
    return _page(
        f"{d.이름} 편집",
        알림 + 오류
        + "<div class='card'>"
        f"<h2>{html.escape(d.이름)} 편집</h2>"
        "<form method='post' action='/dash/rename' style='display:flex;gap:8px;flex-wrap:wrap'>"
        f"<input type='hidden' name='id' value='{did}'>"
        f"<input type='text' name='name' value='{html.escape(d.이름)}' style='width:260px'>"
        f"<input type='text' name='desc' value='{html.escape(d.설명)}'"
        " placeholder='설명' style='width:320px'>"
        "<label class='rt-lbl'>화면 폭 <select name='width'>"
        + "".join(f"<option{' selected' if w == (d.너비 or '보통') else ''}>{w}</option>"
                  for w in WIDTHS)
        + "</select></label>"
        "<button type='submit'>이름·설명 저장</button></form>"
        f"<p style='margin-top:10px'><a class='btn' href='/dash/view?id={did}'>보기</a> "
        "<a class='btn sec' href='/dash'>목록</a></p>"
        "<form method='post' action='/dash/block/add' style='margin-top:10px'>"
        f"<input type='hidden' name='dash' value='{did}'>"
        f"<p>블록 추가: {종류단추}</p></form>" + 종류설명
        + _수식도움() + _틀도움() + _열목록도움() + 미리보기고르기 + "</div>"
        + ("".join(_블록편집(b, 축값, 미리볼사람,
                            "" if b.종류 == "시트" else _블록그리기(b, 줄묶음, 축값, 아는열))
                   for b in 블록들)
           or "<div class='card'><p class='muted'>블록이 없습니다. 위에서 추가하세요.</p></div>")
        + _지운블록칸(did)
        + _수식목록() + _FX_JS + _FXAC_JS + _열너비끌기_JS
        + (f"<script>{_SHEET_JS}</script>"
           if any(b.종류 == "시트" for b in 블록들) else ""),
        me=me,
    )


#: 문장 칸 아래에 **실제 값으로** 결과를 바로 보여 준다.
#:
#: 저장하고 대시보드로 가서 확인하고 다시 돌아오는 왕복이 있으면, 수식 하나
#: 고치는 데 세 화면이 든다. 그러면 아무도 안 고친다. 서버에 물어보는 이유는
#: 하나다 — **화면과 대시보드가 같은 계산기를 써야** 미리보기를 믿을 수 있다.
def _수식목록() -> str:
    """자동완성이 쓸 목록을 화면에 한 번만 심는다.

    서버에 다시 묻지 않는다 — 글자 하나 칠 때마다 왕복하면 느리고, 폐쇄망에서
    끊기면 자동완성이 통째로 죽는다. 목록은 열 몇십 개라 실어도 가볍다.
    """
    from ... import expr
    from ... import formula as F

    자료 = {
        "열": sorted(대시보드_열()),
        "행함수": list(expr.PUBLIC_FUNC_NAMES),
        # 별칭까지 보여준다 (COUNTIFS·AVERAGE…). 목록을 두 군데 적지 않으려고
        # `formula.CALLABLE` 이 «부를 수 있는 이름 전부» 를 들고 있다.
        "집계함수": list(F.CALLABLE),
        "대상": list(F.TARGETS),
    }
    자료["설명"] = 함수설명자료(자료["행함수"] + 자료["집계함수"])
    return ("<script>window.수식목록 = "
            + json.dumps(자료, ensure_ascii=False) + ";</script>")


def _열목록도움() -> str:
    """쓸 수 있는 열 이름을 통째로 훑어볼 자리.

    자동완성은 **치면서 찾는** 것이고 이건 **훑어보는** 것이다. 무엇이 있는지
    모르면 칠 수도 없어서 둘 다 필요하다.
    """
    이름들 = sorted(대시보드_열())
    칸 = "".join(f"<code style='margin:0 6px 4px 0;display:inline-block'>"
               f"{html.escape(n)}</code>" for n in 이름들)
    return (
        f"<details><summary class='muted'>쓸 수 있는 열 이름 전부 ({len(이름들)}개)"
        "</summary><div style='margin-top:8px;line-height:2'>"
        "<p class='muted'>수식 칸에 <b>이름 일부를 치면</b> 그것으로 시작하는 "
        "이름이 바로 뜹니다. 아래는 훑어보기용입니다.</p>"
        f"{칸}</div></details>"
    )


#: 수식 칸 자동완성.
#:
#: 치고 있는 낱말을 캐럿에서 뒤로 읽어, 그것으로 **시작하는** 이름을 먼저,
#: 그 다음 **중간에 든** 이름을 보여준다. 칸이 비어 있으면 열 이름 전체를
#: 띄운다 — 무엇을 쓸 수 있는지 둘러보는 자리다.
_FXAC_JS = '\n<script>' + _정적JS("formula_autocomplete.js") + '</script>'
#: 미리보기 표의 머리글 경계를 끌어 열 너비를 정한다 (편집 화면에만).
_열너비끌기_JS = '\n<script>' + _정적JS("col_resize.js") + '</script>'


_FX_JS = '\n<script>' + _정적JS("formula_preview.js") + '</script>'


@라우트("GET", '/dash', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 볼 수 있습니다.')
@라우트("GET", '/dash/view', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 볼 수 있습니다.')
@라우트("GET", '/dash/edit', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 볼 수 있습니다.')
def get_dash(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    err = (params.get("err") or [""])[0]
    msg = (params.get("msg") or [""])[0]
    if path == "/dash":
        return self._send(_dash_list_page(me, err, msg))
    try:
        did = int((params.get("id") or ["0"])[0])
    except ValueError:
        return self._redirect("/dash")
    if path == "/dash/view":
        return self._send(_dash_view_page(did, me, _대시거르개(params)))
    return self._send(_dash_edit_page(did, me, err, msg))
    return _없는주소(self)


@라우트("GET", '/dash/who', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 볼 수 있습니다.')
def get_dash_who(self, me, path):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return self._send(_dash_who_page(me, params))
    return _없는주소(self)


@라우트("GET", '/dash/preview', 권한='대시보드_편집')
def get_dash_preview(self, me, path):
    # 문장 칸 아래 미리보기. 대시보드와 **같은 계산기**를 써야 믿을 수 있다.
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    틀 = (params.get("line") or [""])[0]
    cid = (params.get("id") or [""])[0]
    # 집계 문맥(=COUNT(...)) 과 행 문맥(=한글_이름) 은 계산기가 다르다.
    # 화면이 어느 쪽인지 알려 준다 — 글만 보고 맞히려 들면 틀린다.
    if (params.get("kind") or ["row"])[0] == "agg":
        # 축표 칸 수식의 {행}{열} 은 그대로는 계산이 안 된다. 그 블록의
        # **첫 축 값**을 넣어서 한 칸만 미리 계산해 본다.
        if "{행}" in 틀 or "{열}" in 틀:
            try:
                b = boards.block(int((params.get("bid") or ["0"])[0]))
            except (TypeError, ValueError):
                b = None
            if b is None:
                return self._json(
                    {"text": "", "error": "{행}{열} 은 축을 정해야 계산됩니다"})
            축값 = 대시보드_축()
            첫행 = (축값.get(b.행축) or b.행이름 or ["(행)"])[0]
            첫열 = (축값.get(b.열축) or b.열이름 or ["(열)"])[0]
            보임 = 틀.replace("{행}", 첫행).replace("{열}", 첫열)
            try:
                글, _값 = 수식계산(보임, 대시보드_행_잠깐(), 대시보드_열())
            except (F.FormulaError, expr.ExprError, SheetError,
                    ValueError) as exc:
                return self._json({"text": "", "error": str(exc)})
            return self._json(
                {"text": f"{글}   ({첫행} × {첫열} 칸)", "error": ""})
        # 블록이 쓰는 계산기(`sheet.계산`)와 **같은 것**으로 미리 본다.
        # `F.run` 으로 보면 `=COUNT(지원자)/2` 처럼 섞은 식이 미리보기에서만
        # 틀렸다고 나왔다.
        try:
            글, _값 = 수식계산(틀, 대시보드_행_잠깐(), 대시보드_열())
        except (F.FormulaError, expr.ExprError, SheetError, ValueError) as exc:
            return self._json({"text": "", "error": str(exc)})
        return self._json({"text": str(글), "error": ""})
    # 여기부터는 행 문맥 — 한 사람의 값이 있어야 계산할 수 있다.
    if not cid or store.get(cid) is None:
        return self._json({"text": "", "error": "미리볼 지원자가 없습니다"})
    값들 = _프로필값(cid)
    if expr.is_formula(틀):
        글, 오류 = expr.render(틀, 값들)
        return self._json({"text": 글, "error": 오류})
    return self._json({"text": P.render_line(틀, 값들), "error": ""})
    return _없는주소(self)


@라우트("GET", '/dash/sheet.xlsx', 권한='대시보드_조회')
def get_dash_sheet_xlsx(self, me, path):
    # 시트는 서버가 직접 만든다. 화면에서 TSV 를 만들어 보내는 길로는
    # 색도 병합도 못 싣는다 (글자만 옮겨진다).
    if not can(me, "엑셀_다운로드"):
        return self._deny()
    params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    try:
        bid = int((params.get("block") or ["0"])[0])
    except ValueError:
        bid = 0
    b = boards.block(bid)
    if b is None or b.종류 != "시트":
        return self._send(_page("없음", "<div class='card'>시트를 찾을 수 없습니다.</div>",
                                me=me), code=404)
    결과 = render_sheet(b, 대시보드_행(), 대시보드_열(), 잘라보기=True)
    데이터 = build_sheet_xlsx(결과, b.제목 or "시트")
    이름 = urllib.parse.quote((b.제목 or "시트") + ".xlsx")
    return self._send(
        데이터,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        extra={"Content-Disposition":
               f"attachment; filename*=UTF-8''{이름}"},
    )
    return _없는주소(self)


@라우트("POST", '/dash/block/widths', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 다룰 수 있습니다.', json=True)
def post_dash_block_widths(self, me, path):
    """미리보기에서 열 경계를 끌어 정한 너비를 **바로** 저장한다.

    목록은 `목록열` 의 n번째 열 폭, 축표·표는 `열너비[열 이름]` 이다.
    """
    if not can(me, "대시보드_편집"):
        return self._json({"ok": False, "error": "대시보드를 고칠 권한이 없습니다."}, code=403)
    data = urllib.parse.parse_qs(
        self._read_body().decode("utf-8", "replace"), keep_blank_values=True)
    try:
        b = boards.block(int((data.get("id") or ["0"])[0]))
    except ValueError:
        b = None
    if b is None or b.종류 not in ("목록", "축표", "표"):
        return self._json({"ok": False, "error": "블록을 찾을 수 없습니다."}, code=404)
    열쇠들 = data.get("key") or []
    폭들 = [min(2000, max(30, _px(x))) if _px(x) else 0
           for x in (data.get("px") or [])] + [0] * len(열쇠들)
    설정 = json.loads(json.dumps(b.설정))           # 깊은 복사
    if b.종류 == "목록":
        줄들 = [list(줄) + ["", "", ""] for 줄 in (설정.get("목록열") or [])]
        for 키, 폭 in zip(열쇠들, 폭들):
            if 키.isdigit() and int(키) < len(줄들):
                줄들[int(키)][2] = str(폭) if 폭 else ""
        설정["목록열"] = [줄[:3] for 줄 in 줄들]
    else:
        너비 = dict(설정.get("열너비") or {})
        for 키, 폭 in zip(열쇠들, 폭들):
            if 폭:
                너비[키] = str(폭)
            else:
                너비.pop(키, None)
        설정["열너비"] = 너비
    boards.save_block(b.id, 제목=b.제목, 설정=설정)
    return self._json({"ok": True})


@라우트("POST", '/dash/add', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 다룰 수 있습니다.')
@라우트("POST", '/dash/rename', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 다룰 수 있습니다.')
@라우트("POST", '/dash/copy', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 다룰 수 있습니다.')
@라우트("POST", '/dash/delete', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 다룰 수 있습니다.')
@라우트("POST", '/dash/restore', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 다룰 수 있습니다.')
@라우트("POST", '/dash/purge', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 다룰 수 있습니다.')
@라우트("POST", '/dash/block/add', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 다룰 수 있습니다.')
@라우트("POST", '/dash/block/move', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 다룰 수 있습니다.')
@라우트("POST", '/dash/block/copy', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 다룰 수 있습니다.')
@라우트("POST", '/dash/block/delete', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 다룰 수 있습니다.')
@라우트("POST", '/dash/block/restore', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 다룰 수 있습니다.')
@라우트("POST", '/dash/block/draft', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 다룰 수 있습니다.')
@라우트("POST", '/dash/sheet/calc', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 다룰 수 있습니다.', 읽기전용=True)
@라우트("POST", '/dash/sheet/save', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 다룰 수 있습니다.')
@라우트("POST", '/dash/block/save', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 다룰 수 있습니다.')
@라우트("POST", '/dash/block/preview', 권한='대시보드_조회', 거부말='대시보드는 채용담당자 이상만 다룰 수 있습니다.', 읽기전용=True)
def post_dash_add_묶음(self, me, path):
    data = urllib.parse.parse_qs(
        self._read_body().decode("utf-8", "replace"), keep_blank_values=True
    )

    def 정수(키: str, 기본: int = 0) -> int:
        try:
            return int((data.get(키) or [str(기본)])[0])
        except ValueError:
            return 기본

    if path == "/dash/add":
        이름 = (data.get("name") or [""])[0]
        try:
            did = boards.add(이름, 만든이=me.아이디,
                             설명=(data.get("desc") or [""])[0])
        except ValueError as exc:
            return self._redirect("/dash?err=" + urllib.parse.quote(str(exc)))
        if data.get("sample"):
            _예시블록(did)
        audit.record(me.아이디, "대시보드", str(did), 항목="만들기", 새값=이름)
        return self._redirect(f"/dash/edit?id={did}")

    if path == "/dash/rename":
        did = 정수("id")
        try:
            boards.rename(did, (data.get("name") or [""])[0],
                          (data.get("desc") or [""])[0])
        except ValueError as exc:
            return self._redirect(f"/dash/edit?id={did}&err="
                                  + urllib.parse.quote(str(exc)))
        boards.set_width(did, (data.get("width") or [""])[0])
        return self._redirect(f"/dash/edit?id={did}&msg="
                              + urllib.parse.quote("저장했습니다."))

    if path == "/dash/copy":
        did = 정수("id")
        옛 = boards.get(did)
        if 옛 is None:
            return self._redirect("/dash")
        for n in range(2, 50):
            새이름 = f"{옛.이름} 복사본{'' if n == 2 else n}"
            if not boards.by_name(새이름):
                break
        새id = boards.copy(did, 새이름, 만든이=me.아이디)
        audit.record(me.아이디, "대시보드", str(새id), 항목="복제", 새값=새이름)
        return self._redirect(f"/dash/edit?id={새id}")

    if path == "/dash/delete":
        d = boards.get(정수("id"))
        if d is not None and not _대시_지울수있나(me, d):
            return self._redirect("/dash?err=" + urllib.parse.quote(
                f"'{d.이름}' 은 만든 사람({d.만든이 or '기록 없음'})이나 관리자만 지울 수 있습니다."))
        이름 = boards.delete(d.id, me.아이디) if d else ""
        if 이름:
            audit.record(me.아이디, "대시보드", 이름, 비고="대시보드 휴지통으로")
        return self._redirect("/dash?msg=" + urllib.parse.quote(
            f"'{이름}' 을 휴지통으로 보냈습니다. 아래 휴지통에서 되살릴 수 있습니다."
            if 이름 else "없는 대시보드입니다."))

    if path == "/dash/restore":
        이름 = boards.restore(정수("id"))
        if 이름:
            audit.record(me.아이디, "대시보드", 이름, 비고="휴지통에서 되살림")
        return self._redirect("/dash?msg=" + urllib.parse.quote(
            f"'{이름}' 을 되살렸습니다." if 이름 else "휴지통에 없는 대시보드입니다."))

    if path == "/dash/purge":
        d = boards.get(정수("id"), 지운것도=True)
        if d is not None and not _대시_지울수있나(me, d):
            return self._redirect("/dash?err=" + urllib.parse.quote(
                "만든 사람이나 관리자만 완전히 지울 수 있습니다."))
        이름 = boards.purge(d.id) if d else ""
        if 이름:
            audit.record(me.아이디, "대시보드", 이름, 비고="대시보드 완전 삭제")
        return self._redirect("/dash?msg=" + urllib.parse.quote(
            f"'{이름}' 을 완전히 지웠습니다." if 이름 else "휴지통에 없는 대시보드입니다."))

    if path == "/dash/block/add":
        did = 정수("dash")
        종류 = (data.get("kind") or [""])[0]
        try:
            설정 = {"줄": 기본_프로필틀, "머리": 기본_프로필머리} \
                if 종류 == "프로필" else {}
            boards.add_block(did, 종류, 제목=종류, 설정=설정)
        except ValueError as exc:
            return self._redirect(f"/dash/edit?id={did}&err="
                                  + urllib.parse.quote(str(exc)))
        return self._redirect(f"/dash/edit?id={did}")

    if path == "/dash/block/move":
        bid = 정수("id")
        b = boards.block(bid)
        boards.move_block(bid, 정수("dir", 1))
        return self._redirect(f"/dash/edit?id={b.dashboard_id if b else 0}")

    if path == "/dash/block/copy":
        # 블록 하나만 닮은 것으로 하나 더. **저장된 것**을 베낀다 —
        # 화면에서 고치다 만 것은 아직 DB 에 없다.
        bid = 정수("id")
        b = boards.block(bid)
        did = b.dashboard_id if b else 0
        if b is not None:
            boards.copy_block(bid)
            audit.record(me.아이디, "대시보드", str(did), 항목="블록 복제",
                         새값=b.제목 or b.종류)
        return self._redirect(f"/dash/edit?id={did}")

    if path == "/dash/block/delete":
        bid = 정수("id")
        b = boards.block(bid)
        did = b.dashboard_id if b else 0
        이름 = boards.delete_block(bid)
        if 이름:
            audit.record(me.아이디, "대시보드", str(did), 항목="블록 삭제",
                         이전값=_블록기록(b), 비고=이름)
        return self._redirect(f"/dash/edit?id={did}&msg=" + urllib.parse.quote(
            "블록을 지웠습니다. 맨 아래 «지운 블록» 에서 되살릴 수 있습니다."))

    if path == "/dash/block/restore":
        did = boards.restore_block(정수("id"))
        if did:
            audit.record(me.아이디, "대시보드", str(did), 항목="블록 되살림")
        return self._redirect(f"/dash/edit?id={did}" if did else "/dash")

    if path == "/dash/block/draft":
        # 말 -> 블록 정의 초안. **LLM 은 값을 만들지 않는다** — 정의만
        # 내고, 표는 언제나 우리 계산기가 그린다.
        bid = 정수("id")
        b = boards.block(bid)
        if b is None:
            return self._redirect("/dash")
        말 = (data.get("말") or [""])[0]
        설정, 메모 = dash_draft.draft(
            말, 대시보드_열(), 종류=b.종류,
            축목록=[a for a in AXIS_SOURCES if a != "직접 입력"],
            예시표=(data.get("예시") or [""])[0],
        )
        뒤로 = f"/dash/edit?id={b.dashboard_id}"
        if not 설정:
            return self._redirect(
                f"{뒤로}&err=" + urllib.parse.quote(" / ".join(메모)))
        제목 = 설정.pop("_제목", "") or b.제목 or b.종류
        boards.save_block(bid, 제목=제목, 설정={**b.설정, **설정})
        audit.record(me.아이디, "대시보드", str(b.dashboard_id),
                     항목=f"{b.종류} 초안", 새값=말[:80])
        return self._redirect(
            f"{뒤로}&msg=" + urllib.parse.quote(" / ".join(메모)))

    if path == "/dash/sheet/calc":
        # 편집 중 **다시 계산만** 한다. 저장하지 않는다 — 칸 하나 고칠
        # 때마다 저장하고 화면을 통째로 다시 그리던 것을 이걸로 바꿨다.
        if not can(me, "대시보드_편집"):
            return self._json({"error": "대시보드를 고칠 권한이 없습니다."}, code=403)
        b = boards.block(정수("id"))
        if b is None or b.종류 != "시트":
            return self._json({"error": "시트를 찾을 수 없습니다."}, code=404)
        import dataclasses
        임시 = dataclasses.replace(b, 설정=_시트_받기(b, data))
        표, 오류 = _시트표(임시, 대시보드_행_잠깐(), 대시보드_열(), 편집=True)
        return self._json({"sheet": _시트모델(임시), "html": 표, "오류": 오류})

    if path == "/dash/sheet/save":
        # 시트는 격자·서식·병합이 JSON 한 덩어리로 온다. **믿지 않는다** —
        # `시트_다듬기` 가 색·크기·병합·격자 밖 칸을 전부 다시 거른다.
        bid = 정수("id")
        b = boards.block(bid)
        if b is None:
            return self._redirect("/dash")
        설정 = _시트_받기(b, data)
        제목 = (data.get("title") or [""])[0]
        boards.save_block(bid, 제목=제목, 설정=설정)
        뒤로 = f"/dash/edit?id={b.dashboard_id}"
        if not (data.get("끝") or [""])[0]:
            # 도구막대가 값을 바꿔 **다시 계산하러** 보낸 것이다.
            # 저장은 됐지만 안내는 안 띄운다 — 누를 때마다 뜨면 시끄럽다.
            return self._redirect(f"{뒤로}#b{bid}")
        새것 = boards.block(bid)
        if _블록기록(b) != _블록기록(새것):
            audit.record(me.아이디, "대시보드", str(b.dashboard_id),
                         항목=f"시트 블록 #{bid}", 이전값=_블록기록(b),
                         새값=_블록기록(새것), 비고=제목)
        return self._redirect(f"{뒤로}&msg="
                              + urllib.parse.quote("시트를 저장했습니다.")
                              + f"#b{bid}")

    if path == "/dash/block/save":
        bid = 정수("id")
        b = boards.block(bid)
        if b is None:
            return self._redirect("/dash")
        설정, 오류 = _블록설정(b, data)
        if 오류:
            return self._redirect(f"/dash/edit?id={b.dashboard_id}&err="
                                  + urllib.parse.quote(오류))
        boards.save_block(bid, 제목=(data.get("title") or [""])[0], 설정=설정)
        # 무엇이 어떻게 바뀌었는지 남긴다. 제목만 남기면 «누가 이 수식을
        # 바꿨나» 를 알 길이 없었다.
        새것 = boards.block(bid)
        if _블록기록(b) != _블록기록(새것):
            audit.record(me.아이디, "대시보드", str(b.dashboard_id),
                         항목=f"{b.종류} 블록 #{bid}", 이전값=_블록기록(b),
                         새값=_블록기록(새것), 비고=새것.제목 if 새것 else "")
        return self._redirect(f"/dash/edit?id={b.dashboard_id}&msg="
                              + urllib.parse.quote("블록을 저장했습니다.")
                              + f"#b{bid}")

    if path == "/dash/block/preview":
        # 저장 없이 미리보기 — 폼에 적힌 설정으로 그려만 본다.
        if not can(me, "대시보드_편집"):
            return self._json({"error": "대시보드를 고칠 권한이 없습니다."}, code=403)
        b = boards.block(정수("id"))
        if b is None:
            return self._json({"error": "블록을 찾을 수 없습니다."}, code=404)
        설정, 오류 = _블록설정(b, data)
        if 오류:
            return self._json({"error": 오류})
        import dataclasses
        임시 = dataclasses.replace(
            b, 제목=(data.get("title") or [b.제목])[0], 설정=설정)
        return self._json({"html": _블록그리기(임시, 대시보드_행_잠깐(),
                                           대시보드_축(), 대시보드_열())})
    return self._redirect("/dash")
    return _없는주소(self)
