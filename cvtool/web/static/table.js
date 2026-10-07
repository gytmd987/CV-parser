
/* 대시보드 편집: 저장 없이 미리보기 */
function blockPreview(btn){
  var 폼 = btn.closest('form'), 카드 = btn.closest('.card');
  var 자리 = 카드.querySelector('.blockout .bo-body'), 말 = 카드.querySelector('.blockout .bo-note');
  var 몸 = new URLSearchParams(new FormData(폼));
  btn.disabled = true;
  fetch('/dash/block/preview', {method:'POST', body:몸, credentials:'same-origin'})
    .then(function(r){ return r.json(); })
    .then(function(j){
      카드.querySelector('.blockout').open = true;
      if(j.error){ 자리.innerHTML = ''; var p = document.createElement('p');
        p.className = 'flag'; p.textContent = j.error; 자리.appendChild(p);
        말.textContent = '(저장 안 한 설정 — 오류)'; return; }
      자리.innerHTML = j.html; 말.textContent = '(저장 안 한 설정 — 저장해야 반영됩니다)';
      if(window.enhanceTables) enhanceTables();
    })
    .catch(function(){ alert('미리보기를 만들지 못했습니다.'); })
    .then(function(){ btn.disabled = false; });
}
/* 프로필 카드 — 찾기 · 모두 펼치기/접기 */
function pcardFind(el){
  var 말 = (el.value || '').trim().toLowerCase();
  el.closest('.card').querySelectorAll('details.pcard').forEach(function(d){
    var 맞음 = !말 || d.textContent.toLowerCase().indexOf(말) >= 0;
    d.style.display = 맞음 ? '' : 'none';
    if(말 && 맞음) d.open = true;
  });
}
function pcardAll(btn, 펼칠까){
  btn.closest('.card').querySelectorAll('details.pcard').forEach(function(d){
    if(d.style.display !== 'none') d.open = 펼칠까;
  });
}
function markDirty(el){
  el.classList.toggle('dirty', el.value !== (el.dataset.orig || ''));
}
function cellText(td){
  if(!td) return '';
  var f = td.querySelector ? td.querySelector('input,select,textarea') : null;
  if(f){
    if(f.type === 'checkbox') return f.checked ? 'Y' : '';
    if(f.tagName === 'SELECT'){
      var o = f.options[f.selectedIndex];
      return o ? o.text.replace(/\s+/g,' ').trim() : '';
    }
    /* 거르기·정렬에 쓰는 값이라 한 줄로 접는다 (복사·엑셀은 cellRaw 를 쓴다) */
    return f.value.replace(/\s+/g,' ').trim();
  }
  return (td.textContent || '').replace(/\s+/g,' ').trim();
}

/* 복사·엑셀에 실을 값. **줄바꿈을 살린다.**
   `cellText` 는 거르기·정렬용이라 한 줄로 접는데, 그걸 그대로 복사에 쓰면
   여러 줄로 적어 둔 비고가 한 줄로 뭉개져 나간다.

   여러 줄 칸은 화면에 `<br>` 로 그려져서 `textContent` 로는 줄이 안 잡힌다
   ('가<br>나' → '가나'). 그래서 값을 담아 둔 자리를 순서대로 본다. */
function cellRaw(td){
  if(!td) return '';
  var f = td.querySelector ? td.querySelector('input,select,textarea') : null;
  var v;
  if(f){
    if(f.type === 'checkbox') return f.checked ? 'Y' : '';
    if(f.tagName === 'SELECT'){
      var o = f.options[f.selectedIndex];
      return o ? o.text.replace(/\s+/g,' ').trim() : '';
    }
    v = f.value;
  } else if(td.classList.contains('multi') && td.title){
    v = td.title;                        /* 대시보드가 <br> 로 그린 여러 줄 */
  } else if(td.dataset && td.dataset.raw){
    v = td.dataset.raw;                  /* 인재 Pool 편집 칸의 저장된 값 */
  } else {
    v = td.textContent || '';
  }
  if(v.indexOf('\n') < 0) return v.replace(/\s+/g,' ').trim();
  /* 줄 사이는 살리고 줄 안만 정리한다 (서버의 normalize.paragraph 와 같은 규칙) */
  return v.split('\n').map(function(x){ return x.replace(/\s+/g,' ').trim(); })
          .join('\n').replace(/^\n+|\n+$/g, '');
}

