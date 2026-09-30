
var RT = {editor: null, subject: null, last: null, range: null, cell: null};

function rtInit(){
  RT.editor = document.getElementById('rtbody');
  if(!RT.editor) return;
  RT.subject = document.querySelector('input[name=subject]');
  RT.last = RT.editor;
  try { document.execCommand('styleWithCSS', false, true); } catch(e) {}

  ['keyup','mouseup','input'].forEach(function(ev){
    RT.editor.addEventListener(ev, function(){
      RT.last = RT.editor; rtSave(); rtSyncTableBar();
    });
  });
  document.addEventListener('selectionchange', function(){
    if(document.activeElement !== RT.editor) return;
    rtSave(); rtSyncTableBar();
  });
  if(RT.subject) RT.subject.addEventListener('focus', function(){ RT.last = RT.subject; });

  // 도구를 눌러도 커서를 잃지 않게 한다 (이게 편집이 들쭉날쭉하던 원인)
  // 표 도구도 같다 — 커서가 표 안에 있어야 어느 칸에 적용할지 알 수 있다.
  document.querySelectorAll('.rt-bar').forEach(function(bar){
    bar.addEventListener('mousedown', function(e){
      if(e.target.closest('input[type=color], input[type=file],'
                          + ' input[type=number], select')) return;
      e.preventDefault();
    });
  });

  rtDragInit();
  RT.editor.addEventListener('paste', rtPaste);
  RT.editor.addEventListener('input', function(){
    markDirty(document.getElementById('bodyfield'));
  });
  var form = RT.editor.closest('form');
  if(form) form.addEventListener('submit', function(){
    document.getElementById('bodyfield').value = RT.editor.innerHTML;
  });
}

function rtSave(){
  var s = window.getSelection();
  if(s.rangeCount && RT.editor.contains(s.anchorNode)) RT.range = s.getRangeAt(0);
}
function rtFocus(){
  if(document.activeElement === RT.editor) return;
  var r = RT.range;                       // focus() 가 저장된 위치를 건드릴 수 있다
  RT.editor.focus();
  if(r && RT.editor.contains(r.startContainer)){
    var s = window.getSelection();
    s.removeAllRanges();
    s.addRange(r);
  }
}
function rtCmd(cmd, val){
  rtFocus();
  document.execCommand(cmd, false, val || null);
  rtSave();
  markDirty(document.getElementById('bodyfield'));
}
function rtInsert(html){
  rtFocus();
  document.execCommand('insertHTML', false, html);
  rtSave();
  markDirty(document.getElementById('bodyfield'));
}

// --- 글씨 크기: execCommand 의 1~7 을 실제 pt 로 바꿔친다 ----------------------
function rtFontSize(pt, label){
  rtFocus();
  document.execCommand('styleWithCSS', false, false);
  document.execCommand('fontSize', false, '7');      // 7 을 표시로 쓴다
  document.execCommand('styleWithCSS', false, true);
  var 표시들 = Array.prototype.slice.call(RT.editor.querySelectorAll('font[size="7"]'));
  var 마지막 = null;
  표시들.forEach(function(f){
    var s = document.createElement('span');
    s.style.fontSize = pt;
    while(f.firstChild) s.appendChild(f.firstChild);
    f.parentNode.replaceChild(s, f);
    마지막 = s;
  });
  if(마지막){                                        // 이어서 칠 수 있게 커서를 둔다
    var r = document.createRange();
    r.selectNodeContents(마지막);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(r);
    rtSave();
  }
  var btn = document.getElementById('rt-size-btn');
  if(btn && label) btn.firstChild.textContent = label;
  markDirty(document.getElementById('bodyfield'));
}
function rtFontName(name){
  rtCmd('fontName', name);
  var btn = document.getElementById('rt-font-btn');
  if(btn) btn.firstChild.textContent = name;
}

