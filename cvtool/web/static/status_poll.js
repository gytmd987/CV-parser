
(function(){
  var 표 = document.getElementById('현황표');
  var 알림 = document.getElementById('상태알림');
  if(!표 || !알림 || !알림.dataset.busy) return;
  var 타이머 = setInterval(function(){
    fetch('/status/rows', {credentials: 'same-origin'})
      .then(function(r){ return r.ok ? r.text() : null; })
      .then(function(html){
        if(html === null) return;
        var 담을것 = document.createElement('div');
        담을것.innerHTML = html;
        var 새표 = 담을것.querySelector('#현황표');
        var 새알림 = 담을것.querySelector('#상태알림');
        if(새표) 표.innerHTML = 새표.innerHTML;
        if(새알림){
          알림.innerHTML = 새알림.innerHTML;
          if(!새알림.dataset.busy){        /* 다 끝났다 — 그만 두드린다 */
            알림.dataset.busy = '';
            clearInterval(타이머);
          }
        }
      })
      .catch(function(){ /* 잠깐 끊긴 것뿐이다. 다음 차례에 다시 해본다 */ });
  }, 3000);
})();
