/* 편집 화면 «이렇게 보입니다» 의 표 — 머리글 오른쪽 경계를 끌어 열 너비를 바꾼다.
   엑셀처럼: 끄는 동안 바로 넓어지고, 놓으면 **그때 보이는 모든 열 너비**를 저장한다
   (한 열만 정하면 나머지가 남은 자리를 나눠 가져 끈 만큼 안 움직인다).
   블록 폼의 너비 칸도 같이 고친다 — 안 그러면 «이 블록 저장» 이 옛 값으로 덮는다. */
(function(){
  var 끝폭 = 6, 끄는중 = null;
  function 머리(e){
    var th = e.target.closest && e.target.closest('.blockout table[data-wblock] th[data-wkey]');
    if(!th) return null;
    var r = th.getBoundingClientRect();
    return (r.right - e.clientX <= 끝폭) ? th : null;
  }
  document.addEventListener('mousemove', function(e){
    if(끄는중){
      var w = Math.max(30, Math.round(끄는중.시작폭 + e.clientX - 끄는중.시작x));
      끄는중.th.style.width = w + 'px';
      끄는중.표.style.width = (끄는중.합 - 끄는중.시작폭 + w) + 'px';
      return;
    }
    var th = 머리(e);
    document.querySelectorAll('th.wgrab').forEach(function(x){ if(x !== th) x.classList.remove('wgrab'); });
    if(th) th.classList.add('wgrab');
  });
  document.addEventListener('mousedown', function(e){
    var th = 머리(e);
    if(!th) return;
    e.preventDefault();
    var 표 = th.closest('table'), 합 = 0;
    /* 지금 보이는 폭으로 모든 열을 못박는다. **다 재고 나서** 박는다 — 하나씩
       재며 박으면 앞 열을 박는 순간 뒤 열 폭이 바뀌어 엉뚱하게 넓어졌다. */
    var 머리들 = Array.prototype.slice.call(표.querySelectorAll('tr:first-child th[data-wkey]'));
    var 잰것 = 머리들.map(function(x){ return Math.round(x.getBoundingClientRect().width); });
    머리들.forEach(function(x, i){ x.style.width = 잰것[i] + 'px'; 합 += 잰것[i]; });
    표.classList.add('fixed'); 표.style.width = 합 + 'px';
    끄는중 = {th: th, 표: 표, 시작x: e.clientX, 시작폭: Math.round(th.getBoundingClientRect().width), 합: 합};
    document.body.classList.add('wdragging');
  });
  document.addEventListener('mouseup', function(){
    if(!끄는중) return;
    var 표 = 끄는중.표; 끄는중 = null;
    document.body.classList.remove('wdragging');
    var bid = 표.dataset.wblock, 몸 = new URLSearchParams(), 폭들 = {};
    몸.set('id', bid);
    표.querySelectorAll('tr:first-child th[data-wkey]').forEach(function(x){
      var w = Math.round(x.getBoundingClientRect().width);
      몸.append('key', x.dataset.wkey); 몸.append('px', w); 폭들[x.dataset.wkey] = w;
    });
    폼칸고치기(bid, 폭들);
    fetch('/dash/block/widths', {method: 'POST', body: 몸, credentials: 'same-origin'})
      .then(function(r){ return r.json(); })
      .then(function(j){ if(!j.ok) alert(j.error || '너비를 저장하지 못했습니다.'); })
      .catch(function(){ alert('너비를 저장하지 못했습니다.'); });
  });
  function 폼칸고치기(bid, 폭들){
    var 폼 = document.getElementById('bf' + bid);
    if(!폼) return;
    var 바뀐것 = null;
    /* 목록 — n번째 열 줄의 너비 칸 */
    var 목록칸 = 폼.querySelectorAll("input[name='colwidth']");
    /* 축표·표 — 열 이름이 같은 너비 칸 */
    폼.querySelectorAll("input[name='colwname']").forEach(function(이름칸){
      var 칸 = 이름칸.parentNode.querySelector("input[name='colw']");
      if(칸 && 폭들.hasOwnProperty(이름칸.value)){ 칸.value = 폭들[이름칸.value]; 바뀐것 = 칸; }
    });
    Object.keys(폭들).forEach(function(k){
      if(/^[0-9]+$/.test(k) && 목록칸[+k]){ 목록칸[+k].value = 폭들[k]; 바뀐것 = 목록칸[+k]; }
    });
    if(바뀐것 && window.wsum) wsum(바뀐것);            /* 합계 글도 따라 */
  }
})();