// --- 붙여넣기 정리 ------------------------------------------------------------
function rtPaste(e){
  var dt = e.clipboardData;
  if(!dt) return;
  var html = dt.getData('text/html');
  if(!html){ return; }                                // 글자만이면 그대로 둔다
  e.preventDefault();
  var box = document.createElement('div');
  box.innerHTML = html
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/<(style|script|meta|link)[\s\S]*?<\/\1>/gi, '')
    .replace(/<(style|meta|link)[^>]*>/gi, '');
  box.querySelectorAll('*').forEach(function(el){
    ['class','id','lang','align'].forEach(function(a){ el.removeAttribute(a); });
    if(el.hasAttribute('style')){
      var 남길것 = ['color','background-color','font-size','font-family','font-weight',
                  'font-style','text-decoration','text-align'];
      var 새 = 남길것.map(function(k){
        var v = el.style.getPropertyValue(k);
        return v ? k + ':' + v : '';
      }).filter(Boolean).join(';');
      if(새) el.setAttribute('style', 새); else el.removeAttribute('style');
    }
  });
  rtInsert(box.innerHTML);
}

// --- 공통 드롭다운 -----------------------------------------------------------
function rtDrop(btn, html, onPick){
  var old = document.getElementById('rtdrop');
  if(old){
    var 같은것 = old.dataset.owner === btn.id;
    old.remove();
    if(같은것) return;
  }
  var m = document.createElement('div');
  m.id = 'rtdrop';
  m.dataset.owner = btn.id || '';
  m.innerHTML = html;
  document.body.appendChild(m);
  var r = btn.getBoundingClientRect();
  m.style.left = Math.min(r.left, window.innerWidth - m.offsetWidth - 12) + 'px';
  m.style.top = (r.bottom + window.scrollY + 3) + 'px';
  m.addEventListener('mousedown', function(e){ e.preventDefault(); });
  m.addEventListener('click', function(e){
    var it = e.target.closest('[data-v]');
    if(!it) return;
    onPick(it.dataset.v, it.dataset.label || it.textContent.trim(), it);
    if(!it.dataset.keep) m.remove();
  });
  return m;
}
function closeRtDrop(){
  var m = document.getElementById('rtdrop');
  if(m) m.remove();
}
document.addEventListener('click', function(e){
  var m = document.getElementById('rtdrop');
  if(m && !m.contains(e.target) && !(e.target.closest && e.target.closest('.rt-drop')))
    m.remove();
});
document.addEventListener('keydown', function(e){
  if(e.key !== 'Escape') return;
  var m = document.getElementById('rtdrop');
  if(m) m.remove();
});

function rtFontMenu(btn){
  var 목록 = window.rtFonts || [];
  rtDrop(btn, 목록.map(function(f){
    return "<button type='button' data-v=\"" + f + "\" style=\"font-family:'" + f
      + "'\">" + f + "</button>";
  }).join(''), function(v){ rtFontName(v); });
}
function rtSizeMenu(btn){
  var 목록 = window.rtSizes || [];
  rtDrop(btn, 목록.map(function(s){
    return "<button type='button' data-v='" + s + "' style='font-size:"
      + Math.min(parseInt(s, 10) * 1.2, 26) + "px'>" + s + "</button>";
  }).join(''), function(v){ rtFontSize(v, v); });
}
function rtColorMenu(btn, cmd){
  var 색 = ['#000000','#404040','#808080','#b0b0b0','#ffffff',
           '#b91c1c','#ea580c','#ca8a04','#15803d','#0e7490',
           '#1d4ed8','#4f46e5','#7c3aed','#be185d','#78350f',
           '#fee2e2','#ffedd5','#fef9c3','#dcfce7','#dbeafe'];
  var html = "<div class='rt-swatch'>" + 색.map(function(c){
    return "<button type='button' data-v='" + c + "' style='background:" + c
      + "' title='" + c + "'></button>";
  }).join('') + "</div>"
    + "<label class='rt-pick'>직접 고르기"
    + "<input type='color' onchange=\"rtCmd('" + cmd + "', this.value)\"></label>";
  rtDrop(btn, html, function(v){ rtCmd(cmd, v); });
}
/* 격자에서 고르는 크기. 예전에는 6×6 이 끝이라 그보다 큰 표를 아예 못 만들었다.
   격자를 넓히고, 그보다 더 크면 숫자로 직접 치게 한다. */
var RT_GRID_R = 10, RT_GRID_C = 10;

