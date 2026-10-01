"""모든 화면의 틀 — CSS, 위쪽 탭, 알림, `_page`, 처리 현황 표.
"""

from __future__ import annotations

import contextvars
import html
import urllib.parse
from pathlib import Path

from .. import review
from ..auth import can, User

from .state import registry, store
from .jobs import _status, _status_lock

from .mail_targets import _안보낸수


#: 화면에 심는 JavaScript 는 `cvtool/web/static/*.js` 에 있다. 예전에는 파이썬
#: 문자열 안에 있어서 문법 검사도 못 했고, 따옴표·`\t` 하나에 편집기가 통째로
#: 안 뜨는 일이 있었다. 지금은 파일이라 `node --check` 로 본다 (tests/test_static_js.py).
_정적폴더 = Path(__file__).resolve().parent / "static"


def _정적JS(이름: str) -> str:
    """`static/이름` 의 글. 서버가 뜰 때 한 번 읽는다."""
    return (_정적폴더 / 이름).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------
_CSS = """
/* 색·간격·모서리를 토큰으로 모은다. 예전에는 값이 파일 곳곳에 흩어져 있어서
   한 군데만 고치면 나머지가 어긋났다. */
:root{
 --bg:#f7f8fa;--card:#fff;--line:#e6e8ec;--line2:#f0f1f4;--grid:#222;
 --txt:#16191d;--txt2:#42474e;--muted:#6b7280;
 --accent:#2f6fed;--accent-w:#eaf1fe;--accent-d:#1d4fc4;
 --r:10px;--r-s:7px;
 --sh:0 1px 2px rgba(16,24,40,.04),0 1px 3px rgba(16,24,40,.06);
 --sh-l:0 4px 6px -2px rgba(16,24,40,.04),0 12px 16px -4px rgba(16,24,40,.08);
}
*{box-sizing:border-box}
/* 맑은 고딕은 화면에서 낡아 보인다. 요즘 OS 에 깔린 글꼴을 먼저 쓰고,
   없으면 순서대로 내려간다 (폐쇄망이라 웹폰트는 못 받는다). */
body{margin:0;background:var(--bg);color:var(--txt);
 font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI Variable Text","Segoe UI",
 Roboto,"Pretendard","Apple SD Gothic Neo","Noto Sans KR","맑은 고딕",sans-serif;
 -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
header{background:var(--card);color:var(--txt);padding:0 20px;display:flex;gap:2px;
 align-items:stretch;border-bottom:1px solid var(--line);position:sticky;top:0;z-index:50}
header a{color:var(--muted);text-decoration:none;font-weight:550;font-size:13.5px;
 display:flex;align-items:center;padding:13px 12px;border-bottom:2px solid transparent;
 white-space:nowrap}
header .brand{color:var(--txt);font-weight:750;font-size:15px;letter-spacing:-.01em;
 margin-right:10px;padding-right:16px;border-right:1px solid var(--line);
 border-bottom:0;align-self:center;padding-top:0;padding-bottom:0}
header a:hover{color:var(--txt)}
/* 지금 보고 있는 탭. 색만으로 알려주지 않고 굵기와 아래 밑줄이 함께 바뀐다. */
header a.on{color:var(--accent);font-weight:700;border-bottom-color:var(--accent)}
/* 갈 곳이 둘인 탭. 마우스를 올리거나 키보드로 들어오면 아래로 펴진다.
   :hover 만 걸면 키보드로는 영영 못 여니 :focus-within 을 같이 건다. */
header .tab{position:relative;display:flex}
header .tab .sub{display:none;position:absolute;top:100%;left:0;min-width:172px;
 background:var(--card);border:1px solid var(--line);border-radius:0 0 var(--r) var(--r);
 border-top:0;box-shadow:var(--sh-l);padding:4px 0;flex-direction:column;z-index:60}
header .tab:hover .sub,header .tab:focus-within .sub{display:flex}
header .tab .sub a{padding:9px 14px;border-bottom:0;font-weight:500}
header .tab .sub a:hover{background:var(--bg);color:var(--accent);text-decoration:none}
header .sp{flex:1}
/* 오른쪽 끝의 '누구로 들어와 있나'. 두 글자가 아래위로 어긋나 보이지 않게
   같은 줄에 세우고, 역할은 작은 딱지로 붙인다. */
header .who{display:flex;align-items:center;gap:6px;color:var(--muted);
 font-size:12.5px;padding:0 4px}
header .who b{font-weight:650;font-size:11px;color:var(--txt2);background:var(--bg);
 border:1px solid var(--line);border-radius:99px;padding:1px 8px}
header a[href='/logout']{font-weight:500}
/* 링크. 브라우저 기본 파랑 밑줄은 화면을 낡아 보이게 한다. */
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.card h2 a{color:inherit}
main{padding:22px 20px 40px;max-width:var(--mainw,1600px);margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
 padding:18px 20px;margin-bottom:16px;box-shadow:var(--sh)}
h2{margin:0 0 12px;font-size:15px;font-weight:700;letter-spacing:-.01em}
button,.btn{background:var(--accent);color:#fff;border:1px solid var(--accent);
 border-radius:var(--r-s);padding:7px 13px;font:inherit;font-size:13.5px;font-weight:550;
 cursor:pointer;text-decoration:none;display:inline-block;line-height:1.4;
 transition:background .12s,border-color .12s,box-shadow .12s}
button:hover,.btn:hover{background:var(--accent-d);border-color:var(--accent-d)}
button:active,.btn:active{transform:translateY(.5px)}
/* 키보드로 옮겨 다닐 때 지금 어디인지 보여야 한다 (마우스 클릭에는 안 뜬다) */
button:focus-visible,.btn:focus-visible,input:focus-visible,select:focus-visible,
textarea:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button:disabled{opacity:.5;cursor:not-allowed}
.btn.sec,button.sec{background:var(--card);color:var(--txt2);border-color:var(--line)}
.btn.sec:hover,button.sec:hover{background:var(--bg);border-color:#d0d4da;color:var(--txt)}
button.danger,.btn.danger{background:#dc2626;border-color:#dc2626}
button.danger:hover,.btn.danger:hover{background:#b91c1c;border-color:#b91c1c}
/* 지우기처럼 **되돌릴 수 없는** 일은 눈에 띄되 손이 먼저 가면 안 된다.
   평소엔 조용히 있다가 손이 닿으면 빨갛게 찬다. */
button.ghost,.btn.ghost{background:var(--card);color:#c02626;border-color:#f3c9c9}
button.ghost:hover,.btn.ghost:hover{background:#dc2626;color:#fff;border-color:#dc2626}
/* 단추가 여럿 늘어서는 줄 */
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 14px}
.bar .muted{margin-left:2px}
table{border-collapse:collapse;width:100%;font-size:12.5px}
/* 칸마다 테두리를 두른다. 가로줄만 있으면 열이 여럿일 때 **어디까지가 한 칸인지**
   눈으로 자를 수가 없다 — 엑셀에서 표를 볼 때 격자를 켜는 이유와 같다.
   선은 진한 실선이다. 연한 선은 칸을 갈라 주지 못한다. */
th,td{border:1px solid var(--grid);padding:7px 9px;text-align:left;
 white-space:nowrap;max-width:260px;overflow:hidden;text-overflow:ellipsis}
/* 머리글은 줄바꿈을 허용한다. 안 그러면 '저널_주저자_수' 같은 긴 이름 하나가
   값은 한 글자뿐인 열을 통째로 넓혀 버린다. keep-all 은 한국어 낱말을 안 쪼갠다. */
th{background:var(--bg);position:sticky;top:0;white-space:normal;word-break:keep-all;
 line-height:1.3;vertical-align:bottom;font-size:11.5px;font-weight:650;color:var(--txt2);
 padding:8px 9px;border-bottom:1px solid var(--grid);z-index:1}
/* 열 성격에 맞춘 너비. 다 같게 하면 어떤 건 남고 어떤 건 모자란다. */
.w-xs{max-width:76px;min-width:52px}
.w-sm{max-width:96px;min-width:64px}
.w-md{max-width:150px;min-width:88px}
.w-lg{max-width:230px;min-width:130px}
.w-xl{max-width:380px;min-width:200px}
/* 표 안에서는 **줄을 바꾸지 않는다.** 한 줄이 길어지면 그 줄만 키가 커져서
   표가 들쭉날쭉해지고 눈이 줄을 못 따라간다. 넘치는 글은 … 으로 자르고,
   마우스를 올리면 전체가 뜬다(title). **내용은 그대로 있다** — 자르는 건
   보이는 것뿐이고, 복사·엑셀·검색은 원래 글을 쓴다. */
.scroll table td{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.scroll table td.ctl{overflow:visible}
/* 얼룩말 무늬 대신 **지금 보고 있는 줄**만 밝힌다. 눈이 가로로 따라가기 쉽고
   화면도 조용해진다. (자를 대고 읽던 걸 마우스가 대신한다) */
.scroll table tr:hover td{background:var(--accent-w)}
.scroll{overflow:auto;max-height:70vh;border:1px solid var(--line);border-radius:var(--r-s);
 background:var(--card)}
.flag{color:#c02626;font-weight:650}
.ok{color:#15803d}
.muted{color:var(--muted);font-size:12.5px}
/* 작업 결과 알림 — 화면 맨 위 띠가 아니라 오른쪽 위에 잠깐 떴다 사라진다.
   띠로 붙이면 아래 내용이 통째로 밀려서, 방금 고친 자리가 눈에서 사라진다.
   자리를 차지하지 않게 화면 위에 띄우고, 좁게 잡아 뒤를 가리지 않는다. */
#알림상자{position:fixed;top:58px;right:18px;z-index:900;display:flex;
 flex-direction:column;gap:8px;width:min(340px,42vw);pointer-events:none}
main .toast{display:none}          /* 제자리로 옮기기 전에는 안 보인다 */
#알림상자 .toast{display:block;pointer-events:auto;cursor:pointer;
 background:var(--card);border:1px solid var(--line);border-radius:var(--r-s);
 box-shadow:var(--sh-l);padding:11px 13px 11px 32px;font-size:13.5px;
 line-height:1.45;color:var(--txt);position:relative;word-break:break-word;
 opacity:0;transform:translateY(-6px);animation:토스트등장 .16s ease-out forwards}
#알림상자 .toast::before{content:'';position:absolute;left:12px;top:15px;
 width:9px;height:9px;border-radius:50%}
#알림상자 .toast.ok::before{background:#22c55e}
#알림상자 .toast.bad::before{background:#dc2626}
#알림상자 .toast.bad{border-color:#f3c9c9}
#알림상자 .toast.out{opacity:0;transform:translateY(-6px);
 transition:opacity .35s,transform .35s}
@keyframes 토스트등장{to{opacity:1;transform:none}}
@media (max-width:700px){#알림상자{width:auto;left:12px;right:12px;top:52px}}
.warn{background:#fffaeb;border:1px solid #fde68a;border-left:3px solid #f59e0b;
 padding:11px 14px;border-radius:var(--r-s);margin-bottom:14px;color:#7c4a03}
.done{background:#f0fdf4;border:1px solid #bbf7d0;border-left:3px solid #22c55e;
 padding:11px 14px;border-radius:var(--r-s);margin-bottom:14px;color:#14532d}
input[type=password],input[type=text],input[type=number],input[type=email],
input[type=search],select,textarea{padding:7px 10px;border:1px solid var(--line);
 border-radius:var(--r-s);font:inherit;font-size:13.5px;background:var(--card);
 color:var(--txt);transition:border-color .12s,box-shadow .12s}
input[type=text]:focus,input[type=password]:focus,input[type=number]:focus,
select:focus,textarea:focus{border-color:var(--accent);
 box-shadow:0 0 0 3px var(--accent-w);outline:none}
input::placeholder{color:#aeb4bd}
.login{max-width:360px;margin:14vh auto}
.pill{padding:2px 9px;border-radius:99px;font-size:11px;font-weight:650;
 display:inline-block;line-height:1.7}
.p-미분류{background:#fee2e2;color:#b91c1c}
.p-내부{background:#e0e7ff;color:#3730a3}
.p-처리중{background:#dbeafe;color:#1d4ed8}
.p-완료{background:#dcfce7;color:#15803d}
.p-검토필요{background:#fef3c7;color:#92400e}
.p-실패{background:#fee2e2;color:#b91c1c}
.p-중복의심{background:#ffe4e6;color:#9f1239}
.p-대기중{background:#e5e7eb;color:#374151}
.p-겹침{background:#fef3c7;color:#92400e}
.dup{background:#fff1f2}
tr.grouphead td{background:#eef2ff;border-top:2px solid #c7d2fe}
/* 검토가 필요한 줄. 색만으로 알리지 않고 배지도 같이 붙는다. */
tr.needs th,tr.needs td{background:#fffbeb}
tr.needs th{border-left:3px solid #f59e0b}
tr.needs td:first-child{border-left:3px solid #f59e0b}
.p-안본것{background:#fef3c7;color:#92400e}
td.edit{cursor:cell}
td.edit:hover{outline:2px solid var(--accent);outline-offset:-2px}
td.saved{background:#dcfce7 !important}
td.fxcol{color:#4b5563;font-style:italic}
td.err{background:#fee2e2 !important}
td.edit input,td.edit select{padding:2px 4px;font-size:12.5px;width:100%}
td.ctl,th.ctl{white-space:normal;max-width:none;overflow:visible}
/* 고르는 칸이 긴 항목 이름만큼 늘어나 표를 밀어내지 않게 한다 */
td.ctl select{max-width:180px}
td.ctl input[type=text]{max-width:220px}
.mergebar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;background:#eff6ff;
 border:1px solid #bfdbfe;border-radius:var(--r-s);padding:10px 12px;margin:0 0 12px}
.mergebar select{min-width:280px;max-width:100%}
.mergebar b{color:#1d4ed8}
tr.hide{display:none}
input.dirty,select.dirty{background:#fef3c7;border-color:#fcd34d}
.tbar{display:flex;gap:8px;align-items:center;margin:0 0 8px}
.tbar input.tfilter{width:220px}
th.sortable{cursor:pointer;user-select:none}
th.sortable:hover{background:#dbeafe}
th[data-dir=asc]::after{content:' ↑';font-size:11px;color:var(--accent)}
th[data-dir=desc]::after{content:' ↓';font-size:11px;color:var(--accent)}
th.filtered{background:#dbeafe}
th.filtered::after{content:' (추림)';font-size:10px;color:var(--accent)}
#colmenu{position:absolute;z-index:100;background:#fff;border:1px solid var(--line);
 border-radius:var(--r);box-shadow:var(--sh-l);padding:6px;min-width:230px;
 max-width:320px;font-size:13px}
#colmenu .cm-head{font-weight:700;padding:4px 8px;color:var(--muted);
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#colmenu button{display:block;width:100%;text-align:left;background:none;color:var(--txt);
 padding:6px 8px;border-radius:var(--r-s);font-size:13px}
#colmenu button:hover{background:#eff6ff}
#colmenu .cm-btns{display:flex;gap:6px;padding:4px 0}
#colmenu .cm-btns button{background:var(--accent);color:#fff;text-align:center}
#colmenu .cm-btns button.sec{background:var(--txt2)}
#colmenu .cm-sep{border-top:1px solid var(--line);margin:5px 0}
#colmenu .cm-title{font-weight:700;padding:2px 8px}
#colmenu .cm-q{width:100%;margin:4px 0;padding:5px 7px;font-size:13px}
#colmenu .cm-list{max-height:200px;overflow:auto;border:1px solid var(--line);border-radius:var(--r-s)}
#colmenu .cm-row{display:block;padding:3px 8px;cursor:pointer;white-space:nowrap;
 overflow:hidden;text-overflow:ellipsis}
#colmenu .cm-row:hover{background:#eff6ff}
#colmenu .cm-row.hide{display:none}
#colmenu .cm-allrow{padding-left:8px}
td.sel{background:#bfdbfe !important;outline:1px solid #2563eb;outline-offset:-1px}
/* 열 순서 끌기 */
#colorder tr[data-col]{cursor:default}
#colorder td.grip{white-space:nowrap;cursor:grab;user-select:none}
#colorder td.grip .griph{color:#b6bcc5;font-size:15px;letter-spacing:-2px}
#colorder tr.dragging{opacity:.45}
#colorder tr.dropmark td{box-shadow:inset 0 2px 0 var(--accent)}
button.tiny{padding:1px 6px;font-size:12px;min-width:22px;line-height:1.3}
#colform button.dirty{background:#b45309;border-color:#b45309}
/* 수식 미리보기 — 친 대로 바로 아래에 결과가 뜬다 */
/* 수식 자동완성 — 치는 대로 열 이름·함수를 좁혀 보여준다.
   열 이름을 외우고 있어야 쓸 수 있는 도구는 아무도 안 쓴다. */
#fxdrop{position:absolute;z-index:120;background:var(--card);
 border:1px solid var(--line);border-radius:var(--r-s);box-shadow:var(--sh-l);
 padding:4px;max-height:260px;overflow:auto;min-width:220px;font-size:13px}
#fxdrop .it{display:flex;gap:8px;align-items:baseline;padding:5px 9px;
 border-radius:5px;cursor:pointer;white-space:nowrap}
#fxdrop .it:hover,#fxdrop .it.on{background:#eff6ff}
#fxdrop .it b{font-weight:650;color:var(--txt)}
#fxdrop .it i{font-style:normal;font-size:11px;color:var(--muted);margin-left:auto}
#fxdrop .it .desc{color:var(--muted);font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:320px}
#fxdrop .foot{padding:3px 9px;color:var(--muted);font-size:11px;border-top:1px solid var(--line,#e5e7eb)}
a.person{color:inherit;text-decoration:underline dotted;text-underline-offset:3px}
a.person:hover{color:var(--accent)}
a.person.plink{font-size:12px;font-weight:400;margin-left:6px;text-decoration:none;color:var(--accent)}
.blockout table[data-wblock] th[data-wkey]{position:relative}
.blockout table[data-wblock] th.wgrab{cursor:col-resize;box-shadow:inset -3px 0 0 var(--accent)}
body.wdragging,body.wdragging *{cursor:col-resize !important;user-select:none}
#fxsig{position:absolute;z-index:121;background:#fffbe6;border:1px solid #e9d98a;border-radius:6px;
padding:6px 10px;font-size:12.5px;max-width:620px;box-shadow:0 4px 14px rgba(0,0,0,.12);pointer-events:none}
#fxsig .sig{font-family:Consolas,'D2Coding',monospace}
#fxsig .sig .fn{font-weight:700;color:#1d4ed8}
#fxsig .sig b{background:#fde68a;border-radius:3px;padding:0 2px}
#fxsig .what{color:#374151;margin-top:2px}
#fxsig .ex{color:var(--muted);margin-top:2px}
#fxsig .ex code{font-size:12px}
#fxdrop .head{padding:4px 9px;color:var(--muted);font-size:11.5px}
.fxout{display:block;font-size:12px;margin-top:3px;min-height:16px;word-break:break-all;white-space:pre-line}
/* 대시보드 표 모양 — 만드는 사람이 고른다 */
table.dtbl th{background:var(--headbg,var(--bg))}
/* 격자는 **진한 검정 실선**이다. 연한 선은 칸을 갈라 주지 못한다 —
   격자를 켜는 이유가 칸 구분이니, 흐리면 켜는 의미가 없다. */
table.dtbl.b-grid th,table.dtbl.b-grid td{border:1px solid #222}
table.dtbl.b-row th,table.dtbl.b-row td{border:0;border-bottom:1px solid var(--line2)}
table.dtbl.b-row th{border-bottom:1px solid var(--line)}
table.dtbl.b-none th,table.dtbl.b-none td{border:0}
table.dtbl.b-none th{border-bottom:1px solid var(--line)}
/* 내용에 맞춤 — 칸을 억지로 줄이지 않는다. 넘치면 **가로로 스크롤**한다.
   (칸이 적으면 허전하지 않게 최소한 화면 폭은 채운다) */
table.dtbl.fit{width:auto;min-width:100%}
/* 한 칸이 통째로 화면을 잡아먹지 않게 상한만 둔다 (직접 정한 너비가 이긴다).
   상한이 있어도 열이 많으면 합이 화면을 넘어 가로 스크롤이 걸린다. */
table.dtbl.fit th,table.dtbl.fit td{max-width:420px}
/* 열 너비를 **전부** 정한 표. 폭은 그 합이고, 화면보다 넓으면 가로로 스크롤한다.
   - table-layout:fixed 여야 정한 대로 선다. auto 면 브라우저가 내용 길이를
     보고 제멋대로 다시 나눠서, 열을 넓혀도 옆 열이 그만큼 줄어들 뿐이다.
   - min-width:100% 를 풀어야 카드 폭에 안 묶인다 (바로 이것이 «전체 너비가
     고정» 으로 보이던 까닭이다).
   - 상한(260/420px)도 푼다. 정한 너비가 상한에 걸려 잘리면 정한 뜻이 없다. */
table.dtbl.fixed{table-layout:fixed;min-width:0}
table.dtbl.fixed th,table.dtbl.fixed td{max-width:none}
/* --- 시트 ---------------------------------------------------------------
   엑셀처럼 칸마다 서식을 건다. 칸 스타일이 인라인으로 붙으므로 여기서는
   격자와 머리글(A·B·1·2)만 만든다. 표 폭은 열 너비의 합이다. */
a.drill{color:inherit;text-decoration:underline dotted;text-underline-offset:3px}
a.drill:hover{color:var(--accent,#2f6fd0)}
details.blockout{margin-top:12px;border-top:1px dashed var(--line);padding-top:8px}
details.blockout .card{box-shadow:none;border:1px dashed var(--line);margin:8px 0 0}
details.pcard{border:1px solid var(--line);border-radius:8px;padding:8px 14px;
  margin-bottom:8px}
details.pcard>summary{font-weight:800;cursor:pointer;padding:2px 0}
details.pcard[open]>summary{margin-bottom:6px}
table.sheet{border-collapse:collapse;table-layout:fixed;max-width:none}
table.sheet th,table.sheet td{border:1px solid #d6dbe3;padding:4px 6px;
  max-width:none;white-space:normal;vertical-align:middle}
table.sheet th{background:#f1f4f8;color:#5b6472;font-weight:600;text-align:center;
  font-size:12px;user-select:none;overflow:hidden}
table.sheet td{height:26px;overflow:hidden;overflow-wrap:anywhere}
table.sheet th.corner{width:44px}
/* 머리글(A·B·1·2)은 칸과 맞닿은 변을 **양보한다** (none 은 겹친 선 다툼에서 늘 진다).
   안 그러면 표 맨 위·맨 왼쪽 칸에 그은 테두리가 머리글의 연한 선에 져서 지워졌다. */
table.sheet th[data-col],table.sheet th.corner{border-bottom:none}
table.sheet th[data-row],table.sheet th.corner{border-right:none}
/* 보기에서 격자 숨김 — 내가 그은 테두리(인라인 style)만 남는다 */
table.sheet.plain td{border:1px solid transparent}
table.sheet.editing td.outview{opacity:.45;background-image:repeating-linear-gradient(45deg,transparent 0 6px,rgba(100,116,139,.08) 6px 12px)}
table.sheet.editing th[data-col],table.sheet.editing th[data-row]{position:relative}
table.sheet.editing th[data-col]::after{content:'';position:absolute;right:-3px;top:0;
  width:6px;height:100%;cursor:col-resize;z-index:2}
table.sheet.editing th[data-row]::after{content:'';position:absolute;left:0;bottom:-3px;
  height:6px;width:100%;cursor:row-resize;z-index:2}
table.sheet.editing td.refpick{outline:2px dashed #16a34a;outline-offset:-2px}
.sheetstate{font-size:12px}
.sheetstate.dirty{color:#b45309;font-weight:700}
/* 편집 중에만: 고른 칸을 파랗게. outline 이라 칸 크기가 안 흔들린다. */
table.sheet.editing td{cursor:cell}
table.sheet.editing td{position:relative}
table.sheet.editing td.picked{outline:2px solid #2f6fd0;outline-offset:-2px}
table.sheet.editing td.anchor{outline-width:3px}
/* 끌어서 채우는 손잡이. 고른 네모의 오른쪽 아래 귀퉁이에 붙는다 (엑셀과 같다). */
table.sheet.editing td .fillgrip{position:absolute;right:-4px;bottom:-4px;
  width:9px;height:9px;background:#2f6fd0;border:1px solid #fff;border-radius:2px;
  cursor:crosshair;z-index:3}
table.sheet.editing td.filling{outline:2px dashed #2f6fd0;outline-offset:-2px;
  background:rgba(47,111,208,.07)}
.sheetbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;
  padding:8px;border:1px solid #e3e7ee;border-radius:8px;background:#fbfcfe;
  margin-bottom:8px}
.sheetbar button,.sheetbar select,.sheetbar input{font-size:13px}
.sheetbar input[type=color]{width:34px;height:26px;padding:0;border:1px solid #ccd3dd}
.sheetbar .sep{width:1px;height:20px;background:#dde2ea;margin:0 2px}
.sheetfx{width:100%;font-family:'D2Coding','Consolas',monospace;resize:none;
  overflow:hidden;line-height:1.45;min-height:34px;display:block}
/* 칸만 고른 동안(옮기는 중) — 수식칸이 키보드를 받지만 고른 글이 파랗게 보이면
   «적는 중» 처럼 보인다. 칠과 커서를 감춘다. 치기 시작하면 원래대로. */
.sheetfx.nav{caret-color:transparent}
.sheetfx.nav::selection{background:transparent}
table.dtbl.zebra tr:nth-child(even) td{background:#fafbfc}
/* 조건서식으로 칠한 칸은 얼룩말도 hover 도 덮지 않는다 — 일부러 칠한 것이다.
   (인라인 스타일이라 이 규칙들보다 우선하지만, 명시해 두어야 나중에 규칙을
    하나 더 얹어도 안 깨진다) */
table.dtbl.zebra tr:nth-child(even) td.painted{background:none}
.scroll table.dtbl tr:hover td.painted{background:none}
table.dtbl.zebra tr:hover td{background:var(--accent-w)}
table.dtbl.tight th,table.dtbl.tight td{padding:3px 6px;font-size:12px}
/* CHAR(10) 을 넣은 칸만 줄을 바꾼다. 나머지는 한 줄로 잘린 채 둔다 */
.scroll table td.multi{white-space:normal;overflow:visible;text-overflow:clip;line-height:1.45}
.fxout.fxok{color:#15803d}
input.fx{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
.rt{border:1px solid var(--line);border-radius:var(--r);overflow:hidden;background:#fff}
.rt-bar{display:flex;flex-wrap:wrap;gap:2px;align-items:center;padding:6px;
 background:#f3f4f6;border-bottom:1px solid var(--line)}
.rt-bar button{background:#fff;color:var(--txt);border:1px solid var(--line);
 padding:4px 7px;font-size:13px;min-width:29px;border-radius:5px}
.rt-bar button:hover{background:#eff6ff;border-color:var(--accent)}
.rt-bar button.rt-drop{display:inline-flex;align-items:center;gap:4px;justify-content:space-between}
.rt-bar button.rt-drop span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rt-bar button.rt-drop i{font-style:normal;font-size:9px;color:var(--muted)}
.rt-bar button.rt-var{background:var(--accent);color:#fff;border-color:var(--accent)}
.rt-bar button.rt-var i{color:#fff}
.rt-bar .rt-sep{width:1px;height:20px;background:var(--line);margin:0 4px}
.rt-bar .rt-ink{display:inline-block;padding:0 3px;border-radius:3px;font-weight:700}
.rt-bar label.btnlike{display:inline-flex;align-items:center;font-size:13px;
 color:var(--txt);border:1px solid var(--line);border-radius:5px;padding:4px 7px;
 background:#fff;cursor:pointer}
.rt-bar label.btnlike:hover{background:#eff6ff;border-color:var(--accent)}
.rt-body{min-height:340px;max-height:60vh;overflow:auto;padding:16px 18px;
 font:12pt/1.7 "맑은 고딕",system-ui,sans-serif;outline:none}
.rt-body:focus{box-shadow:inset 0 0 0 2px #bfdbfe}
/* 메일 본문 표는 **지원자 표가 아니다.** 위쪽 th,td 규칙(260px 상한, 한 줄로
   자르기)은 화면의 데이터 표를 위한 것이라, 그게 편집기까지 죄면 열 너비를
   아무리 잡아도 260px 에서 멈춘다. 여기서는 푼다. */
.rt-body table td,.rt-body table th,
.mailbody table td,.mailbody table th{
 max-width:none;min-width:0;white-space:normal;overflow:visible;text-overflow:clip;
 border:0}
.rt-body table{width:auto}
.rt-body img{max-width:100%}
/* 열 경계에 마우스를 대면 끌 수 있다는 걸 알려 준다 */
.rt-body table{cursor:auto}
#rtdrop{position:absolute;z-index:120;background:#fff;border:1px solid var(--line);
 border-radius:var(--r);box-shadow:var(--sh-l);padding:5px;font-size:13px;
 max-height:340px;overflow:auto;min-width:120px}
#rtdrop > button{display:block;width:100%;text-align:left;background:none;border:0;
 color:var(--txt);padding:5px 9px;border-radius:5px;font-size:13px;cursor:pointer}
#rtdrop > button:hover{background:#eff6ff}
#rtdrop .rt-swatch{display:grid;grid-template-columns:repeat(5,22px);gap:4px;padding:4px}
#rtdrop .rt-swatch button{width:22px;height:22px;border:1px solid var(--line);
 border-radius:4px;padding:0;cursor:pointer}
#rtdrop .rt-pick{display:flex;align-items:center;gap:6px;padding:6px 6px 2px;
 color:var(--muted);border-top:1px solid var(--line);margin-top:4px;cursor:pointer}
#rtdrop .rt-grid{display:grid;grid-template-columns:repeat(10,14px);gap:3px;padding:5px}
#rtdrop .rt-grid i{width:14px;height:13px;border:1px solid var(--line);border-radius:2px;
 background:#fff;cursor:pointer}
#rtdrop .rt-grid i.on{background:#bfdbfe;border-color:var(--accent)}
#rtdrop .rt-gridlabel{text-align:center;color:var(--muted);padding:2px 0 4px}
#rtdrop .rt-gridmore{border-top:1px solid var(--line);padding:6px;color:var(--muted);
 display:flex;align-items:center;gap:4px;white-space:nowrap}
#rtdrop .rt-gridmore input{padding:3px 4px;font-size:12px}
#rtdrop .rt-gridmore button{width:auto;display:inline-block;background:var(--accent);
 color:#fff;border:0;border-radius:5px;padding:4px 9px;cursor:pointer}
/* 표 도구 — 커서가 표 안에 있을 때만 뜬다 */
.rt-tablebar{background:#eff6ff;border-bottom:1px solid #bfdbfe}
.rt-tablebar[hidden]{display:none}
.rt-bar .rt-lbl{display:inline-flex;align-items:center;gap:4px;font-size:12.5px;
 color:var(--muted);padding:0 2px}
.rt-bar .rt-lbl input[type=number],.rt-bar .rt-lbl select{padding:3px 5px;font-size:12.5px}
/* 편집기 안에서만 보이는 표 눈금. 메일에는 안 나간다 (인라인 스타일이 아니다) */
.rt-body table td:empty::after{content:'\00a0'}
#rtdrop.varmenu{width:280px}
#rtdrop .vm-head{padding:4px 8px;color:var(--muted)}
#rtdrop .vm-q{width:100%;margin:4px 0;padding:6px 8px;font-size:13px}
#rtdrop .vm-list{max-height:260px;overflow:auto}
#rtdrop .vm-group{font-weight:700;color:var(--accent);padding:8px 8px 3px;
 border-top:1px solid var(--line);margin-top:4px}
#rtdrop .vm-group:first-child{border-top:0;margin-top:0}
#rtdrop .vm-item{display:block;width:100%;text-align:left;background:none;
 color:var(--txt);padding:5px 8px;border-radius:var(--r-s);font-size:13px}
#rtdrop .vm-item:hover{background:#eff6ff}
#rtdrop .hide{display:none}
/* 보내야 하는 때 — 단계마다 한 줄. 열일곱 개를 한 줄로 늘어놓으면 못 훑는다. */
.whenrow{display:flex;align-items:center;gap:2px 4px;flex-wrap:wrap;padding:2px 0}
.whenname{display:inline-block;min-width:76px;color:var(--muted);font-size:12.5px}
/* 표 안에서 여는 여러 줄 입력칸 */
textarea.cellbox{width:100%;min-width:240px;resize:vertical;font:inherit}
.cellhint{color:var(--muted);font-size:11px;margin-top:2px}
.mailbody{border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;
 background:#fff;max-height:420px;overflow:auto;font:12pt/1.7 "맑은 고딕",sans-serif}
.mailbody img{max-width:100%}
pre.rubric{background:var(--bg);border:1px solid var(--line);border-radius:var(--r-s);padding:10px 12px;font-size:12px;white-space:pre-wrap;margin:8px 0 0;color:var(--muted)}
#toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:#16191d;
 color:#fff;padding:11px 18px;border-radius:var(--r);opacity:0;pointer-events:none;
 transition:opacity .15s,transform .15s;z-index:99;box-shadow:var(--sh-l);font-size:13.5px}
#toast.show{opacity:1;transform:translateX(-50%) translateY(-2px)}
"""


