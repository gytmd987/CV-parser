
/* 열 너비 합계. 숫자로 맞추는 방식이라 **지금 표가 몇 px 인지** 보여야
   한 칸 고칠 때마다 저장하고 보러 가는 왕복이 안 생긴다.
   전부 채워야 표 폭이 그 합이 되므로, 빈 칸이 남았으면 그것도 알려 준다. */
function wsum(el){
  var 카드 = el.closest ? el.closest('.card') : null;
  if(!카드) return;
  var 칸들 = 카드.querySelectorAll("input[name='colw'],input[name='colwidth']");
  var 합 = 0, 빈것 = 0, 센것 = 0;
  칸들.forEach(function(c){
    /* 목록 편집 표의 맨 아래 줄은 '열 추가' 자리다. 수식이 비어 있으면 아직
       열이 아니므로 세지 않는다 — 안 그러면 늘 "1칸 비어 있음" 이 뜬다. */
    var 줄 = c.closest ? c.closest('tr') : null;
    var 식 = 줄 ? 줄.querySelector("input[name='colformula']") : null;
    if(식 && !식.value.trim()) return;
    센것++;
    var v = parseInt(c.value, 10);
    if(v > 0) 합 += v; else 빈것++;
  });
  var 글 = !센것 ? ''
         : 빈것 ? '합계 ' + 합 + 'px · ' + 빈것 + '칸 비어 있음'
         : '표 폭 ' + 합 + 'px';
  카드.querySelectorAll('.wsum').forEach(function(s){ s.textContent = 글; });
}
document.addEventListener('DOMContentLoaded', function(){
  document.querySelectorAll("input[name='colw'],input[name='colwidth']")
    .forEach(function(c){ wsum(c); });
});

function fxWhoChanged(sel){
  document.querySelectorAll('.fx').forEach(function(el){
    el.dataset.cid = sel.value;
    fxPreview(el);
  });
}
function fxPreview(el){
  /* 타이머를 **칸마다** 둔다. 하나로 두면 화면을 처음 그릴 때 칸들이 서로를
     취소해서 마지막 하나만 살아남는다 (실제로 그랬다 — 미리보기가 통째로
     비어 보였다). */
  clearTimeout(el.__fxt);
  el.__fxt = setTimeout(function(){
    var out = el.parentNode.querySelector('.fxout');
    if(!out) return;
    var 틀 = el.value.trim();
    if(!틀){ out.textContent = ''; out.className = 'fxout muted'; return; }
    var q = '/dash/preview?id=' + encodeURIComponent(el.dataset.cid || '')
          + '&kind=' + (el.dataset.kind || 'row')
          + '&bid=' + (el.dataset.bid || '')
          + '&line=' + encodeURIComponent(틀);
    fetch(q, {credentials: 'same-origin'})
      .then(function(r){ return r.json(); })
      .then(function(res){
        if(res.error){ out.textContent = res.error; out.className = 'fxout flag'; }
        else {
          out.textContent = res.text ? '→ ' + res.text
                                     : '→ (빈 값 — 이 줄은 안 나옵니다)';
          out.className = 'fxout ' + (res.text ? 'fxok' : 'muted');
        }
      })
      .catch(function(){ /* 잠깐 끊긴 것뿐이다 */ });
  }, 250);
}
document.addEventListener('DOMContentLoaded', function(){
  document.querySelectorAll('.fx').forEach(fxPreview);
});
/* 열 순서 바꾸기·빼기. 저장하러 갔다 오지 않고 여기서 끝낸다 — 열 하나 옮기려고
   페이지를 왕복하면 표를 만들 엄두가 안 난다. */
function rowMove(btn, 어디){
  var tr = btn.closest('tr'), 형제 = 어디 < 0 ? tr.previousElementSibling
                                            : tr.nextElementSibling;
  if(!형제 || !형제.querySelector('input')) return;   /* 머리글 줄은 건너뛴다 */
  if(어디 < 0) tr.parentNode.insertBefore(tr, 형제);
  else tr.parentNode.insertBefore(형제, tr);
}
function rowDrop(btn){
  var tr = btn.closest('tr'), 몸 = tr.parentNode;
  tr.querySelectorAll('input').forEach(function(el){ el.value = ''; });
  if(몸.querySelectorAll('tr').length > 2) tr.remove();   /* 빈 줄 하나는 남긴다 */
}