function rtTableMenu(btn){
  var html = "<div class='rt-grid' style='grid-template-columns:repeat("
    + RT_GRID_C + ",14px)'>";
  for(var r = 1; r <= RT_GRID_R; r++){
    for(var c = 1; c <= RT_GRID_C; c++){
      html += "<i data-v='" + r + "x" + c + "' data-r='" + r + "' data-c='" + c + "'></i>";
    }
  }
  html += "</div><div class='rt-gridlabel'>표 크기를 고르세요</div>"
    + "<div class='rt-gridmore'>더 크게: "
    + "<input type='number' id='rt-mr' min='1' max='60' value='3' style='width:46px'>행 × "
    + "<input type='number' id='rt-mc' min='1' max='30' value='3' style='width:46px'>열 "
    + "<button type='button' id='rt-mgo'>넣기</button></div>";
  var m = rtDrop(btn, html, function(v){
    var 조각 = v.split('x');
    rtTable(parseInt(조각[0], 10), parseInt(조각[1], 10));
  });
  if(!m) return;
  var 라벨 = m.querySelector('.rt-gridlabel');
  m.addEventListener('mouseover', function(e){
    var it = e.target.closest('i[data-v]');
    if(!it) return;
    var R = +it.dataset.r, C = +it.dataset.c;
    라벨.textContent = R + ' × ' + C;
    m.querySelectorAll('i').forEach(function(cell){
      cell.classList.toggle('on', +cell.dataset.r <= R && +cell.dataset.c <= C);
    });
  });
  /* 숫자 칸은 격자와 달리 클릭이 메뉴를 닫으면 안 된다 */
  m.addEventListener('mousedown', function(e){
    if(e.target.closest('.rt-gridmore')) e.stopPropagation();
  });
  m.querySelector('#rt-mgo').addEventListener('click', function(){
    var R = parseInt(m.querySelector('#rt-mr').value, 10);
    var C = parseInt(m.querySelector('#rt-mc').value, 10);
    if(R > 0 && C > 0) rtTable(R, C);
    closeRtDrop();
  });
}

/* 메일에서 표가 깨지는 걸 막으려면 인라인 스타일이어야 한다 (<style> 은 지워진다). */
var RT_CELL = 'border:1px solid #999;padding:6px;vertical-align:top';

function rtTable(행, 열){
  if(!행 || !열) return;
  var s = "<table class='rt-tbl' style='border-collapse:collapse;width:100%;"
    + "font-size:11pt' width='100%' cellpadding='0' cellspacing='0'>";
  for(var r = 0; r < 행; r++){
    s += '<tr>';
    for(var c = 0; c < 열; c++){ s += "<td style='" + RT_CELL + "'>&nbsp;</td>"; }
    s += '</tr>';
  }
  s += '</table><p><br></p>';
  rtInsert(s);
}

/* ---- 넣은 뒤에 고치기 -------------------------------------------------------
   예전에는 표를 넣고 나면 손댈 방법이 없어서, 열 하나를 더 넣으려고 표를 지우고
   처음부터 다시 만들어야 했다. 커서가 든 표를 찾아서 그 자리에서 고친다. */
/* 커서가 지금 든 칸. 없으면 null. */
function rtCellNow(){
  var sel = window.getSelection();
  if(!sel || !sel.rangeCount || !RT.editor) return null;
  var n = sel.getRangeAt(0).startContainer;
  if(n.nodeType !== 1) n = n.parentNode;
  var td = n && n.closest ? n.closest('td,th') : null;
  return (td && RT.editor.contains(td)) ? td : null;
}
/* 도구가 손댈 칸.
   너비 칸이나 테두리 목록을 **누르는 순간 편집기 커서를 잃는다** (포커스가
   그 칸으로 옮겨간다). 그래서 마지막으로 커서가 있던 칸을 기억해 두고 쓴다.
   기억한 칸이 지워졌으면(행·열 삭제) 버린다. */
function rtCellAt(){
  var 지금 = rtCellNow();
  if(지금){ RT.cell = 지금; return 지금; }
  var 기억 = RT.cell;
  if(기억 && RT.editor && RT.editor.contains(기억)) return 기억;
  RT.cell = null;
  return null;
}
function rtTableAt(){
  var td = rtCellAt();
  return td ? td.closest('table') : null;
}
function rtColIndex(td){
  return Array.prototype.indexOf.call(td.parentNode.children, td);
}
function rtRows(t){ return Array.prototype.slice.call(t.rows); }