#: 지금 처리 중인 요청 경로. 어느 탭에 불을 켤지 정하는 데만 쓴다.
#: _page 를 부르는 곳이 마흔 군데라 인자를 하나 더 받게 하는 대신 여기 둔다.
#: 요청마다 스레드가 따로라 값이 섞이지 않는다.
현재경로: contextvars.ContextVar[str] = contextvars.ContextVar("현재경로", default="")

#: (라벨, 주소, 이 탭에 속하는 경로들, 하위 목록, 볼 수 있나)
#: 소속 경로를 적어 두는 이유: 지원자 상세(/candidate)는 탭이 아니지만
#: 인재 Pool 에서 들어간 화면이라 그 탭에 불이 켜져 있어야 한다.
#:
#: 하위 목록은 **탭에 마우스를 올리면 펴지는 것**이다. 갈 곳이 둘인데 눌러
#: 들어가서 다시 고르게 하면 걸음이 하나 더 든다 (부서·과제가 그랬다).
#: 하위가 없는 탭은 빈 튜플이라 지금까지와 똑같이 동작한다.
def _탭들(me: User | None, badge: str,
        메일배지: str = "") -> list[tuple[str, str, tuple[str, ...],
                                       tuple[tuple[str, str], ...]]]:
    학회 = "/names?kind=" + urllib.parse.quote("학회·저널")
    후보 = [
        # 일이 흘러가는 순서대로: 넣고 → 보고 → 뽑고 → 들여다본다
        ("지원자 추가", "/upload", ("/upload",), (), can(me, "지원자_등록")),
        ("인재 Pool", "/", ("/", "/candidate", "/attachment", "/export.xlsx"),
         (), can(me, "지원자_목록")),
        ("채용 현황", "/recruit", ("/recruit",), (),
         can(me, "채용현황_수정") or can(me, "지원자_조회")),
        (f"메일{메일배지}", "/mail", ("/mail",),
         (("메일 템플릿 관리", "/mail"),
          (f"메일 발송이력{메일배지}", "/mail/log")),
         can(me, "메일_템플릿")),
        ("대시보드", "/dash", ("/dash",), (), can(me, "대시보드_조회")),
        (f"명칭 관리{badge}", 학회, ("/names",), (), can(me, "명칭_관리")),
        # 과제 파일 관리는 이 아래 하위 화면으로 들어갔다 (/match/*)
        ("부서·과제", "/org", ("/org", "/match"),
         (("부서·과제 편집", "/org/edit"), ("과제 정보 관리", "/match")),
         can(me, "부서과제_관리")),
        ("계정", "/users", ("/users",), (), can(me, "계정_현업추가")),
        ("표 항목", "/fields", ("/fields",), (), can(me, "열_구성")),
        ("변경 이력", "/history", ("/history",), (), can(me, "변경이력_조회")),
    ]
    return [(라벨, 주소, 소속, 하위)
            for 라벨, 주소, 소속, 하위, 보임 in 후보 if 보임]


