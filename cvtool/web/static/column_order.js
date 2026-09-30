
(function(){
  var 표 = document.getElementById('colorder');
  if(!표) return;
  var 끄는줄 = null;

  function 번호다시(){
    /* 화면에 보이는 차례가 곧 순서다. 사람이 세지 않는다. */
    var n = 0;
    표.querySelectorAll('tr[data-col]').forEach(function(tr){
      var f = tr.querySelector('.ordfield');
      if(f){ f.value = String(++n); }
    });
    var 저장 = document.querySelector('#colform button[type=submit]');
    if(저장) 저장.classList.add('dirty');
  }

  window.colMove = function(btn, 어디){
    var tr = btn.closest('tr');
    var 형제 = 어디 < 0 ? tr.previousElementSibling : tr.nextElementSibling;
    /* '숨긴 열' 머리줄은 건너뛴다 — 그 위/아래로 넘어가면 숨김도 같이 바뀐다 */
    while(형제 && !형제.dataset.col){
      형제 = 어디 < 0 ? 형제.previousElementSibling : 형제.nextElementSibling;
    }
    if(!형제) return;
    if(어디 < 0) tr.parentNode.insertBefore(tr, 형제);
    else tr.parentNode.insertBefore(형제, tr);
    번호다시();
    tr.scrollIntoView({block: 'nearest'});
  };

  표.addEventListener('dragstart', function(e){
    var tr = e.target.closest('tr[data-col]');
    if(!tr) return;
    끄는줄 = tr;
    tr.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    /* 파이어폭스는 데이터를 넣어야 끌기가 시작된다 */
    try { e.dataTransfer.setData('text/plain', tr.dataset.col); } catch(_){}
  });
  표.addEventListener('dragend', function(){
    if(끄는줄) 끄는줄.classList.remove('dragging');
    끄는줄 = null;
    표.querySelectorAll('.dropmark').forEach(function(x){
      x.classList.remove('dropmark');
    });
  });
  표.addEventListener('dragover', function(e){
    if(!끄는줄) return;
    e.preventDefault();
    var tr = e.target.closest && e.target.closest('tr[data-col]');
    if(!tr || tr === 끄는줄) return;
    표.querySelectorAll('.dropmark').forEach(function(x){
      x.classList.remove('dropmark');
    });
    tr.classList.add('dropmark');
    var r = tr.getBoundingClientRect();
    var 위쪽 = (e.clientY - r.top) < r.height / 2;
    tr.parentNode.insertBefore(끄는줄, 위쪽 ? tr : tr.nextSibling);
  });
  표.addEventListener('drop', function(e){
    if(!끄는줄) return;
    e.preventDefault();
    번호다시();
  });
})();