function rtRow(어디){                      /* -1 위, +1 아래 */
  var td = rtCellAt(); if(!td) return;
  var tr = td.parentNode, 새 = tr.cloneNode(true);
  Array.prototype.forEach.call(새.cells, function(c){ c.innerHTML = '&nbsp;'; });
  tr.parentNode.insertBefore(새, 어디 < 0 ? tr : tr.nextSibling);
  rtTouched();
}
function rtRowDel(){
  var td = rtCellAt(); if(!td) return;
  var t = td.closest('table');
  if(t.rows.length <= 1){ rtTableDel(); return; }   /* 마지막 줄이면 표째 */
  td.parentNode.parentNode.removeChild(td.parentNode);
  rtTouched();
}
function rtCol(어디){                      /* -1 왼쪽, +1 오른쪽 */
  var td = rtCellAt(); if(!td) return;
  var i = rtColIndex(td), t = td.closest('table');
  rtRows(t).forEach(function(tr){
    var 기준 = tr.cells[i];
    var 새 = document.createElement(기준 && 기준.tagName === 'TH' ? 'th' : 'td');
    새.setAttribute('style', 기준 ? 기준.getAttribute('style') || RT_CELL : RT_CELL);
    새.innerHTML = '&nbsp;';
    if(어디 < 0) tr.insertBefore(새, 기준 || null);
    else tr.insertBefore(새, 기준 ? 기준.nextSibling : null);
  });
  rtTouched();
}
function rtColDel(){
  var td = rtCellAt(); if(!td) return;
  var i = rtColIndex(td), t = td.closest('table');
  if(t.rows[0] && t.rows[0].cells.length <= 1){ rtTableDel(); return; }
  rtRows(t).forEach(function(tr){ if(tr.cells[i]) tr.deleteCell(i); });
  rtTouched();
}
/* 표 전체의 너비.
   예전에는 만들 때 width:100% 를 박아 두고 그것뿐이었다. 그러면 열 너비를
   아무리 고쳐도 **정해진 폭을 나눠 갖는 것**이라, 열 하나만 넓히는 게 아예
   불가능했다 (옆 열이 그만큼 줄어든다). 표 폭 자체를 정할 수 있어야 한다. */
function rtTableWidth(){
  var t = rtTableAt(); if(!t) return;
  var sel = document.getElementById('rt-tblw');
  var px  = document.getElementById('rt-tblpx');
  var 값 = sel ? sel.value : '100%';
  if(px) px.style.display = (값 === 'px') ? '' : 'none';
  if(값 === 'px'){
    var n = parseInt(px && px.value, 10);
    if(!(n > 0)) return;                       /* 아직 안 쳤다 */
    t.style.width = n + 'px'; t.setAttribute('width', n);
  } else if(값 === 'fit'){
    /* 열마다 px 로 잡고 표는 그 합계를 따른다 — 열 너비 조절이 제대로 되는 길 */
    rtSeedColPx(t);
    rtSumToTable(t);
  } else if(값 === 'auto'){
    t.style.width = 'auto'; t.removeAttribute('width');
    t.style.tableLayout = '';
    rtRows(t).forEach(function(tr){
      Array.prototype.forEach.call(tr.cells, function(c){
        c.style.width = ''; c.removeAttribute('width');
      });
    });
  } else {
    t.style.width = '100%'; t.setAttribute('width', '100%');
  }
  rtTouched();
}

/* 열 너비. px 로도, % 로도 잡을 수 있다.
   px 로 잡으려면 표가 '창에 맞춤' 이면 안 된다 — 그건 폭이 이미 정해진
   것이라 px 가 의미를 잃는다. 그래서 px 를 쓰면 표를 '내용에 맞춤' 으로
   옮겨 준다. 말없이 안 먹는 것보다 낫다. */
function rtColWidth(){
  var td = rtCellAt(); if(!td) return;
  var 칸 = document.getElementById('rt-colw');
  var 단위 = document.getElementById('rt-colu');
  var u = 단위 ? 단위.value : 'px';
  rtSetColWidth(td, 칸 ? 칸.value : '', u);
}

/* 지금 그려진 폭을 그대로 px 로 못박는다.
   폭이 안 정해진 표를 '열 너비에 맞춤' 으로 옮기면 표가 글자 길이만큼 쪼그라든다
   (빈 표는 50px 쯤 된다). 보이던 모습 그대로에서 시작해야 놀라지 않는다. */
