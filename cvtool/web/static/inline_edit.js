
/* ---- 알림 ------------------------------------------------------------------
   화면 맨 위 띠 대신 오른쪽 위에 잠깐 띄운다. 닫기를 누를 필요가 없다 —
   저절로 사라지고, 마우스를 올리고 있는 동안에는 안 사라진다(읽는 중이니까).
   잘못됐다는 알림은 더 오래 둔다. 읽고 고쳐야 하는 글이라서. */
function 알림상자(){
  var 상자 = document.getElementById('알림상자');
  if(!상자){
    상자 = document.createElement('div');
    상자.id = '알림상자';
    document.body.appendChild(상자);
  }
  return 상자;
}
function 알림닫기(t){
  if(t.__감) return;
  t.__감 = 1;
  t.classList.add('out');
  setTimeout(function(){ if(t.parentNode) t.parentNode.removeChild(t); }, 400);
}
function 알림시작(t){
  var 남은 = t.classList.contains('bad') ? 16000 : 10000;
  var 켠때 = 0, 타이머 = null;
  function 걸기(){ 켠때 = Date.now(); 타이머 = setTimeout(function(){ 알림닫기(t); }, 남은); }
  t.addEventListener('mouseenter', function(){
    clearTimeout(타이머);
    남은 -= Date.now() - 켠때;
  });
  t.addEventListener('mouseleave', function(){
    if(남은 < 1200) 남은 = 1200;      /* 스쳐 지나갔다고 바로 없어지면 안 된다 */
    걸기();
  });
  t.addEventListener('click', function(){ 알림닫기(t); });
  걸기();
}
/* 화면 안에서 바로 알릴 때 (칸 저장 실패 등). alert 은 눌러서 꺼야 해서 안 쓴다. */
function 토스트(글, 나쁨){
  var t = document.createElement('div');
  t.className = 'toast ' + (나쁨 ? 'bad' : 'ok');
  t.textContent = 글;
  알림상자().appendChild(t);
  알림시작(t);
  return t;
}
document.addEventListener('DOMContentLoaded', function(){
  var 것들 = document.querySelectorAll('main .toast');
  for(var i = 0; i < 것들.length; i++){
    알림상자().appendChild(것들[i]);
    알림시작(것들[i]);
  }
});

/* ---- 저장하면 보던 자리로 돌아온다 -----------------------------------------
   폼을 내면 페이지가 다시 그려지고 **맨 위로 튄다.** 상세 화면 아래쪽 칸을
   고치거나, 명칭 관리에서 백 줄짜리 표 중간을 고칠 때마다 다시 스크롤해서
   내려와야 했다. 고칠 게 여러 개면 그걸 매번 한다.

   낼 때 지금 위치를 적어 두고, 같은 주소로 돌아오면 그 자리로 되돌린다.
   브라우저가 스스로 복원하려 드는 것도 꺼서 두 번 움직이지 않게 한다. */
try { if('scrollRestoration' in history) history.scrollRestoration = 'manual'; } catch(e) {}
function 자리키(){ return '자리:' + location.pathname; }
document.addEventListener('submit', function(){
  try { sessionStorage.setItem(자리키(), String(window.scrollY)); } catch(e) {}
}, true);
document.addEventListener('DOMContentLoaded', function(){
  var y = null;
  try { y = sessionStorage.getItem(자리키()); } catch(e) {}
  if(y === null) return;
  try { sessionStorage.removeItem(자리키()); } catch(e) {}
  /* 주소에 #조각이 있으면 그쪽이 먼저다 — 검토 카드처럼 일부러 보낸 자리다 */
  if(location.hash) return;
  var 되돌리기 = function(){ window.scrollTo(0, +y); };
  되돌리기();
  /* 표가 늦게 그려지면 높이가 바뀐다. 한 번 더 맞춘다. */
  window.requestAnimationFrame(되돌리기);
  setTimeout(되돌리기, 60);
});

document.addEventListener('click', function(ev){
  if(window.__rangeDragged){ window.__rangeDragged = false; return; }  // 범위 선택 중이었다
  var td = ev.target.closest && ev.target.closest('td.edit');
  if(!td || td.querySelector('input,select')) return;
  openCell(td);
});
function openCell(td){
  var raw = td.dataset.raw || '', kind = td.dataset.kind || 'text', el;
  if(kind === 'select'){
    el = document.createElement('select');
    JSON.parse(td.dataset.opts || '[]').forEach(function(o){
      var op = document.createElement('option');
      op.value = o; op.textContent = o || '(빈칸)';
      if(o === raw) op.selected = true;
      el.appendChild(op);
    });
  } else if(kind === '긴글'){
    el = document.createElement('textarea');
    el.rows = 4; el.value = raw; el.className = 'cellbox';
    if(td.dataset.help) el.placeholder = td.dataset.help;
  } else {
    el = document.createElement('input');
    el.type = 'text'; el.value = raw;
    if(td.dataset.help) el.placeholder = td.dataset.help;
  }
  var before = td.textContent, done = false;
  td.textContent = ''; td.appendChild(el);
  if(kind === '긴글'){
    /* 여러 줄 상자에서 Enter 는 줄바꿈이다. 저장하는 방법을 적어 둔다 —
       안 적으면 어떻게 끝내는지 알 수가 없다. */
    var 힌트 = document.createElement('div');
    힌트.className = 'cellhint';
    힌트.textContent = 'Ctrl+Enter 저장 · Esc 취소';
    td.appendChild(힌트);
  }
  el.focus();
  if(el.select) el.select();
  function cancel(){ if(done) return; done = true; td.textContent = before; }
  function save(){
    if(done) return; done = true;
    var v = el.value;
    if(v === raw){ td.textContent = before; return; }
    td.textContent = '저장 중...';
    var body = new URLSearchParams();
    body.append('id', td.dataset.id);
    body.append('항목', td.dataset.col);
    body.append('새값', v);
    body.append('이전값', raw);
    body.append('scope', td.dataset.scope || '기본');
    fetch('/api/cell', {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'},
      body: body.toString()})
     .then(function(r){ return r.json(); })
     .then(function(d){
       if(d.ok){
         td.dataset.raw = d.raw; td.textContent = d.표시; td.title = d.표시;
         td.classList.add('saved');
         setTimeout(function(){ td.classList.remove('saved'); }, 1200);
       } else {
         td.textContent = before; td.title = d.error;
         td.classList.add('err'); 토스트(d.error, 1);
         setTimeout(function(){ td.classList.remove('err'); }, 4000);
       }
     })
     .catch(function(e){ td.textContent = before; 토스트('저장 실패: ' + e, 1); });
  }
  el.addEventListener('keydown', function(e){
    if(e.key === 'Enter'){
      /* 여러 줄 칸에서는 Enter 가 줄바꿈이다. 보이는 게 여러 줄짜리 상자인데
         Enter 가 저장이면 손이 배신당한다. 저장은 Ctrl(⌘)+Enter. */
      if(kind === '긴글' && !(e.ctrlKey || e.metaKey)) return;
      e.preventDefault(); save();
    }
    else if(e.key === 'Escape'){ e.preventDefault(); cancel(); }
  });
  el.addEventListener('blur', save);
  if(kind === 'select') el.addEventListener('change', save);
}