def _지금탭(경로: str, 소속: tuple[str, ...]) -> bool:
    """이 경로가 그 탭에 속하나.

    '/' 는 정확히 같을 때만이다. 안 그러면 모든 화면이 인재 Pool 이 된다.
    """
    for base in 소속:
        if base == "/":
            if 경로 == "/":
                return True
        elif 경로 == base or 경로.startswith(base + "/"):
            return True
    return False


def _알림(msg: str = "", err: str = "") -> str:
    """작업 결과 알림.

    예전에는 화면 맨 위에 띠로 붙였다. 그러면 아래 내용이 통째로 밀려 내려가
    **방금 고친 자리가 눈에서 사라진다.** 알림은 결과를 알려주는 것뿐이라
    자리를 뺏을 이유가 없다. 오른쪽 위에 잠깐 띄우고 저절로 없앤다.

    여기 쓰는 건 **한 번 하고 끝나는 일의 결과**뿐이다(저장했습니다 · 지웠습니다).
    화면에 계속 붙어 있어야 하는 안내 — 연습 모드입니다, 검토가 필요한 CV 가
    3건 있습니다 — 는 띠 그대로 둔다. 10초 뒤에 사라지면 안 되는 글이다.
    """
    조각 = [f"<div class='toast {종류}' role='status'>{html.escape(글)}</div>"
          for 글, 종류 in ((msg, "ok"), (err, "bad")) if 글]
    return "".join(조각)