function rtSeedColPx(t){
  var 첫줄 = t.rows[0]; if(!첫줄) return;
  var 폭 = Array.prototype.map.call(첫줄.cells, function(c){
    return Math.round(c.getBoundingClientRect().width);
  });
  t.style.tableLayout = 'fixed';
  rtRows(t).forEach(function(tr){
    Array.prototype.forEach.call(tr.cells, function(c, i){
      if(!폭[i]) return;
      c.style.width = 폭[i] + 'px';
      c.setAttribute('width', String(폭[i]));
    });
  });
}

/* 표 폭 = 열 폭의 합.
   **엑셀과 같다 — 열을 넓히면 표가 따라 넓어진다.** 표 폭이 먼저 고정돼 있으면
   열 하나를 넓힐 때 옆 열이 그만큼 줄어들 뿐이라, 열 너비 조절이 반쪽이 된다. */
function rtSumToTable(t){
  var 첫줄 = t.rows[0]; if(!첫줄) return;
  var 합 = 0;
  for(var i = 0; i < 첫줄.cells.length; i++){
    var v = 첫줄.cells[i].style.width || '';
    if(v.slice(-2) !== 'px') return;            /* px 아닌 열이 있으면 손대지 않는다 */
    합 += parseFloat(v) || 0;
  }
  if(!(합 > 0)) return;
  t.style.width = Math.round(합) + 'px';
  t.setAttribute('width', String(Math.round(합)));
  var sel = document.getElementById('rt-tblw');
  if(sel) sel.value = 'fit';
  var px = document.getElementById('rt-tblpx');
  if(px) px.style.display = 'none';
}

function rtSetColWidth(td, 값, 단위){
  var i = rtColIndex(td), t = td.closest('table');
  var w = parseFloat(값);
  /* px 로 잡는다는 건 '이 열을 이만큼' 이라는 뜻이다. 표 폭이 먼저 정해져
     있으면 그 말이 지켜지지 않으므로, 표를 열 합계에 맞추는 쪽으로 옮긴다. */
  if(단위 === 'px' && w > 0){
    var 첫 = t.rows[0] && t.rows[0].cells[0];
    if(!첫 || (첫.style.width || '').slice(-2) !== 'px') rtSeedColPx(t);
  }
  /* 너비를 정한 표는 table-layout:fixed 여야 정한 대로 선다. 안 그러면
     브라우저가 내용 길이를 보고 제멋대로 다시 나눈다. */
  if(w > 0) t.style.tableLayout = 'fixed';
  rtRows(t).forEach(function(tr){
    var c = tr.cells[i]; if(!c) return;
    /* 메일 클라이언트(특히 Outlook)는 width 속성을 스타일보다 잘 따른다 */
    if(w > 0){
      c.style.width = w + 단위;
      c.setAttribute('width', 단위 === 'px' ? String(Math.round(w)) : w + '%');
    } else {
      c.style.width = ''; c.removeAttribute('width');
    }
  });
  if(단위 === 'px' && w > 0) rtSumToTable(t);
  rtTouched();
}

/* 경계선을 끌어서 넓히기.
   숫자를 치는 것보다 이게 먼저 손이 간다. 칸의 오른쪽 끝 4px 안에서 누르면
   그 열의 너비를 끄는 대로 바꾼다. contenteditable 이 글자를 고르려 들기
   때문에 mousedown 에서 기본 동작을 막아야 한다. */
