
function 시트편집기(칸){
  var 숨은 = 칸.querySelector('input.sheetdata');
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
  function 고르기(가, 나){
    기준 = 가; 끝 = 나 || 가;
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
    if(document.activeElement !== 수식칸){
      수식칸.value = 기준 ? ((모델.칸[기준]||{}).글 || '') : '';
    }
    수식칸.disabled = !기준;
    var 표시 = 칸.querySelector('.sheetat');
    if(표시) 표시.textContent = 네모글() || '-';
  }
  function 옮기기(dr, dc, 넓히기){
    if(!기준) return;
    var n = 자리(넓히기 ? (끝 || 기준) : 기준);
    var r = Math.max(0, Math.min((+모델.행수||1)-1, n.r+dr));
    var c = Math.max(0, Math.min((+모델.열수||1)-1, n.c+dc));
    if(넓히기) 고르기(기준, 주소(r,c)); else 고르기(주소(r,c));
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
  function 적기시작(첫글){
    if(!기준) return;
    편집전 = (모델.칸[기준]||{}).글 || '';
    수식칸.focus();
    if(첫글 !== undefined){ 수식칸.value = 첫글; }
    var n = 수식칸.value.length; 수식칸.setSelectionRange(n, n);
    수식칸.dispatchEvent(new Event('input', {bubbles:true}));
  }
  function 적는중인가(){ return document.activeElement === 수식칸; }
  function 참조넣을때인가(){
    if(!적는중인가()) return false;
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
    /* 격자에 초점을 둔다 — 그래야 방향키·바로 적기가 된다 */
    칸.querySelector('.sheetgrid').focus({preventScroll:true});
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

  /* -- 키보드: 격자 --------------------------------------------------------- */
  var 복사한것 = '';
  칸.querySelector('.sheetgrid').addEventListener('keydown', function(e){
    if(!기준) return;
    var 컨트롤 = e.ctrlKey || e.metaKey, 키 = e.key;
    if(컨트롤){
      var k = 키.toLowerCase();
      if(k === 'z'){ e.preventDefault(); 되감기(e.shiftKey); return; }
      if(k === 'y'){ e.preventDefault(); 되감기(true); return; }
      if(k === 's'){ e.preventDefault(); 저장(); return; }
      if(k === 'b'){ e.preventDefault(); 껐다켜기('굵게'); return; }
      if(k === 'i'){ e.preventDefault(); 껐다켜기('기울임'); return; }
      if(k === 'u'){ e.preventDefault(); 껐다켜기('밑줄'); return; }
      if(k === 'c'){ 복사(); return; }
      if(k === 'v'){ e.preventDefault(); 붙이기(); return; }
      if(k === 'd' || k === 'r'){
        e.preventDefault();
        var n = 테두리네모();
        if(n.r0 === n.r1 && n.c0 === n.c1) return;
        var 원본 = k === 'd' ? 주소(n.r0,n.c0) + ':' + 주소(n.r0,n.c1)
                            : 주소(n.r0,n.c0) + ':' + 주소(n.r1,n.c0);
        채우기(원본, 네모글());
      }
      return;
    }
    var 방향 = {ArrowUp:[-1,0], ArrowDown:[1,0], ArrowLeft:[0,-1], ArrowRight:[0,1]}[키];
    if(방향){ e.preventDefault(); 옮기기(방향[0], 방향[1], e.shiftKey); return; }
    if(키 === 'Enter'){ e.preventDefault(); 옮기기(e.shiftKey ? -1 : 1, 0); return; }
    if(키 === 'Tab'){ e.preventDefault(); 옮기기(0, e.shiftKey ? -1 : 1); return; }
    if(키 === 'F2'){ e.preventDefault(); 적기시작(); return; }
    if(키 === 'Delete' || 키 === 'Backspace'){ e.preventDefault(); 지우기(); return; }
    if(키.length === 1 && !e.altKey){ e.preventDefault(); 적기시작(키); }
  });

  /* -- 키보드: 수식칸 ------------------------------------------------------- */
  수식칸.addEventListener('keydown', function(e){
    if(e.defaultPrevented) return;          /* 자동완성이 먼저 가져갔다 (Tab·Esc) */
    if(e.key === 'Enter' && !e.altKey){
      if(document.getElementById('fxdrop')) return;
      e.preventDefault(); 글넣기();
      칸.querySelector('.sheetgrid').focus({preventScroll:true});
      옮기기(e.shiftKey ? -1 : 1, 0);
    }else if(e.key === 'Tab'){
      e.preventDefault(); 글넣기();
      칸.querySelector('.sheetgrid').focus({preventScroll:true});
      옮기기(0, e.shiftKey ? -1 : 1);
    }else if(e.key === 'Escape'){
      e.preventDefault();
      if(편집전 !== null) 수식칸.value = 편집전;
      편집전 = null; 넣은참조 = null;
      칸.querySelector('.sheetgrid').focus({preventScroll:true});
    }else if(편집전 === null){
      편집전 = (모델.칸[기준]||{}).글 || '';
    }
  });
  수식칸.addEventListener('input', function(){ 넣은참조 = null; });
  /* 다른 데로 초점이 나가면 적은 것을 넣는다 (칸 참조를 누를 때는 안 나간다) */
  수식칸.addEventListener('blur', function(){ setTimeout(function(){
    if(!적는중인가() && 편집전 !== null) 글넣기(); }, 150); });

  /* -- 복사·붙여넣기 --------------------------------------------------------- */
  function 복사(){
    복사한것 = 네모글();
    if(!navigator.clipboard) return;
    var n = 테두리네모(), 줄들 = [];
    for(var r=n.r0; r<=n.r1; r++){
      var 줄 = [];
      for(var c=n.c0; c<=n.c1; c++){
        var td = 칸태그(주소(r,c)); 줄.push(td ? td.textContent.trim() : '');
      }
      줄들.push(줄.join('\t'));
    }
    navigator.clipboard.writeText(줄들.join('\n')).catch(function(){});
  }
  function 붙이기(){
    if(복사한것){
      var 원 = 복사한것.split(':'), 가 = 자리(원[0]), 나 = 자리(원[원.length-1]);
      var n = 테두리네모(), 대상;
      if(n.r0 !== n.r1 || n.c0 !== n.c1) 대상 = 네모글();
      else 대상 = 주소(n.r0,n.c0) + ':' + 주소(n.r0 + Math.abs(나.r-가.r), n.c0 + Math.abs(나.c-가.c));
      채우기(복사한것, 대상);
      return;
    }
    /* 밖(엑셀 등)에서 복사한 글: 탭·줄바꿈으로 나눠 칸마다 넣는다 */
    if(!navigator.clipboard || !navigator.clipboard.readText) return;
    navigator.clipboard.readText().then(function(글){
      if(!글) return;
      var 시작 = 자리(기준);
      기록();
      글.replace(/\r/g,'').replace(/\n$/,'').split('\n').forEach(function(줄, i){
        줄.split('\t').forEach(function(v, j){
          var r = 시작.r + i, c = 시작.c + j;
          if(r >= 모델.행수 || c >= 모델.열수) return;
          var a = 주소(r,c);
          if(v) 칸값(a).글 = v; else if(모델.칸[a]){ delete 모델.칸[a].글; if(비었나(a)) delete 모델.칸[a]; }
        });
      });
      바뀜(); 다시계산();
    }).catch(function(){});
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
      칸.querySelector('.sheetgrid').focus({preventScroll:true});
    });
  });

  고른것칠하기();
}
document.addEventListener('DOMContentLoaded', function(){
  document.querySelectorAll('.sheetedit').forEach(시트편집기);
});