/* TSV 한 칸. 줄바꿈·탭·따옴표가 들었으면 따옴표로 감싼다 — 엑셀이 붙여넣을 때
   알아듣는 규칙이라, 이렇게 해야 여러 줄이 한 칸 안에 그대로 들어간다. */
function tsvField(v){
  return /[\t\n"]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
}
function bodyRows(tb){ return tb.tBodies[0] ? Array.prototype.slice.call(tb.tBodies[0].rows) : []; }
function headCells(tb){ return tb.tHead ? Array.prototype.slice.call(tb.tHead.rows[0].cells) : []; }
function headText(th){ return (th.dataset.label || th.textContent || '').replace(/\s+/g,' ').trim(); }

// 머리글이 빈 칸(체크박스·상세 링크)은 엑셀에 옮길 내용이 아니라 뺀다
function keepCols(tb){
  var keep = [];
  headCells(tb).forEach(function(th, i){ if(headText(th)) keep.push(i); });
  return keep.length ? keep : headCells(tb).map(function(_, i){ return i; });
}

function tableTSV(tb){
  var heads = headCells(tb), keep = keepCols(tb), out = [];
  out.push(keep.map(function(i){ return headText(heads[i]); }).join('\t'));
  bodyRows(tb).forEach(function(tr){
    if(tr.classList.contains('hide')) return;
    out.push(keep.map(function(i){
      return tsvField(cellRaw(tr.cells[i]));
    }).join('\t'));
  });
  return out.join('\n');
}

function copyText(text){
  var ta = document.getElementById('copybuf');
  if(!ta){
    ta = document.createElement('textarea');
    ta.id = 'copybuf';
    ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
    document.body.appendChild(ta);
  }
  ta.value = text;
  ta.select();
  var ok = false;
  try { ok = document.execCommand('copy'); } catch(e) { ok = false; }
  if(!ok && navigator.clipboard) navigator.clipboard.writeText(text);
  return ta;
}

function toast(msg){
  var el = document.getElementById('toast');
  if(!el){ el = document.createElement('div'); el.id = 'toast'; document.body.appendChild(el); }
  el.textContent = msg;
  el.className = 'show';
  clearTimeout(window.__toastT);
  window.__toastT = setTimeout(function(){ el.className = ''; }, 2200);
}

// --- 찾기 (표 전체 검색 + 열별 값 추리기) ------------------------------------
// 머리줄 전체선택 — **보이는 줄만** 고른다.
// 표 위 찾기 칸으로 걸러 놓고 전체선택을 눌렀을 때, 화면에 없는 사람까지
// 선택되면 그대로 메일이 나가거나 지워진다.
function selectVisible(head){
  var tb = head.closest('table');
  var 센것 = 0;
  bodyRows(tb).forEach(function(tr){
    if(tr.classList.contains('hide')) return;
    var c = tr.querySelector('input[name=ids]');
    if(c){ c.checked = head.checked; if(c.checked) 센것++; }
  });
  showPicked(tb);
  selbarUpdate(head);
}

/* 인재 Pool — 사람을 체크했을 때만 «메일 · 채용 시작 · 삭제» 막대가 나온다 */
function selbarUpdate(el){
  var 폼 = el && el.closest ? el.closest('form.selform') : null;
  if(!폼) return;
  var n = 폼.querySelectorAll('input[name=ids]:checked').length;
  var 막대 = 폼.querySelector('.selbar');
  if(!막대) return;
  막대.classList.toggle('has', n > 0);
  var 수 = 막대.querySelector('.selcount'); if(수) 수.textContent = n + '명';
}
document.addEventListener('change', function(e){
  if(e.target && e.target.name === 'ids') selbarUpdate(e.target);
});

// 지금 몇 명 골랐는지 표 위에 적어 둔다.
function showPicked(tb){
  var bar = tb.__bar;
  if(!bar) return;
  var 칸 = bar.querySelector('.tpicked');
  if(!칸){
    칸 = document.createElement('span');
    칸.className = 'muted tpicked';
    bar.querySelector('.tcount').after(칸);
  }
  var n = tb.querySelectorAll('input[name=ids]:checked').length;
  칸.textContent = n ? ' · 고른 사람 ' + n + '명' : '';
}

function applyFilters(tb){
  var q = (tb.__q || '').trim().toLowerCase();
  var f = tb.__filters || {};
  var cols = Object.keys(f);
  var 보임 = 0;
  bodyRows(tb).forEach(function(tr){
    var hit = !q || tr.innerText.toLowerCase().indexOf(q) >= 0;
    for(var i = 0; hit && i < cols.length; i++){
      var idx = parseInt(cols[i], 10);
      if(!f[cols[i]].has(cellText(tr.cells[idx]))) hit = false;
    }
    tr.classList.toggle('hide', !hit);
    if(hit) 보임++;
    else {                                   // 안 보이는 줄은 체크를 풀어둔다
      var c = tr.querySelector('input[type=checkbox]');
      if(c) c.checked = false;
    }
  });
  headCells(tb).forEach(function(th, i){
    th.classList.toggle('filtered', f[i] !== undefined);
  });
  var 머리 = tb.querySelector('th input[type=checkbox]');
  if(머리) 머리.checked = false;      // 걸러내면 전체선택도 풀린다
  showPicked(tb);
  var out = tb.__bar && tb.__bar.querySelector('.tcount');
  if(out) out.textContent = (q || cols.length) ? (보임 + '줄 보임') : '';
}

function sortBy(tb, idx, asc){
  headCells(tb).forEach(function(o){ o.removeAttribute('data-dir'); });
  if(idx === null){                                   // 정렬 해제 = 원래 순서
    var body = tb.tBodies[0];
    (tb.__order || []).forEach(function(r){ body.appendChild(r); });
    return;
  }
  headCells(tb)[idx].dataset.dir = asc ? 'asc' : 'desc';
  var rows = bodyRows(tb);
  rows.sort(function(a, b){
    var x = cellText(a.cells[idx]), y = cellText(b.cells[idx]);
    var nx = parseFloat(x.replace(/[^0-9.\-]/g,'')), ny = parseFloat(y.replace(/[^0-9.\-]/g,''));
    var 숫자 = x !== '' && y !== '' && !isNaN(nx) && !isNaN(ny)
              && /^[0-9.,\-\s]+$/.test(x) && /^[0-9.,\-\s]+$/.test(y);
    if(x === '' && y !== '') return 1;                // 빈칸은 늘 아래로
    if(y === '' && x !== '') return -1;
    var c = 숫자 ? (nx - ny) : x.localeCompare(y, 'ko');
    return asc ? c : -c;
  });
  var body = tb.tBodies[0];
  rows.forEach(function(r){ body.appendChild(r); });
}

function closeColMenu(){
  var m = document.getElementById('colmenu');
  if(m) m.remove();
}

// 열 제목을 누르면 무엇을 할지 고르게 한다 (엑셀 필터 단추와 같은 방식)
function openColMenu(tb, idx, th){
  closeColMenu();
  var 값들 = {}, f = tb.__filters || {};
  bodyRows(tb).forEach(function(tr){
    var v = cellText(tr.cells[idx]);
    값들[v] = (값들[v] || 0) + 1;
  });
  var 목록 = Object.keys(값들).sort(function(a, b){ return a.localeCompare(b, 'ko'); });
  var 선택 = f[idx];

  var m = document.createElement('div');
  m.id = 'colmenu';
  m.innerHTML =
    "<div class='cm-head'>" + headText(th) + "</div>"
    + "<button type='button' data-act='asc'>▲ 오름차순 정렬</button>"
    + "<button type='button' data-act='desc'>▼ 내림차순 정렬</button>"
    + "<button type='button' data-act='nosort'>정렬 해제</button>"
    + "<div class='cm-sep'></div>"
    + "<div class='cm-title'>값으로 추리기</div>"
    + "<input type='text' class='cm-q' placeholder='값 찾기'>"
    + "<label class='cm-row cm-allrow'><input type='checkbox' class='cm-all'> <b>전체</b></label>"
    + "<div class='cm-list'>"
    + 목록.map(function(v, i){
        var on = !선택 || 선택.has(v);
        return "<label class='cm-row' data-v='" + i + "'>"
          + "<input type='checkbox' value='" + i + "'" + (on ? " checked" : "") + "> "
          + (v === '' ? "<i>(빈칸)</i>" : v.replace(/</g,'&lt;'))
          + " <span class='muted'>" + 값들[v] + "</span></label>";
      }).join('')
    + "</div>"
    + "<div class='cm-btns'><button type='button' data-act='apply'>적용</button>"
    + "<button type='button' class='sec' data-act='clear'>이 열 조건 해제</button></div>"
    + "<div class='cm-sep'></div>"
    + "<button type='button' data-act='copycol'>이 열만 복사</button>";
  document.body.appendChild(m);
  var r = th.getBoundingClientRect();
  m.style.left = Math.min(r.left, window.innerWidth - m.offsetWidth - 12) + 'px';
  m.style.top = (r.bottom + window.scrollY + 2) + 'px';

  var boxes = function(){ return Array.prototype.slice.call(m.querySelectorAll('.cm-list input')); };
  var all = m.querySelector('.cm-all');
  all.checked = boxes().every(function(b){ return b.checked; });
  all.addEventListener('change', function(){
    boxes().forEach(function(b){
      if(!b.closest('.cm-row').classList.contains('hide')) b.checked = all.checked;
    });
  });
  m.querySelector('.cm-q').addEventListener('input', function(e){
    var q = e.target.value.toLowerCase();
    Array.prototype.slice.call(m.querySelectorAll('.cm-list .cm-row')).forEach(function(row){
      row.classList.toggle('hide', q && row.textContent.toLowerCase().indexOf(q) < 0);
    });
  });
  m.addEventListener('click', function(ev){
    var act = ev.target.dataset ? ev.target.dataset.act : null;
    if(!act) return;
    if(act === 'asc' || act === 'desc'){ sortBy(tb, idx, act === 'asc'); closeColMenu(); }
    else if(act === 'nosort'){ sortBy(tb, null); closeColMenu(); }
    else if(act === 'clear'){
      tb.__filters = tb.__filters || {};
      delete tb.__filters[idx];
      applyFilters(tb); closeColMenu();
    }
    else if(act === 'apply'){
      var 고른값 = new Set();
      boxes().forEach(function(b){ if(b.checked) 고른값.add(목록[parseInt(b.value, 10)]); });
      tb.__filters = tb.__filters || {};
      if(고른값.size === 목록.length) delete tb.__filters[idx];
      else tb.__filters[idx] = 고른값;
      applyFilters(tb); closeColMenu();
    }
    else if(act === 'copycol'){
      var 줄 = [headText(th)];
      bodyRows(tb).forEach(function(tr){
        if(!tr.classList.contains('hide')) 줄.push(tsvField(cellRaw(tr.cells[idx])));
      });
      copyText(줄.join('\n'));
      toast('이 열을 복사했습니다. Ctrl+C 로 붙여넣으세요.');
      closeColMenu();
    }
  });
}
document.addEventListener('click', function(ev){
  var m = document.getElementById('colmenu');
  if(m && !m.contains(ev.target) && !(ev.target.closest && ev.target.closest('th.sortable'))) closeColMenu();
});
document.addEventListener('keydown', function(ev){ if(ev.key === 'Escape') closeColMenu(); });

function addToolbar(tb){
  var box = tb.closest('.scroll');
  if(!box) return;
  var bar = document.createElement('div');
  bar.className = 'tbar';
  bar.innerHTML =
    "<input type='text' placeholder='표에서 찾기' class='tfilter'>"
    + "<span class='muted tcount'></span><span style='flex:1'></span>"
    + "<button type='button' class='sec txlsx'>엑셀 내려받기</button>";
  box.parentNode.insertBefore(bar, box);
  tb.__bar = bar;

  bar.querySelector('.tfilter').addEventListener('input', function(e){
    tb.__q = e.target.value;
    applyFilters(tb);
  });
  // 줄마다 체크를 켜고 끌 때도 고른 사람 수를 따라가게 한다
  tb.addEventListener('change', function(ev){
    if(ev.target && ev.target.name === 'ids') showPicked(tb);
  });
  showPicked(tb);
  // 내려받기 단추는 표마다 **하나뿐**이다 (예전에는 카드 위에도 있었다).
  //
  // 표에 data-export 가 달려 있으면 서버가 만든 엑셀을 받는다 — 전화번호
  // 앞자리 0 이 살아 있고 열 너비도 잡혀 있다. 다만 서버는 화면에서 방금
  // 걸러낸 것까지는 모르므로, **표 위 찾기·열 필터를 쓰고 있으면** 보이는
  // 줄 그대로 만들어 보낸다. 화면과 파일이 다르면 안 된다.
  bar.querySelector('.txlsx').addEventListener('click', function(){
    var 걸러냄 = (tb.__q || '').trim() !== ''
      || Object.keys(tb.__filters || {}).length > 0;
    var 서버 = tb.dataset.export || '';
    window.__leaving = true;
    if(서버 && !걸러냄){
      location.href = 서버;
      setTimeout(function(){ window.__leaving = false; }, 1000);
      return;
    }
    var form = document.createElement('form');
    form.method = 'post'; form.action = '/table.xlsx';
    form.innerHTML = "<input type='hidden' name='name'><input type='hidden' name='tsv'>";
    form.elements.name.value = tb.dataset.name || document.title;
    form.elements.tsv.value = tableTSV(tb);
    document.body.appendChild(form);
    form.submit();
    setTimeout(function(){ form.remove(); window.__leaving = false; }, 1000);
  });
}

function sortable(tb){
  headCells(tb).forEach(function(th, idx){
    if(th.querySelector('input')) return;              // 전체선택 칸은 빼고
    if(!headText(th)) return;
    th.classList.add('sortable');
    th.title = '눌러서 정렬·추리기';
    th.addEventListener('click', function(ev){
      if(ev.target.tagName === 'A') return;
      openColMenu(tb, idx, th);
    });
  });
}

function rangeSelect(tb){
  var anchor = null, dragging = false;
  function clear(){
    Array.prototype.slice.call(tb.querySelectorAll('td.sel')).forEach(function(td){
      td.classList.remove('sel');
    });
  }
  function pos(td){ return {r: td.parentNode.rowIndex, c: td.cellIndex}; }
  function paint(a, b){
    clear();
    var r1 = Math.min(a.r, b.r), r2 = Math.max(a.r, b.r);
    var c1 = Math.min(a.c, b.c), c2 = Math.max(a.c, b.c);
    var heads = headCells(tb), lines = [], cols = [];
    for(var c = c1; c <= c2; c++) cols.push(c);
    // 머리글은 늘 함께 복사한다. 없으면 엑셀에서 무슨 열인지 알 수 없다.
    lines.push(cols.map(function(c){ return heads[c] ? headText(heads[c]) : ''; }).join('\t'));
    bodyRows(tb).forEach(function(tr){
      if(tr.rowIndex < r1 || tr.rowIndex > r2 || tr.classList.contains('hide')) return;
      lines.push(cols.map(function(c){
        var td = tr.cells[c];
        if(td) td.classList.add('sel');
        return tsvField(cellRaw(td));
      }).join('\t'));
    });
    return lines.join('\n');
  }
  tb.addEventListener('mousedown', function(ev){
    var td = ev.target.closest && ev.target.closest('td');
    if(!td || !tb.contains(td)) return;
    if(ev.target.closest('input,select,textarea,a,button')) return;
    anchor = pos(td); dragging = true;
    window.__rangeDragged = false;
    clear();
  });
  tb.addEventListener('mousemove', function(ev){
    if(!dragging || !anchor) return;
    var td = ev.target.closest && ev.target.closest('td');
    if(!td || !tb.contains(td)) return;
    var here = pos(td);
    if(here.r !== anchor.r || here.c !== anchor.c){
      window.__rangeDragged = true;
      document.body.style.userSelect = 'none';
    }
    tb.__tsv = paint(anchor, here);
  });
  document.addEventListener('mouseup', function(){
    if(!dragging) return;
    dragging = false;
    document.body.style.userSelect = '';
    if(window.__rangeDragged && tb.__tsv){
      copyText(tb.__tsv);
      toast(tb.querySelectorAll('td.sel').length + '칸 선택됨 — Ctrl+C 로 복사하세요');
    }
  });
}

// --- 저장 안 한 채로 나가려 할 때 -------------------------------------------
function dirtyGuard(){
  document.querySelectorAll('form').forEach(function(f){
    f.addEventListener('submit', function(){ window.__leaving = true; });
  });
  document.addEventListener('click', function(ev){
    var a = ev.target.closest && ev.target.closest('a[href]');
    if(!a || a.target === '_blank') return;
    var href = a.getAttribute('href') || '';
    if(href.charAt(0) === '#' || href.indexOf('javascript:') === 0) return;
    if(!document.querySelector('.dirty')) return;
    if(confirm('저장하지 않은 수정이 있습니다.\n저장하지 않고 이동할까요?')) window.__leaving = true;
    else ev.preventDefault();
  }, true);
  window.addEventListener('beforeunload', function(e){
    if(window.__leaving) return;
    if(!document.querySelector('.dirty')) return;
    e.preventDefault();
    e.returnValue = '저장하지 않은 수정이 있습니다.';
    return e.returnValue;
  });
}

function enhanceTables(){
  document.querySelectorAll('.scroll table').forEach(function(tb){
    /* 시트는 빼 둔다. 이 막대의 «엑셀 내려받기» 는 화면 글자를 TSV 로 긁는
       길이라 색도 병합도 안 실리고(시트에는 서버가 만드는 제 것이 있다),
       머리글 정렬·범위선택도 시트가 스스로 하는 칸 고르기와 부딪힌다. */
    if(tb.classList.contains('sheet')) return;
    if(tb.dataset.enhanced) return;
    tb.dataset.enhanced = '1';
    if(!tb.tHead && tb.rows.length) tb.createTHead().appendChild(tb.rows[0]);
    if(!tb.tBodies.length) return;
    tb.__order = bodyRows(tb);          // 정렬 해제하면 되돌릴 원래 순서
    tb.__filters = {};
    addToolbar(tb);
    sortable(tb);
    rangeSelect(tb);
  });
  dirtyGuard();
}
document.addEventListener('DOMContentLoaded', enhanceTables);

/* 지원자 상세 «기본 정보» — 읽기 ↔ 편집. 읽기에서는 빈 항목을 감춘다.
   고친 칸(노랗게 표시된 칸)이 있으면 읽기로 못 돌아간다 — 저장 안 한 것을 숨기지 않게. */
function roMarkBlank(card){
  card.querySelectorAll('table.kv tr').forEach(function(tr){
    var 칸 = tr.querySelector('input:not([type=hidden]),select,textarea');
    var 글 = 칸 ? (칸.value || '').trim() : tr.querySelector('td') ?
             (tr.querySelector('td').textContent || '').trim() : '';
    tr.classList.toggle('blank', !글 || 글 === '-' || 글 === '(빈칸)');
  });
}
function roToggle(btn){
  var card = btn.closest('.card');
  if(card.classList.contains('ro')){
    card.classList.remove('ro'); btn.textContent = '읽기 모드로';
  }else{
    if(card.querySelector('.dirty')){ alert('고친 내용이 있어요. 먼저 «고친 내용 저장» 을 눌러 주세요.'); return; }
    roMarkBlank(card); card.classList.add('ro'); btn.textContent = '편집';
  }
}
document.addEventListener('DOMContentLoaded', function(){
  document.querySelectorAll('.card.ro').forEach(roMarkBlank);
  /* 검토 카드에서 «기본 정보» 로 가는 링크를 누르면 바로 편집할 수 있게 */
  if(location.hash === '#추출결과'){
    var c = document.querySelector('#추출결과.ro');
    if(c){ var b = c.querySelector('.tiny-edit'); if(b) roToggle(b); }
  }
});