var RTDrag = null;
function rtDragInit(){
  if(!RT.editor) return;
  RT.editor.addEventListener('mousemove', function(e){
    if(RTDrag) return;
    var td = e.target.closest && e.target.closest('td,th');
    var 끝인가 = td && (td.getBoundingClientRect().right - e.clientX) <= 5;
    RT.editor.style.cursor = 끝인가 ? 'col-resize' : '';
  });
  RT.editor.addEventListener('mousedown', function(e){
    var td = e.target.closest && e.target.closest('td,th');
    if(!td) return;
    var r = td.getBoundingClientRect();
    if(r.right - e.clientX > 5) return;
    e.preventDefault();
    RTDrag = {td: td, x: e.clientX, w: r.width};
    RT.cell = td;
  });
  document.addEventListener('mousemove', function(e){
    if(!RTDrag) return;
    var 새폭 = Math.max(24, Math.round(RTDrag.w + (e.clientX - RTDrag.x)));
    rtSetColWidth(RTDrag.td, 새폭, 'px');
    var 칸 = document.getElementById('rt-colw');
    var 단위 = document.getElementById('rt-colu');
    if(칸) 칸.value = 새폭;
    if(단위) 단위.value = 'px';
  });
  document.addEventListener('mouseup', function(){
    if(!RTDrag) return;
    RTDrag = null;
    RT.editor.style.cursor = '';
  });
}
function rtBorder(값){
  var t = rtTableAt(); if(!t) return;
  t.querySelectorAll('td,th').forEach(function(c){
    c.style.border = (값 === 'none') ? 'none' : 값;
  });
  rtTouched();
}
function rtHeadRow(켬){
  var t = rtTableAt(); if(!t || !t.rows.length) return;
  Array.prototype.forEach.call(t.rows[0].cells, function(c){
    c.style.fontWeight = 켬 ? 'bold' : '';
    c.style.background = 켬 ? '#eef2f7' : '';
  });
  rtTouched();
}
function rtTableDel(){
  var t = rtTableAt(); if(!t) return;
  if(!window.confirm('이 표를 통째로 지웁니다.')) return;
  t.parentNode.removeChild(t);
  rtTouched();
}
function rtTouched(){
  if(RT.editor) RT.editor.dispatchEvent(new Event('input', {bubbles: true}));
  rtSyncTableBar();
}
/* 커서가 표 안에 있을 때만 표 도구를 보여 준다. 늘 떠 있으면 자리만 차지하고,
   무엇에 적용되는지도 알 수 없다. */
function rtSyncTableBar(){
  var bar = document.getElementById('rttablebar');
  if(!bar) return;
  /* 편집기 안에 커서가 있을 때만 기억을 갱신한다. 도구 칸에 포커스가 가 있는
     동안에는 기억한 칸을 그대로 두어야 표 도구가 사라지지 않는다. */
  var 안에있나 = document.activeElement === RT.editor;
  var td = 안에있나 ? rtCellNow() : null;
  if(안에있나) RT.cell = td;
  if(!td) td = (RT.cell && RT.editor.contains(RT.cell)) ? RT.cell : null;
  bar.hidden = !td;
  if(!td) return;
  var t = td.closest('table');
  var w = bar.querySelector('#rt-colw'), u = bar.querySelector('#rt-colu');
  if(w && document.activeElement !== w){
    var v = td.style.width || '';
    if(v.slice(-1) === '%'){ w.value = parseFloat(v); if(u) u.value = '%'; }
    else if(v.slice(-2) === 'px'){ w.value = parseFloat(v); if(u) u.value = 'px'; }
    else w.value = '';
  }
  /* 표 너비 칸도 지금 표에 맞춰 둔다. 안 그러면 다른 표로 옮겨도 앞 표의
     설정이 남아 있어서, 건드리는 순간 엉뚱한 값이 적용된다. */
  var tw = bar.querySelector('#rt-tblw'), tp = bar.querySelector('#rt-tblpx');
  if(tw && document.activeElement !== tw && document.activeElement !== tp){
    var 폭 = t.style.width || '100%';
    var 첫 = t.rows[0] && t.rows[0].cells[0];
    var 열px = 첫 && (첫.style.width || '').slice(-2) === 'px';
    if(폭 === '100%') tw.value = '100%';
    else if(폭 === 'auto' || 폭 === '') tw.value = 'auto';
    else if(열px) tw.value = 'fit';            /* 열 합계를 따르는 중 */
    else { tw.value = 'px'; if(tp) tp.value = parseFloat(폭); }
    if(tp) tp.style.display = (tw.value === 'px') ? '' : 'none';
  }
  var h = bar.querySelector('#rt-head');
  if(h) h.checked = !!(t.rows[0] && t.rows[0].cells[0]
                       && t.rows[0].cells[0].style.fontWeight === 'bold');
  var b = bar.querySelector('#rt-border');
  if(b){
    var 현재 = td.style.border || '1px solid #999';
    var 있나 = Array.prototype.some.call(b.options, function(o){ return o.value === 현재; });
    if(있나) b.value = 현재;
  }
}
function rtLink(){
  var url = prompt('링크 주소를 넣으세요', 'https://');
  if(url) rtCmd('createLink', url);
}
// 그림은 본문에 base64 로 박지 않고 **서버에 파일로 올린다.**
// 본문에는 짧은 참조만 남는다. 원본이 본문 글자 안에만 있으면, 본문이 한 번
// 상했을 때 되돌릴 방법이 없다.
function rtImage(input){
  var f = input.files && input.files[0];
  input.value = '';
  if(!f) return;
  if(f.size > 2 * 1024 * 1024){
    토스트('그림이 너무 큽니다 (' + Math.round(f.size / 1024) + 'KB). '
      + '본문에 넣는 그림은 2MB 까지입니다. 큰 파일은 첨부로 붙이세요.', 1);
    return;
  }
  var fd = new FormData();
  fd.append('template', window.템플릿ID || '0');
  fd.append('file', f, f.name);
  rtInsert("<span id='rtimgwait' class='muted'>그림 올리는 중…</span>");
  fetch('/mail/image/add', {method: 'POST', body: fd})
    .then(function(r){ return r.json(); })
    .then(function(res){
      var 자리 = document.getElementById('rtimgwait');
      if(자리 && 자리.parentNode){
        if(res.ok){
          var img = document.createElement('img');
          img.src = res.src;
          img.style.maxWidth = '100%';
          자리.parentNode.replaceChild(img, 자리);
        }else{
          자리.parentNode.removeChild(자리);
          토스트(res.error || '그림을 올리지 못했습니다.', 1);
        }
      }
    })
    .catch(function(e){
      var 자리 = document.getElementById('rtimgwait');
      if(자리 && 자리.parentNode) 자리.parentNode.removeChild(자리);
      토스트('그림을 올리지 못했습니다: ' + e, 1);
    });
}

