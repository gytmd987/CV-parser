
function 시트편집기(칸){
  var 숨은 = 칸.querySelector('input.sheetdata');
  var 격자 = 칸.querySelector('.sheetgrid');
  var 수식칸 = 칸.querySelector('.sheetfx');
  var 폼 = 칸.querySelector('form.sheetform');
  var 상태 = 칸.querySelector('.sheetstate');
  if(!숨은 || !폼) return;
  var bid = 폼.querySelector("input[name='id']").value;
  var 모델 = JSON.parse(숨은.value || '{}');
  모델.칸 = 모델.칸 || {}; 모델.열너비 = 모델.열너비 || {}; 모델.행높이 = 모델.행높이 || {};
  var 기준 = null, 끝 = null, 고른것 = [];
  var 되돌리기 = [], 다시하기 = [];
  var 편집전 = null;            /* 수식칸에 적기 시작할 때의 글 (Esc 로 되돌린다) */
  var 넣은참조 = null;           /* 수식을 적다 칸을 눌러 넣은 참조 자리 {s, e, 처음} */

  function 표(){ return 칸.querySelector('table.sheet'); }
  function 담기(){ 숨은.value = JSON.stringify(모델); }
  function 칸값(a){ return 모델.칸[a] || (모델.칸[a] = {}); }
  function 비었나(a){ var v = 모델.칸[a]; return v && Object.keys(v).length === 0; }
  function 자리(a){
    var m = /^([A-Z]+)([0-9]+)$/.exec(a);
    var c = 0, 글 = m[1];
    for(var i=0;i<글.length;i++) c = c*26 + (글.charCodeAt(i)-64);
    return {r: +m[2]-1, c: c-1};
  }
  function 열글(c){
    var 글 = '', n = c+1;
    while(n > 0){ var 나머지 = (n-1) % 26; 글 = String.fromCharCode(65+나머지) + 글;
                  n = ((n-1-나머지)/26)|0; }
    return 글;
  }
  function 주소(r,c){ return 열글(c) + (r+1); }
  function 칸태그(a){ return 표().querySelector("td[data-cell='"+a+"']"); }

  /* -- 바뀜 표시 · 되돌리기 ------------------------------------------------ */
  function 바뀜(){
    if(상태){ 상태.textContent = '저장 안 한 변경이 있습니다'; 상태.classList.add('dirty'); }
  }
  function 기록(){
    되돌리기.push(JSON.stringify(모델));
    if(되돌리기.length > 100) 되돌리기.shift();
    다시하기 = [];
  }
  function 되감기(앞으로){
    var 꺼낼 = 앞으로 ? 다시하기 : 되돌리기, 넣을 = 앞으로 ? 되돌리기 : 다시하기;
    if(!꺼낼.length) return;
    넣을.push(JSON.stringify(모델));
    모델 = JSON.parse(꺼낼.pop());
    담기(); 바뀜(); 다시계산();
  }

  /* -- 서식 칠하기 (서버의 `시트_칸스타일` 과 같은 규칙) ---------------------- */
  var 글꼴표 = {'고딕':"'맑은 고딕','Malgun Gothic',sans-serif",
              '명조':"'바탕','Batang',serif",
              '고정폭':"'D2Coding','Consolas',monospace"};
  var 선표 = {'얇게':'1px solid','굵게':'2px solid','점선':'1px dashed','이중':'3px double'};
  var 변표 = {'위':'Top','아래':'Bottom','왼쪽':'Left','오른쪽':'Right'};
  /* 테두리는 **두 칸 사이의 선**이다 (서버의 `시트_테두리선` 과 같은 규칙).
     칸마다 제 테두리만 그리면 겹친 선에서 왼쪽·위 칸이 이겨, 오른쪽·아래 칸에
     그은 위·왼쪽 테두리가 연한 격자선에 가려 사라졌다. 선으로 모아 두고 양쪽
     칸이 같은 선을 그린다. 병합된 칸은 덮는 넓이 전체의 둘레다. */
  var 선세기 = {'점선':1,'얇게':2,'굵게':3,'이중':4};
  function 선모으기(){
    var 가로 = {}, 세로 = {};
    function 긋기(판, k, 모양, 색){
      var 옛 = 판[k];
      if(!옛 || (선세기[모양]||0) >= (선세기[옛[0]]||0)) 판[k] = [모양, 색];
    }
    Object.keys(모델.칸).forEach(function(a){
      var v = 모델.칸[a], 테 = v.테두리; if(!테) return;
      var x = 자리(a), h = +v.세로병합 || 1, w = +v.가로병합 || 1, 색 = v.테두리색 || '#222222';
      Object.keys(테).forEach(function(변){
        var 모양 = 테[변]; if(!선표[모양]) return;
        var i;
        if(변 === '위')    for(i=x.c;i<x.c+w;i++) 긋기(가로, x.r+','+i, 모양, 색);
        if(변 === '아래')  for(i=x.c;i<x.c+w;i++) 긋기(가로, (x.r+h)+','+i, 모양, 색);
        if(변 === '왼쪽')  for(i=x.r;i<x.r+h;i++) 긋기(세로, i+','+x.c, 모양, 색);
        if(변 === '오른쪽') for(i=x.r;i<x.r+h;i++) 긋기(세로, i+','+(x.c+w), 모양, 색);
      });
    });
    return {가로: 가로, 세로: 세로};
  }
  function 칸선(선, a){
    var x = 자리(a), v = 모델.칸[a] || {}, h = +v.세로병합 || 1, w = +v.가로병합 || 1, i;
    function 센것(목록){
      var 고른 = null;
      목록.forEach(function(y){ if(y && (!고른 || (선세기[y[0]]||0) > (선세기[고른[0]]||0))) 고른 = y; });
      return 고른;
    }
    var 위=[], 아래=[], 왼=[], 오른=[];
    for(i=x.c;i<x.c+w;i++){ 위.push(선.가로[x.r+','+i]); 아래.push(선.가로[(x.r+h)+','+i]); }
    for(i=x.r;i<x.r+h;i++){ 왼.push(선.세로[i+','+x.c]); 오른.push(선.세로[i+','+(x.c+w)]); }
    return {'위': 센것(위), '아래': 센것(아래), '왼쪽': 센것(왼), '오른쪽': 센것(오른)};
  }
  function 칠하기전부(){
    var 선 = 선모으기();
    표().querySelectorAll('td[data-cell]').forEach(function(td){ 칠하기한칸(td.dataset.cell, 선); });
  }
  function 칠하기한칸(a, 선){
    var td = 칸태그(a); if(!td) return;
    var v = 모델.칸[a] || {};
    td.style.background = v.배경 || '';
    td.style.color = v.글자 || '';
    td.style.fontWeight = v.굵게 ? '700' : '';
    td.style.fontStyle = v.기울임 ? 'italic' : '';
    td.style.textDecoration = v.밑줄 ? 'underline' : '';
    td.style.fontSize = v.크기 ? (v.크기 + 'px') : '';
    td.style.fontFamily = 글꼴표[v.글꼴] || '';
    td.style.textAlign = v.정렬 || '';
    var 네변 = 칸선(선 || 선모으기(), a);
    Object.keys(변표).forEach(function(변){
      var x = 네변[변];
      td.style['border' + 변표[변]] = x ? (선표[x[0]] + ' ' + x[1]) : '';
    });
  }

  /* -- 고르기 ---------------------------------------------------------------- */
  function 네모(가, 나){
    var a = 자리(가), b = 자리(나), 목록 = [];
    for(var r=Math.min(a.r,b.r); r<=Math.max(a.r,b.r); r++)
      for(var c=Math.min(a.c,b.c); c<=Math.max(a.c,b.c); c++)
        목록.push(주소(r,c));
    return 목록;
  }
  /* 고른 네모. **병합된 칸에 걸치면 그 칸을 다 덮도록 넓힌다** (엑셀과 같다).
     안 넓히면 병합 칸 절반만 고른 채 «바깥 테두리» 를 그을 때, 병합 칸은 제
     둘레에, 나머지 칸은 고른 네모 둘레에 선을 그어 선이 두 군데로 갈라졌다. */
  function 테두리네모(){
    if(!기준) return null;
    var a = 자리(기준), b = 자리(끝 || 기준);
    var n = {r0: Math.min(a.r,b.r), c0: Math.min(a.c,b.c),
             r1: Math.max(a.r,b.r), c1: Math.max(a.c,b.c)};
    for(var 번=0; 번<50; 번++){
      var 넓힘 = false;
      Object.keys(모델.칸).forEach(function(k){
        var v = 모델.칸[k], h = +v.세로병합 || 1, w = +v.가로병합 || 1;
        if(h === 1 && w === 1) return;
        var x = 자리(k), r9 = x.r + h - 1, c9 = x.c + w - 1;
        if(x.r > n.r1 || r9 < n.r0 || x.c > n.c1 || c9 < n.c0) return;   /* 안 걸친다 */
        if(x.r < n.r0){ n.r0 = x.r; 넓힘 = true; }
        if(x.c < n.c0){ n.c0 = x.c; 넓힘 = true; }
        if(r9 > n.r1){ n.r1 = r9; 넓힘 = true; }
        if(c9 > n.c1){ n.c1 = c9; 넓힘 = true; }
      });
      if(!넓힘) break;
    }
    return n;
  }
  function 네모글(){
    var n = 테두리네모(); if(!n) return '';
    var 가 = 주소(n.r0,n.c0), 나 = 주소(n.r1,n.c1);
    return 가 === 나 ? 가 : (가 + ':' + 나);
  }
  /* 병합에 **덮인 자리**면 그 병합의 왼쪽 위 칸(주인)을 돌려준다. 아니면 그대로.
     덮인 자리는 화면에 칸이 없다 — 거기 적으면 안 보이는 곳에 글이 들어갔다
     (A1·A2 를 합치고 A1 에서 Enter 로 내려가 A2 에 적으면 사라지던 것). */
  function 주인(a){
    var x = 자리(a), 찾은 = a;
    Object.keys(모델.칸).some(function(k){
      var v = 모델.칸[k], h = +v.세로병합 || 1, w = +v.가로병합 || 1;
      if(h === 1 && w === 1) return false;
      var y = 자리(k);
      if(x.r >= y.r && x.r < y.r + h && x.c >= y.c && x.c < y.c + w){ 찾은 = k; return true; }
      return false;
    });
    return 찾은;
  }
  function 고르기(가, 나){
    기준 = 주인(가); 끝 = 나 || 기준;
    var n = 테두리네모();
    고른것 = 네모(주소(n.r0, n.c0), 주소(n.r1, n.c1));
    고른것칠하기();
  }
  function 고른것칠하기(){
    var 손잡이 = 고른것.length ? 네모글().split(':').pop() : '';
    표().querySelectorAll('td[data-cell]').forEach(function(td){
      td.classList.toggle('picked', 고른것.indexOf(td.dataset.cell) >= 0);
      td.classList.toggle('anchor', td.dataset.cell === 기준);
      var 그립 = td.querySelector('.fillgrip');
      if(td.dataset.cell === 손잡이 && !그립){
        var g = document.createElement('span');
        g.className = 'fillgrip';
        g.title = '끌어서 채우기 (수식의 칸 참조가 따라 옮겨집니다)';
        td.appendChild(g);
      }else if(td.dataset.cell !== 손잡이 && 그립){ 그립.remove(); }
    });
    if(document.activeElement !== 수식칸 || 이동중){
      수식칸.value = 기준 ? ((모델.칸[기준]||{}).글 || '') : '';
      높이맞추기();
      if(이동중 && document.activeElement === 수식칸) 수식칸.select();
    }
    수식칸.disabled = !기준;
    var 표시 = 칸.querySelector('.sheetat');
    if(표시) 표시.textContent = 네모글() || '-';
  }
  function 옮기기(dr, dc, 넓히기){
    if(!기준) return;
    /* 병합된 칸에서 옮기면 **그 병합을 건너뛴다** (엑셀과 같다) — A1:A2 가
       합쳐져 있으면 A1 에서 아래로 가면 A3 다. 덮인 A2 에 멈추지 않는다. */
    var 지금 = 주인(넓히기 ? (끝 || 기준) : 기준);
    var n = 자리(지금), v = 모델.칸[지금] || {};
    var h = +v.세로병합 || 1, w = +v.가로병합 || 1;
    var r = dr > 0 ? n.r + h : (dr < 0 ? n.r - 1 : n.r);
    var c = dc > 0 ? n.c + w : (dc < 0 ? n.c - 1 : n.c);
    r = Math.max(0, Math.min((+모델.행수||1)-1, r));
    c = Math.max(0, Math.min((+모델.열수||1)-1, c));
    if(넓히기) 고르기(기준, 주인(주소(r,c))); else 고르기(주소(r,c));
    var td = 칸태그(넓히기 ? 끝 : 기준);
    if(td && td.scrollIntoView) td.scrollIntoView({block:'nearest', inline:'nearest'});
  }

  /* -- 서버에 다시 계산해 달라고 한다 (저장은 안 한다) ------------------------- */
  var 계산번호 = 0;
  function 다시계산(더){
    담기();
    var 몸 = new URLSearchParams();
    몸.set('id', bid); 몸.set('sheet', 숨은.value);
    Object.keys(더 || {}).forEach(function(k){ 몸.set(k, 더[k]); });
    var 내번호 = ++계산번호;
    return fetch('/dash/sheet/calc', {method:'POST', body:몸, credentials:'same-origin'})
      .then(function(r){ return r.json(); })
      .then(function(j){
        if(내번호 !== 계산번호) return;          /* 더 새 요청이 있다 */
        if(j.error){ alert(j.error); return; }
        모델 = j.sheet; 모델.칸 = 모델.칸 || {};
        담기();
        var 옛 = 표();
        var 틀 = document.createElement('div'); 틀.innerHTML = j.html;
        var 새 = 틀.querySelector('table.sheet');
        옛.parentNode.replaceChild(새, 옛);
        var 경고 = 칸.querySelector('.sheetwarn');
        if(경고) 경고.innerHTML = (j.오류 || []).map(function(x){
          var p = document.createElement('p'); p.className = 'flag'; p.textContent = x;
          return p.outerHTML; }).join('');
        /* 격자가 줄었으면 고른 자리를 안으로 당긴다 */
        if(기준){
          var a = 자리(기준), b = 자리(끝 || 기준);
          var 막 = function(x){ return {r: Math.min(x.r, 모델.행수-1), c: Math.min(x.c, 모델.열수-1)}; };
          a = 막(a); b = 막(b);
          고르기(주소(a.r,a.c), 주소(b.r,b.c));
        }
      })
      .catch(function(){ alert('다시 계산하지 못했습니다. 저장하면 반영됩니다.'); });
  }

  /* -- 글 넣기 --------------------------------------------------------------- */
  function 글넣기(){
    if(!기준) return;
    var 새 = 수식칸.value, 옛 = (모델.칸[기준]||{}).글 || '';
    편집전 = null; 넣은참조 = null;
    표().querySelectorAll('td.refpick').forEach(function(x){ x.classList.remove('refpick'); });
    if(새 === 옛) return;
    기록();
    if(새) 칸값(기준).글 = 새;
    else if(모델.칸[기준]) delete 모델.칸[기준].글;
    if(비었나(기준)) delete 모델.칸[기준];
    바뀜(); 다시계산();
  }
  function 지우기(){
    기록();
    고른것.forEach(function(a){
      if(모델.칸[a]){ delete 모델.칸[a].글; if(비었나(a)) delete 모델.칸[a]; }
    });
    바뀜(); 다시계산();
  }
  /* 키보드는 늘 **수식칸**이 받는다. 칸을 고르기만 한 동안은 «옮기는 중»
     (이동중) 이라 방향키·Enter·Tab·Delete 가 칸을 옮기고 지우고, 글자를 치면
     그때부터 «적는 중» 이다.

     격자(편집 안 되는 div)가 키를 받으면 **한글이 안 쳐졌다** — 입력기가 글자를
     조합할 곳이 없어서 첫 글자가 사라지거나 아예 안 들어간다. 수식칸은 글을
     받는 칸이라 조합이 된다. 옮기는 중에는 칸의 글을 (안 보이게) 통째로 골라
     두어서, 치면 엑셀처럼 **새 글로 바뀐다**. */
  var 이동중 = true;
  function 칸에초점(){
    이동중 = true; 편집전 = null; 넣은참조 = null;
    수식칸.classList.add('nav');
    if(!기준) return;
    수식칸.value = (모델.칸[기준]||{}).글 || '';
    높이맞추기();
    수식칸.focus({preventScroll:true});
    수식칸.select();
  }
  function 적기모드(){
    if(!이동중) return;
    이동중 = false;
    편집전 = (모델.칸[기준]||{}).글 || '';
    수식칸.classList.remove('nav');
  }
  function 적기시작(첫글){
    if(!기준) return;
    수식칸.focus({preventScroll:true});
    적기모드();
    if(첫글 !== undefined){ 수식칸.value = 첫글; }
    var n = 수식칸.value.length; 수식칸.setSelectionRange(n, n);
    수식칸.dispatchEvent(new Event('input', {bubbles:true}));
  }
  function 적는중인가(){ return document.activeElement === 수식칸 && !이동중; }
  /* 수식칸은 적은 줄 수만큼 늘어난다 (Alt+Enter 줄바꿈). 너무 길면 멈추고 넘긴다. */
  function 높이맞추기(){
    수식칸.style.height = 'auto';
    수식칸.style.height = Math.min(수식칸.scrollHeight + 2, 180) + 'px';
    수식칸.style.overflowY = 수식칸.scrollHeight > 178 ? 'auto' : 'hidden';
  }
  function 참조넣을때인가(){
    if(!적는중인가()) return false;           /* 옮기는 중이면 칸을 고르는 것이다 */
    var v = 수식칸.value, i = 수식칸.selectionStart;
    if(v.charAt(0) !== '=') return false;
    if(넣은참조 && 넣은참조.e === i) return true;
    return /[=(,+\-*\/&<>:^]\s*$/.test(v.slice(0, i));
  }

  /* -- 마우스 ---------------------------------------------------------------- */
  var 끄는중 = null, 크기끌기 = null;
  /* 열 너비는 <colgroup> 에 걸리고, 표 폭은 그 합이다 (서버 `_시트표` 와 같다). */
  function 열맞추기(글, px){
    var 열들 = 표().querySelectorAll('colgroup col');
    var i = 자리(글 + '1').c + 1;                  /* 0번 col 은 행 번호 자리 */
    if(열들[i]) 열들[i].style.width = px + 'px';
    var 합 = 0;
    열들.forEach(function(c){ 합 += parseInt(c.style.width, 10) || 0; });
    표().style.width = 합 + 'px';
  }
  function 열너비(글){
    var col = 표().querySelectorAll('colgroup col')[자리(글 + '1').c + 1];
    return col ? (parseInt(col.style.width, 10) || 90) : 90;
  }
  칸.addEventListener('mousedown', function(e){
    var 머리 = e.target.closest && e.target.closest('table.sheet th[data-col],table.sheet th[data-row]');
    if(머리){
      var 판 = 머리.getBoundingClientRect();
      if(머리.dataset.col && e.clientX > 판.right - 6){
        e.preventDefault();
        크기끌기 = {el: 머리, 열: 머리.dataset.col, 시작: e.clientX, 처음: 열너비(머리.dataset.col)};
      }else if(머리.dataset.row && e.clientY > 판.bottom - 6){
        e.preventDefault();
        크기끌기 = {el: 머리, 행: 머리.dataset.row, 시작: e.clientY, 처음: 판.height};
      }else if(머리.dataset.col){           /* 열 전체 고르기 */
        var c = 자리(머리.dataset.col + '1').c;
        고르기(주소(0,c), 주소((+모델.행수||1)-1, c));
      }else{                                /* 행 전체 고르기 */
        var r = +머리.dataset.row - 1;
        고르기(주소(r,0), 주소(r, (+모델.열수||1)-1));
      }
      return;
    }
    if(e.target.classList && e.target.classList.contains('fillgrip')){
      e.preventDefault();
      끄는중 = {원본: 네모글(), 네모: 테두리네모()};
      return;
    }
    var td = e.target.closest && e.target.closest('td[data-cell]');
    if(!td || !칸.contains(td)) return;
    /* 칸이 초점을 가져가지 못하게 한다 — 키보드는 수식칸이 받아야 한다 (한글) */
    e.preventDefault();
    /* 수식을 적는 중이면 누른 칸을 **참조로 넣는다** (엑셀과 같다). */
    if(참조넣을때인가()){
      e.preventDefault();
      var v = 수식칸.value, 참조 = td.dataset.cell;
      if(e.shiftKey && 넣은참조){ 참조 = 넣은참조.처음 + ':' + td.dataset.cell; }
      var s0 = 넣은참조 ? 넣은참조.s : 수식칸.selectionStart;
      var e0 = 넣은참조 ? 넣은참조.e : 수식칸.selectionEnd;
      수식칸.value = v.slice(0, s0) + 참조 + v.slice(e0);
      넣은참조 = {s: s0, e: s0 + 참조.length,
                 처음: (e.shiftKey && 넣은참조) ? 넣은참조.처음 : td.dataset.cell};
      수식칸.setSelectionRange(넣은참조.e, 넣은참조.e);
      표().querySelectorAll('td.refpick').forEach(function(x){ x.classList.remove('refpick'); });
      td.classList.add('refpick');
      return;
    }
    if(적는중인가()) 글넣기();
    if(e.shiftKey && 기준) 고르기(기준, td.dataset.cell);
    else 고르기(td.dataset.cell);
    끄는중 = null;
    고르는중 = true;
    /* 키보드를 받을 자리로 (방향키·바로 적기·한글 조합이 된다) */
    칸에초점();
  });
  var 고르는중 = false;
  /* 너비·높이 끌기는 **문서 전체**에서 따라간다 — 끄는 중에 마우스가 편집기
     밖으로 나가도 멈추지 않게. */
  document.addEventListener('mousemove', function(e){
    if(!크기끌기) return;
    e.preventDefault();
    if(크기끌기.열){
      var w = Math.max(24, Math.round(크기끌기.처음 + e.clientX - 크기끌기.시작));
      열맞추기(크기끌기.열, w); 크기끌기.값 = w;
    }else{
      var h = Math.max(18, Math.round(크기끌기.처음 + e.clientY - 크기끌기.시작));
      크기끌기.el.style.height = h + 'px';
      var 줄 = 크기끌기.el.parentNode; if(줄) 줄.style.height = h + 'px';
      크기끌기.값 = h;
    }
  });
  칸.addEventListener('mousemove', function(e){
    if(크기끌기) return;
    var td = e.target.closest && e.target.closest('td[data-cell]');
    if(!td) return;
    if(끄는중){
      var n = 끄는중.네모, x = 자리(td.dataset.cell);
      var 대상 = 주소(n.r0,n.c0) + ':' + 주소(Math.max(n.r1, x.r), Math.max(n.c1, x.c));
      끄는중.대상 = 대상;
      var 목록 = 네모(대상.split(':')[0], 대상.split(':')[1]);
      표().querySelectorAll('td').forEach(function(y){
        y.classList.toggle('filling', 목록.indexOf(y.dataset.cell) >= 0);
      });
      return;
    }
    if(고르는중 && (e.buttons & 1) && td.dataset.cell !== 끝){
      고르기(기준, td.dataset.cell);         /* 끌어서 여러 칸 고르기 */
    }
  });
  document.addEventListener('mouseup', function(){
    고르는중 = false;
    if(크기끌기){
      var k = 크기끌기; 크기끌기 = null;
      if(k.값){
        기록();
        if(k.열){ 모델.열너비[k.열] = String(k.값); 열맞추기(k.열, k.값); }
        else 모델.행높이[k.행] = String(k.값);
        담기(); 바뀜();
      }
      return;
    }
    if(!끄는중) return;
    var 것 = 끄는중; 끄는중 = null;
    표().querySelectorAll('td.filling').forEach(function(x){ x.classList.remove('filling'); });
    if(것.대상 && 것.대상 !== 것.원본) 채우기(것.원본, 것.대상);
  });
  칸.addEventListener('dblclick', function(e){
    var 머리 = e.target.closest && e.target.closest('table.sheet th[data-col]');
    if(머리){                               /* 열 머리 두 번 누르기 = 너비 자동 */
      기록(); delete 모델.열너비[머리.dataset.col]; 열맞추기(머리.dataset.col, 90);
      담기(); 바뀜(); 다시계산(); return;
    }
    if(e.target.closest && e.target.closest('td[data-cell]') && 기준) 적기시작();
  });

  /* -- 채우기 · 붙여넣기 (칸 참조를 미는 일은 서버가 한다) --------------------- */
  function 채우기(원본, 대상){
    if(!원본 || !대상 || 원본 === 대상) return;
    기록(); 바뀜();
    다시계산({'채울원본': 원본, '채울대상': 대상});
  }
  function 행열(무엇){
    var n = 테두리네모();
    var 행쪽 = 무엇.charAt(0) === '행';
    var 위치 = n ? (행쪽 ? n.r0 : n.c0) : (행쪽 ? +모델.행수 : +모델.열수);
    var 개수 = n ? (행쪽 ? n.r1-n.r0+1 : n.c1-n.c0+1) : 1;
    if(!n && 무엇.slice(-2) === '삭제') 위치 = 위치 - 1;
    기록(); 바뀜();
    다시계산({'행열': 무엇, '위치': 위치, '개수': 개수});
  }

  /* -- 키보드 ---------------------------------------------------------------
     옮기는 중(칸만 고른 상태)의 키. 격자에 초점이 있을 때도, 수식칸이 옮기는
     중일 때도 같은 규칙이다. 처리했으면 true. */
  function 옮기는키(e){
    if(!기준) return false;
    var 컨트롤 = e.ctrlKey || e.metaKey, 키 = e.key;
    if(컨트롤){
      var k = 키.toLowerCase();
      if(k === 'z'){ e.preventDefault(); 되감기(e.shiftKey); return true; }
      if(k === 'y'){ e.preventDefault(); 되감기(true); return true; }
      if(k === 's'){ e.preventDefault(); 저장(); return true; }
      if(k === 'b'){ e.preventDefault(); 껐다켜기('굵게'); return true; }
      if(k === 'i'){ e.preventDefault(); 껐다켜기('기울임'); return true; }
      if(k === 'u'){ e.preventDefault(); 껐다켜기('밑줄'); return true; }
      if(k === 'a'){ e.preventDefault(); 고르기('A1', 주소((+모델.행수||1)-1, (+모델.열수||1)-1)); return true; }
      /* Ctrl+C · Ctrl+V 는 여기서 안 잡는다. 브라우저가 copy · paste 이벤트를
         내게 두어야 클립보드를 **직접** 읽고 쓸 수 있다 (아래). */
      if(k === 'd' || k === 'r'){
        e.preventDefault();
        var n = 테두리네모();
        if(n.r0 === n.r1 && n.c0 === n.c1) return true;
        var 원본 = k === 'd' ? 주소(n.r0,n.c0) + ':' + 주소(n.r0,n.c1)
                            : 주소(n.r0,n.c0) + ':' + 주소(n.r1,n.c0);
        채우기(원본, 네모글());
        return true;
      }
      return false;
    }
    var 방향 = {ArrowUp:[-1,0], ArrowDown:[1,0], ArrowLeft:[0,-1], ArrowRight:[0,1]}[키];
    if(방향){ e.preventDefault(); 옮기기(방향[0], 방향[1], e.shiftKey); return true; }
    if(키 === 'Enter' && !e.altKey){ e.preventDefault(); 옮기기(e.shiftKey ? -1 : 1, 0); return true; }
    if(키 === 'Tab'){ e.preventDefault(); 옮기기(0, e.shiftKey ? -1 : 1); return true; }
    if(키 === 'F2'){ e.preventDefault(); 적기시작(); return true; }
    if(키 === 'Delete' || 키 === 'Backspace'){ e.preventDefault(); 지우기(); return true; }
    if(키 === 'Escape'){ e.preventDefault(); return true; }
    return false;
  }
  /* 격자에 초점이 오면 키보드 자리(수식칸)로 넘긴다 */
  격자.addEventListener('focusin', function(){ if(기준) 칸에초점(); });
  격자.addEventListener('keydown', function(e){
    if(옮기는키(e)) return;
    if(e.key.length === 1 && !e.altKey && !e.ctrlKey && !e.metaKey){ e.preventDefault(); 적기시작(e.key); }
  });

  수식칸.addEventListener('keydown', function(e){
    if(e.defaultPrevented) return;          /* 자동완성이 먼저 가져갔다 (Tab·Esc) */
    if(이동중){
      if(e.isComposing || e.keyCode === 229) return;   /* 한글 조합 시작 — 적기로 넘어간다 */
      if(옮기는키(e)) return;
      if(e.key === 'Enter' && e.altKey){ e.preventDefault(); return; }
      return;                              /* 글자 — 그대로 들어가고 input 에서 적기로 */
    }
    if(e.key === 'Enter' && e.altKey){
      /* 칸 안 줄바꿈 (엑셀과 같다). 적은 자리에 줄바꿈을 끼운다. */
      e.preventDefault();
      var 앞 = 수식칸.selectionStart, 뒤 = 수식칸.selectionEnd, v = 수식칸.value;
      수식칸.value = v.slice(0, 앞) + '\n' + v.slice(뒤);
      수식칸.setSelectionRange(앞 + 1, 앞 + 1);
      높이맞추기();
      return;
    }
    if(e.key === 'Enter' && !e.altKey){
      if(document.getElementById('fxdrop')) return;
      if(e.isComposing) return;            /* 한글 조합을 끝내는 Enter */
      e.preventDefault(); 글넣기();
      옮기기(e.shiftKey ? -1 : 1, 0);
      칸에초점();
    }else if(e.key === 'Tab'){
      e.preventDefault(); 글넣기();
      옮기기(0, e.shiftKey ? -1 : 1);
      칸에초점();
    }else if(e.key === 'Escape'){
      e.preventDefault();
      칸에초점();                          /* 적던 것을 버리고 칸 글로 되돌린다 */
    }
  });
  /* 칸을 고른 채 **수식칸을 누르면 바로 고치기** — 누른 자리에 커서가 선다.
     (예전에는 옮기는 중이라 커서가 안 보이고 방향키가 칸을 옮겨서, 칸을 두 번
     눌러야만 고칠 수 있었다.) */
  수식칸.addEventListener('mousedown', function(){ if(이동중 && 기준) 적기모드(); });
  수식칸.addEventListener('compositionstart', function(){ 적기모드(); });
  수식칸.addEventListener('input', function(){ 적기모드(); 넣은참조 = null; 높이맞추기(); });
  /* 다른 데로 초점이 나가면 적은 것을 넣는다 (칸 참조를 누를 때는 안 나간다) */
  수식칸.addEventListener('blur', function(){ setTimeout(function(){
    if(document.activeElement !== 수식칸 && !이동중 && 편집전 !== null){ 글넣기(); 이동중 = true; 수식칸.classList.add('nav'); }
  }, 150); });

  /* -- 복사·붙여넣기 ---------------------------------------------------------
     키보드 Ctrl+C·V 를 가로채지 않고 **copy·paste 이벤트**를 쓴다. 그래야
     클립보드를 바로 읽고 쓸 수 있다 — `navigator.clipboard` 는 https 가 아닌
     사내망 주소에서 막혀 있어서, 밖에서 복사한 표가 안 붙었다.

     클립보드에는 글(TSV)과 **HTML 표**가 같이 들어간다. 엑셀·구글 시트·웹 페이지는
     HTML 표에 병합(colspan·rowspan)과 서식을 싣는다 — 그걸 읽어 병합·굵기·색·
     테두리까지 옮긴다. 여기서 복사할 때도 같은 모양으로 싣으므로 엑셀에 붙이면
     병합·서식이 따라간다. */
  var 복사한것 = null;              /* {영역, 표식} — 이 시트 안에서 복사한 것 */

  function 이스케이프(글){
    return String(글).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                     .replace(/"/g,'&quot;');
  }
  function TSV칸(글){
    글 = String(글 == null ? '' : 글);
    return /[\t\n"]/.test(글) ? '"' + 글.replace(/"/g, '""') + '"' : 글;
  }
  function 칸복사(e){
    if(!기준 || !e.clipboardData) return;
    e.preventDefault();
    var n = 테두리네모(), 표식 = String(Date.now()) + Math.random().toString(36).slice(2, 7);
    복사한것 = {영역: 네모글(), 표식: 표식};
    var 줄들 = [], 표줄 = [], 덮인 = {};
    for(var r=n.r0; r<=n.r1; r++){
      var 글줄 = [], 칸줄 = [];
      for(var c=n.c0; c<=n.c1; c++){
        var a = 주소(r,c), td = 칸태그(a);
        var 보임 = td ? td.innerText.replace(/\n$/, '') : '';
        글줄.push(TSV칸(덮인[a] ? '' : 보임));
        if(덮인[a] || !td) continue;
        var v = 모델.칸[a] || {}, h = +v.세로병합 || 1, w = +v.가로병합 || 1;
        for(var rr=r; rr<r+h; rr++) for(var cc=c; cc<c+w; cc++) if(rr!==r || cc!==c) 덮인[주소(rr,cc)] = 1;
        var 속 = 보임.split('\n').map(이스케이프).join('<br style="mso-data-placement:same-cell;">');
        칸줄.push('<td' + (w > 1 ? ' colspan="' + w + '"' : '') + (h > 1 ? ' rowspan="' + h + '"' : '')
          + ' style="' + 이스케이프(td.style.cssText) + '"'
          + " data-raw='" + 이스케이프(JSON.stringify(v)).replace(/'/g, '&#39;') + "'>" + 속 + '</td>');
      }
      줄들.push(글줄.join('\t'));
      표줄.push('<tr>' + 칸줄.join('') + '</tr>');
    }
    e.clipboardData.setData('text/plain', 줄들.join('\r\n'));
    e.clipboardData.setData('text/html', '<meta charset="utf-8"><table data-cvsheet="' + bid + '|'
      + 표식 + '" style="border-collapse:collapse">' + 표줄.join('') + '</table>');
  }

  function 칸붙이기(e){
    if(!기준 || !e.clipboardData) return;
    e.preventDefault();
    var html = e.clipboardData.getData('text/html') || '';
    var 글 = e.clipboardData.getData('text/plain') || '';
    /* 이 시트에서 방금 복사한 것 — 수식의 칸 참조를 옮긴 만큼 밀어야 하므로 서버에 맡긴다 */
    if(복사한것 && html.indexOf('data-cvsheet="' + bid + '|' + 복사한것.표식 + '"') >= 0){
      var 원 = 복사한것.영역.split(':'), 가 = 자리(원[0]), 나 = 자리(원[원.length-1]);
      var n = 테두리네모(), 대상;
      if(n.r0 !== n.r1 || n.c0 !== n.c1) 대상 = 네모글();
      else 대상 = 주소(n.r0,n.c0) + ':' + 주소(n.r0 + Math.abs(나.r-가.r), n.c0 + Math.abs(나.c-가.c));
      채우기(복사한것.영역, 대상);
      return;
    }
    var 표 = /<table[\s>]/i.test(html) ? HTML표읽기(html) : null;
    if(!표 || !표.칸들.length) 표 = TSV읽기(글);
    if(표 && 표.칸들.length) 깔기(표);
  }
  격자.addEventListener('copy', 칸복사);
  격자.addEventListener('paste', 칸붙이기);
  /* 수식칸이 옮기는 중이면 복사·붙여넣기는 **칸**의 것이다. 적는 중이면 글자의 것. */
  수식칸.addEventListener('copy', function(e){ if(이동중) 칸복사(e); });
  수식칸.addEventListener('paste', function(e){ if(이동중) 칸붙이기(e); });

  /* 따옴표로 감싼 칸(칸 안 줄바꿈·탭) 까지 읽는 TSV. 엑셀이 이렇게 준다. */
  function TSV읽기(글){
    글 = 글.replace(/\r\n?/g, '\n').replace(/\n$/, '');
    if(!글) return null;
    var 줄들 = [[]], 지금 = '', i = 0, 따옴 = false;
    while(i < 글.length){
      var ch = 글.charAt(i);
      if(따옴){
        if(ch === '"' && 글.charAt(i+1) === '"'){ 지금 += '"'; i += 2; continue; }
        if(ch === '"'){ 따옴 = false; i++; continue; }
        지금 += ch; i++; continue;
      }
      if(ch === '"' && 지금 === ''){ 따옴 = true; i++; continue; }
      if(ch === '\t'){ 줄들[줄들.length-1].push(지금); 지금 = ''; i++; continue; }
      if(ch === '\n'){ 줄들[줄들.length-1].push(지금); 지금 = ''; 줄들.push([]); i++; continue; }
      지금 += ch; i++;
    }
    줄들[줄들.length-1].push(지금);
    var 칸들 = [], 너비 = 0;
    줄들.forEach(function(줄, r){
      너비 = Math.max(너비, 줄.length);
      줄.forEach(function(v, c){ 칸들.push({r: r, c: c, 칸: v ? {글: v} : {}}); });
    });
    return {높이: 줄들.length, 너비: 너비, 칸들: 칸들};
  }

  /* 밖에서 온 HTML 표. 계산된 스타일을 읽으려고 **스크립트가 막힌** 숨은 틀에
     그려 본다 (엑셀은 서식을 <style> 의 클래스로 준다 — 글만 봐서는 모른다). */
  function HTML표읽기(html){
    var 문서 = new DOMParser().parseFromString(html, 'text/html');
    문서.querySelectorAll('script,iframe,object,embed,link,img,meta,base,form').forEach(function(x){ x.remove(); });
    var 틀 = document.createElement('iframe');
    틀.setAttribute('sandbox', 'allow-same-origin');        /* 스크립트는 안 돈다 */
    틀.style.cssText = 'position:absolute;left:-10000px;top:0;width:1600px;height:900px;visibility:hidden';
    document.body.appendChild(틀);
    try{
      var d = 틀.contentDocument;
      d.open(); d.write('<!doctype html>' + 문서.documentElement.outerHTML); d.close();
      var 표 = d.querySelector('table');
      if(!표) return null;
      var 창 = 틀.contentWindow, 칸들 = [], 찬 = {}, 높이 = 0, 너비 = 0, 크기들 = {};
      var 줄목록 = Array.prototype.filter.call(표.rows, function(tr){ return tr.closest('table') === 표; });
      줄목록.forEach(function(tr, r){
        var c = 0;
        Array.prototype.forEach.call(tr.cells, function(td){
          while(찬[r + ',' + c]) c++;
          var w = Math.max(1, td.colSpan || 1), h = Math.max(1, td.rowSpan || 1);
          for(var rr=r; rr<r+h; rr++) for(var cc=c; cc<c+w; cc++) 찬[rr + ',' + cc] = 1;
          var 칸 = 스타일읽기(td, 창, 크기들);
          var 글 = (td.innerText || td.textContent || '').replace(/ /g, ' ').replace(/\n+$/, '');
          if(글) 칸.글 = 글;
          if(w > 1) 칸.가로병합 = w;
          if(h > 1) 칸.세로병합 = h;
          칸들.push({r: r, c: c, 칸: 칸});
          높이 = Math.max(높이, r + h); 너비 = Math.max(너비, c + w);
          c += w;
        });
      });
      /* 가장 흔한 글자 크기는 «기본» 으로 본다 — 안 그러면 모든 칸이 11pt 로 박힌다 */
      var 흔한 = null, 최다 = 0;
      Object.keys(크기들).forEach(function(k){ if(크기들[k] > 최다){ 최다 = 크기들[k]; 흔한 = +k; } });
      칸들.forEach(function(x){ if(x.칸.크기 === 흔한) delete x.칸.크기; });
      return {높이: 높이, 너비: 너비, 칸들: 칸들};
    } finally { 틀.remove(); }
  }
  function 색(v){
    var m = /rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)(?:[,\s/]+([\d.]+))?/.exec(v || '');
    if(!m || (m[4] !== undefined && +m[4] === 0)) return '';
    return '#' + [m[1], m[2], m[3]].map(function(x){ return ('0' + (+x).toString(16)).slice(-2); }).join('');
  }
  function 스타일읽기(td, 창, 크기들){
    var cs = 창.getComputedStyle(td), 칸 = {};
    if(parseInt(cs.fontWeight, 10) >= 600) 칸.굵게 = 1;
    if(cs.fontStyle === 'italic') 칸.기울임 = 1;
    if((cs.textDecorationLine || cs.textDecoration || '').indexOf('underline') >= 0) 칸.밑줄 = 1;
    var 글자 = 색(cs.color);
    if(글자 && 글자 !== '#000000') 칸.글자 = 글자;
    /* 배경은 칸에 없으면 줄·표에 있을 수 있다 */
    for(var el = td; el && el.tagName !== 'BODY'; el = el.parentElement){
      var 배경 = 색(창.getComputedStyle(el).backgroundColor);
      if(배경){ if(배경 !== '#ffffff') 칸.배경 = 배경; break; }
      if(el.tagName === 'TABLE') break;
    }
    var 맞춤 = cs.textAlign;
    if(맞춤 === 'center' || 맞춤 === '-webkit-center') 칸.정렬 = 'center';
    else if(맞춤 === 'right' || 맞춤 === 'end' || 맞춤 === '-webkit-right') 칸.정렬 = 'right';
    var 크기 = Math.round(parseFloat(cs.fontSize) || 0);
    if(크기 >= 8 && 크기 <= 48){ 칸.크기 = 크기; 크기들[크기] = (크기들[크기] || 0) + 1; }
    var 테 = {}, 선색 = '';
    [['위','Top'],['아래','Bottom'],['왼쪽','Left'],['오른쪽','Right']].forEach(function(x){
      var 모양 = cs['border' + x[1] + 'Style'], 두께 = parseFloat(cs['border' + x[1] + 'Width']) || 0;
      if(!모양 || 모양 === 'none' || 모양 === 'hidden' || 두께 <= 0) return;
      var 이것 = 색(cs['border' + x[1] + 'Color']);
      if(!이것) return;                       /* 투명한 선은 선이 아니다 */
      테[x[0]] = 모양 === 'double' ? '이중'
               : (모양 === 'dashed' || 모양 === 'dotted') ? '점선'
               : (두께 >= 1.5 ? '굵게' : '얇게');
      선색 = 선색 || 이것;
    });
    if(Object.keys(테).length){ 칸.테두리 = 테; 칸.테두리색 = 선색 || '#222222'; }
    /* 이 앱에서 복사한 표 (다른 시트) — 수식·서식을 그대로 가져온다 */
    if(td.getAttribute('data-raw')){
      try{
        var 날것 = JSON.parse(td.getAttribute('data-raw'));
        if(날것 && typeof 날것 === 'object'){ delete 날것.가로병합; delete 날것.세로병합; return 날것; }
      }catch(_){}
    }
    return 칸;
  }

  /* 읽은 표를 고른 칸부터 깐다. 그 넓이의 옛 칸·병합은 걷어 낸다 (엑셀과 같다). */
  function 깔기(표){
    var 시작 = 자리(기준);
    var 행끝 = Math.min(100, 시작.r + 표.높이), 열끝 = Math.min(26, 시작.c + 표.너비);
    기록();
    모델.행수 = Math.max(+모델.행수 || 1, 행끝);
    모델.열수 = Math.max(+모델.열수 || 1, 열끝);
    var n = {r0: 시작.r, c0: 시작.c, r1: 행끝 - 1, c1: 열끝 - 1};
    Object.keys(모델.칸).forEach(function(k){
      var x = 자리(k), v = 모델.칸[k], h = +v.세로병합 || 1, w = +v.가로병합 || 1;
      var 안 = x.r >= n.r0 && x.r <= n.r1 && x.c >= n.c0 && x.c <= n.c1;
      var 걸침 = x.r <= n.r1 && x.r + h - 1 >= n.r0 && x.c <= n.c1 && x.c + w - 1 >= n.c0;
      if(안) delete 모델.칸[k];
      else if(걸침){ delete v.세로병합; delete v.가로병합; if(비었나(k)) delete 모델.칸[k]; }
    });
    표.칸들.forEach(function(x){
      var r = 시작.r + x.r, c = 시작.c + x.c;
      if(r > n.r1 || c > n.c1 || !Object.keys(x.칸).length) return;
      var 칸 = JSON.parse(JSON.stringify(x.칸));
      if(칸.세로병합) 칸.세로병합 = Math.min(칸.세로병합, n.r1 - r + 1);
      if(칸.가로병합) 칸.가로병합 = Math.min(칸.가로병합, n.c1 - c + 1);
      if(칸.세로병합 === 1) delete 칸.세로병합;
      if(칸.가로병합 === 1) delete 칸.가로병합;
      모델.칸[주소(r, c)] = 칸;
    });
    바뀜();
    고르기(주소(n.r0, n.c0), 주소(n.r1, n.c1));
    다시계산();
  }

  /* -- 서식 ------------------------------------------------------------------ */
  function 서식먹이기(이름, 값){
    기록();
    고른것.forEach(function(a){
      if(값 === null){ if(모델.칸[a]) delete 모델.칸[a][이름]; }
      else 칸값(a)[이름] = 값;
      if(비었나(a)) delete 모델.칸[a];
    });
    칠하기전부(); 담기(); 바뀜();
  }
  function 껐다켜기(이름){
    var 켤까 = !고른것.every(function(a){ return (모델.칸[a]||{})[이름]; });
    서식먹이기(이름, 켤까 ? 1 : null);
  }
  function 서식지우기(){
    기록();
    고른것.forEach(function(a){
      var 글 = (모델.칸[a]||{}).글, 병 = 모델.칸[a] || {};
      var 남길 = {};
      if(글) 남길.글 = 글;
      if(병.가로병합) 남길.가로병합 = 병.가로병합;
      if(병.세로병합) 남길.세로병합 = 병.세로병합;
      if(Object.keys(남길).length) 모델.칸[a] = 남길; else delete 모델.칸[a];
    });
    칠하기전부(); 담기(); 바뀜();
  }
  /* 테두리. 엑셀의 «테두리» 단추와 같은 묶음들. 병합된 칸은 덮는 넓이로 본다. */
  function 테두리(방식){
    var n = 테두리네모(); if(!n) return;
    var 모양 = (칸.querySelector("[data-sheet='선']") || {}).value || '얇게';
    var 색 = (칸.querySelector("[data-sheet='테두리색']") || {}).value || '#222222';
    if(방식 === '굵은바깥') { 모양 = '굵게'; 방식 = '바깥'; }
    기록();
    고른것.forEach(function(a){
      var td = 칸태그(a); if(!td) return;
      var x = 자리(a), v = 모델.칸[a] || {};
      var 아래끝 = x.r + (+v.세로병합 || 1) - 1, 오른끝 = x.c + (+v.가로병합 || 1) - 1;
      if(방식 === '없음'){
        if(모델.칸[a]){ delete 모델.칸[a].테두리; delete 모델.칸[a].테두리색;
                      if(비었나(a)) delete 모델.칸[a]; }
        return;
      }
      var 변들 = [];
      if(방식 === '모두') 변들 = ['위','아래','왼쪽','오른쪽'];
      else if(방식 === '바깥'){
        if(x.r === n.r0) 변들.push('위');
        if(아래끝 >= n.r1) 변들.push('아래');
        if(x.c === n.c0) 변들.push('왼쪽');
        if(오른끝 >= n.c1) 변들.push('오른쪽');
      }
      else if(방식 === '아래' && 아래끝 >= n.r1) 변들 = ['아래'];
      else if(방식 === '위' && x.r === n.r0) 변들 = ['위'];
      else if(방식 === '왼쪽' && x.c === n.c0) 변들 = ['왼쪽'];
      else if(방식 === '오른쪽' && 오른끝 >= n.c1) 변들 = ['오른쪽'];
      else if(방식 === '가로안' ){
        if(아래끝 < n.r1) 변들.push('아래');
        if(x.r > n.r0) 변들.push('위');
      }
      if(!변들.length) return;
      var 칸하나 = 칸값(a); 칸하나.테두리 = 칸하나.테두리 || {};
      변들.forEach(function(변){ 칸하나.테두리[변] = 모양; });
      칸하나.테두리색 = 색;
    });
    /* «없음» 은 고른 네모 **바깥 이웃**이 이쪽으로 그어 둔 선도 지운다.
       선은 두 칸이 나눠 가지므로, 안 지우면 이웃의 선이 계속 보인다. */
    if(방식 === '없음'){
      var 반대 = {'위':'아래','아래':'위','왼쪽':'오른쪽','오른쪽':'왼쪽'};
      Object.keys(모델.칸).forEach(function(k){
        var v = 모델.칸[k]; if(!v.테두리) return;
        var x = 자리(k), h = +v.세로병합 || 1, w = +v.가로병합 || 1;
        var 겹가로 = x.c <= n.c1 && x.c + w - 1 >= n.c0, 겹세로 = x.r <= n.r1 && x.r + h - 1 >= n.r0;
        if(겹가로 && x.r + h === n.r0) delete v.테두리['아래'];
        if(겹가로 && x.r === n.r1 + 1) delete v.테두리['위'];
        if(겹세로 && x.c + w === n.c0) delete v.테두리['오른쪽'];
        if(겹세로 && x.c === n.c1 + 1) delete v.테두리['왼쪽'];
        if(!Object.keys(v.테두리).length){ delete v.테두리; delete v.테두리색; }
        if(비었나(k)) delete 모델.칸[k];
      });
    }
    칠하기전부(); 담기(); 바뀜();
  }
  function 병합(){
    var n = 테두리네모();
    if(!n || (n.r0 === n.r1 && n.c0 === n.c1)) return;
    기록();
    var 왼위 = 주소(n.r0, n.c0);
    /* 덮이는 칸의 글은 버린다 — 엑셀도 그렇게 하고, 안 버리면 병합을 풀 때
       어디서 나온 글인지 알 수가 없다. */
    네모(왼위, 주소(n.r1,n.c1)).forEach(function(a){ if(a !== 왼위) delete 모델.칸[a]; });
    칸값(왼위).가로병합 = n.c1 - n.c0 + 1;
    칸값(왼위).세로병합 = n.r1 - n.r0 + 1;
    바뀜(); 고르기(왼위); 다시계산();
  }
  function 해제(){
    기록();
    고른것.forEach(function(a){
      if(모델.칸[a]){ delete 모델.칸[a].가로병합; delete 모델.칸[a].세로병합;
                    if(비었나(a)) delete 모델.칸[a]; }
    });
    바뀜(); 다시계산();
  }
  function 저장(){
    if(적는중인가()) 글넣기();
    담기();
    var 끝칸 = 폼.querySelector("input[name='끝']");
    if(!끝칸){ 끝칸 = document.createElement('input'); 끝칸.type = 'hidden';
              끝칸.name = '끝'; 끝칸.value = '1'; 폼.appendChild(끝칸); }
    window.__leaving = true;
    폼.submit();
  }
  폼.addEventListener('submit', function(){ if(적는중인가()) 글넣기(); 담기(); });

  칸.querySelectorAll('[data-sheet]').forEach(function(el){
    var 무엇 = el.dataset.sheet;
    if(무엇 === '선' || 무엇 === '테두리색') return;       /* 테두리 단추가 읽는다 */
    var 이벤트 = (el.tagName === 'SELECT' || el.type === 'color' || el.type === 'checkbox')
                ? 'change' : 'click';
    el.addEventListener(이벤트, function(e){
      if(el.tagName === 'BUTTON') e.preventDefault();
      if(무엇 === '되돌리기') return 되감기(false);
      if(무엇 === '다시하기') return 되감기(true);
      if(무엇 === '격자숨김'){ 기록(); 모델.격자숨김 = el.checked ? 1 : 0; 담기(); 바뀜(); return; }
      if(무엇.indexOf('행') === 0 || 무엇.indexOf('열') === 0){
        if(/(삽입|삭제)$/.test(무엇)) return 행열(무엇);
      }
      if(!고른것.length) return;
      if(무엇 === '굵게' || 무엇 === '기울임' || 무엇 === '밑줄') 껐다켜기(무엇);
      else if(무엇 === '서식지우기') 서식지우기();
      else if(무엇 === '병합') 병합();
      else if(무엇 === '병합해제') 해제();
      else if(무엇 === '테두리'){ if(el.value){ 테두리(el.value); el.value = ''; } }
      else 서식먹이기(무엇, el.value || null);
      칸에초점();
    });
  });

  고른것칠하기();
}
document.addEventListener('DOMContentLoaded', function(){
  document.querySelectorAll('.sheetedit').forEach(시트편집기);
});