def _page(title: str, body: str, nav: bool = True, me: User | None = None,
          폭: str = "") -> bytes:
    # 탭 옆 숫자 = **아직 사람이 안 본 표기 수.** 등급을 안 매긴 것만 세면
    # 소속·전공은 늘 0 이라, 학교 이름이 엉뚱하게 들어와도 아무 표시가 없었다.
    안본것 = registry.unconfirmed_count() if nav else 0
    badge = f' <span class="pill p-안본것">{안본것}</span>' if 안본것 else ""
    # 메일 탭 옆 숫자 = **보냈어야 하는데 안 보낸 메일 수.** 화면을 열 때마다
    # 세지만, 보내야 하는 때를 정해 둔 템플릿이 없으면 아예 세지 않는다.
    안보낸 = sum(_안보낸수(me).values()) if nav else 0
    메일배지 = f' <span class="pill p-안본것">{안보낸}</span>' if 안보낸 else ""
    경로 = 현재경로.get()
    켜진것 = ""
    for _라벨, 주소, 소속, _하위 in _탭들(me, badge, 메일배지):
        if _지금탭(경로, 소속):
            켜진것 = 주소
            break
    링크 = []
    for 라벨, 주소, _소속, 하위 in _탭들(me, badge, 메일배지):
        불 = " class=on" if 주소 == 켜진것 else ""
        본체 = f"<a href='{주소}'{불}>{라벨}</a>"
        if not 하위:
            링크.append(본체)
            continue
        # 탭 자체를 눌러도 첫 화면으로 간다. 마우스를 못 쓰는 상황에서
        # 하위가 안 펴진다고 아예 못 들어가는 자리가 되면 안 된다.
        폄 = "".join(f"<a href='{ㅈ}'>{ㄹ}</a>" for ㄹ, ㅈ in 하위)
        링크.append(f"<span class='tab'>{본체}<span class='sub'>{폄}</span></span>")
    누구 = (
        f"<span class='who'>{html.escape(me.이름)}"
        f"<b>{html.escape(me.역할)}</b></span>"
        if me else ""
    )
    header = (
        "<header><span class='brand'>지원자 관리</span>" + "".join(링크)
        + f"<span class='sp'></span>{누구}<a href='/logout'>로그아웃</a></header>"
        if nav
        else ""
    )
    return (
        f"<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body{f' style=--mainw:{폭}' if 폭 else ''}>{header}<main>{body}</main>"
        + (f"<script>{_TABLE_JS}{_INLINE_JS}</script>" if nav else "")
        + "</body></html>"
    ).encode("utf-8")