// --- 자리표시자 고르기 --------------------------------------------------------
function rtVars(btn){
  var 묶음 = window.자리표시자 || [];
  var html = "<div class='vm-head'>넣을 자리에 커서를 두고 고르세요</div>"
    + "<input type='text' class='vm-q' placeholder='이름으로 찾기'>"
    + "<div class='vm-list'>"
    + 묶음.map(function(g){
        return "<div class='vm-group'>" + g[0] + "</div>"
          + g[1].map(function(v){
              var 설명 = (window.자리표시자설명 || {})[v];
              return "<button type='button' class='vm-item' data-v='" + v + "'>"
                + v
                + (설명 ? " <span class='muted'>— " + 설명 + "</span>" : "")
                + "</button>";
            }).join('');
      }).join('')
    + "</div>";
  var m = rtDrop(btn, html, function(v){
    var 넣을것 = '{{' + v + '}}';
    if(RT.last === RT.subject && RT.subject){
      var s = RT.subject.selectionStart, e = RT.subject.selectionEnd;
      RT.subject.value = RT.subject.value.slice(0, s) + 넣을것
        + RT.subject.value.slice(e);
      RT.subject.focus();
      RT.subject.selectionStart = RT.subject.selectionEnd = s + 넣을것.length;
      markDirty(RT.subject);
    } else {
      rtInsert(넣을것);
    }
  });
  if(!m) return;
  m.classList.add('varmenu');
  var q = m.querySelector('.vm-q');
  q.addEventListener('mousedown', function(e){ e.stopPropagation(); });
  q.addEventListener('input', function(){
    var 찾기 = q.value.trim().toLowerCase();
    m.querySelectorAll('.vm-item').forEach(function(b){
      b.classList.toggle('hide', 찾기 && b.dataset.v.toLowerCase().indexOf(찾기) < 0);
    });
    m.querySelectorAll('.vm-group').forEach(function(g){
      var 보임 = false, el = g.nextElementSibling;
      while(el && el.classList.contains('vm-item')){
        if(!el.classList.contains('hide')) 보임 = true;
        el = el.nextElementSibling;
      }
      g.classList.toggle('hide', !보임);
    });
  });
  setTimeout(function(){ q.focus(); }, 0);
}
document.addEventListener('DOMContentLoaded', rtInit);