def _status_table() -> str:
    """업로드 처리 현황.

    검토 필요로 끝난 줄에서는 **바로 그 지원자 상세로 갈 수 있어야 한다.**
    예전에는 여기서 '검토 필요' 라고만 알려주고, 사람은 인재 Pool 로 가서
    이름을 찾아 들어가야 했다.
    """
    with _status_lock:
        items = list(_status.items())
    if not items:
        return ""
    끝낸것 = store.review_done_map()
    rows = []
    검토수 = 0
    for n, s in items:
        cid = s.get("cid") or ""
        rec = store.get(cid) if cid else None
        남은 = review.remaining(rec.검토_사유, 끝낸것.get(cid, set())) if rec else []
        할일 = ""
        if rec is not None and 남은:
            검토수 += 1
            보임 = " · ".join(review.short(x, 40) for x in 남은[:2])
            더 = f" 외 {len(남은) - 2}건" if len(남은) > 2 else ""
            할일 = (
                f"<a class='btn' href='/candidate?id={urllib.parse.quote(cid)}#검토'>"
                f"검토 {len(남은)}건 →</a>"
                f"<div class='muted' style='white-space:normal;margin-top:3px'>"
                f"{html.escape(보임)}{더}</div>"
            )
        elif rec is not None:
            할일 = (f"<a class='btn sec' href='/candidate?id={urllib.parse.quote(cid)}'>"
                  "상세</a>")
        rows.append(
            f"<tr><td>{html.escape(n)}</td>"
            f"<td><span class='pill p-{s['state']}'>{s['state']}</span></td>"
            f"<td title='{html.escape(s.get('message',''))}'>"
            f"{html.escape(s.get('message',''))}</td>"
            f"<td>{s['시각']}</td>"
            f"<td class='ctl'>{할일}</td></tr>"
        )
    처리중 = any(s["state"] in ("대기중", "처리중") for _, s in items)
    안내 = (
        f"<p class='warn'>검토가 필요한 CV 가 <b>{검토수}건</b> 있습니다. "
        "오른쪽 <b>검토 N건 →</b> 을 누르면 그 지원자의 검토 항목으로 바로 "
        "갑니다.</p>" if 검토수 else ""
    )
    # **페이지를 통째로 새로고침하지 않는다.** 예전에는 <meta refresh> 로 5초마다
    # 다시 그렸는데, 그러면 분석이 도는 동안 파일을 고르는 순간 새로고침이
    # 끼어들어 <input type=file> 선택이 날아갔다 ("첨부가 안 된다"). 지금은
    # 이 표 안쪽만 갈아 끼우므로 고르던 파일도, 스크롤도 그대로 있다.
    상태 = ("<span class='live'>● 처리 중</span> "
          "<span class='muted'>표만 3초마다 갱신됩니다. 파일을 고르는 중이어도 "
          "선택이 풀리지 않습니다.</span>" if 처리중 else "")
    return (
        f"<div class='card'><h2>업로드 처리 현황</h2>{안내}"
        f"<p id='상태알림' data-busy='{'1' if 처리중 else ''}'>{상태}</p>"
        "<div class='scroll'><table data-name='처리 현황' id='현황표'>"
        "<tr><th>파일</th><th>상태</th><th>메모</th><th>시각</th><th>할 일</th></tr>"
        + "".join(rows) + "</table></div>"
        + "<p><form method='post' action='/status/clear' style='display:inline'>"
        "<button type='submit' class='sec'>현황 지우기</button></form>"
        "<span class='muted'> 이 목록만 비웁니다. 지원자는 지워지지 않습니다.</span></p>"
        + "</div>"
    )


# ---------------------------------------------------------------------------
# 표에서 바로 고치기 (칸을 눌러 편집)
# ---------------------------------------------------------------------------
#: 칸을 누르면 입력칸으로 바뀌고, Enter/포커스아웃에 /api/cell 로 저장한다.
#: 상세 화면과 같은 검사·같은 이력을 타므로 규칙이 갈라지지 않는다.
#: 페이지를 새로 그리지 않아 넓은 표에서 스크롤 위치가 유지된다.
_INLINE_JS = _정적JS("inline_edit.js")


# ---------------------------------------------------------------------------
# 표 공통 기능 — 정렬 · 찾기 · 엑셀처럼 범위 복사 · 엑셀 내려받기
# ---------------------------------------------------------------------------
#: 페이지마다 따로 만들지 않는다. `.scroll` 안의 표를 찾아 한 번에 붙인다.
#: 범위 복사는 숨은 textarea 에 TSV 를 넣고 선택해 두는 방식이다.
#: 사내망은 https 가 아니라 navigator.clipboard 를 못 쓰는 경우가 있어서,
#: 브라우저가 자체적으로 처리하는 Ctrl+C 가 가장 확실하다.
_TABLE_JS = _정적JS("table.js")

def 홈(me: User | None) -> str:
    """이 사람이 처음 볼 화면. 현업은 채용 현황이 홈이다."""
    return "/" if can(me, "지원자_목록") else "/recruit"


def _busy_count() -> int:
    with _status_lock:
        return sum(1 for s in _status.values() if s["state"] in ("대기중", "처리중"))


#: 없는 주소, 또는 주소별 처리 함수가 아무것도 돌려주지 않고 끝났을 때 (404).
def _없는주소(self):
    return self._send(_page("없음", "<div class='card'>페이지가 없습니다.</div>"), code=404)


def 함수설명자료(이름들) -> dict:
    """수식 안내가 쓸 함수 설명 {이름: [인자들, 되풀이, 설명, 예]} (`funcdocs.py`)."""
    from ..funcdocs import 함수설명

    return {n: list(함수설명[n]) for n in 이름들 if n in 함수설명}

