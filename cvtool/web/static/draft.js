
(function(){
  var 폼 = document.getElementById('쓰기폼');
  var 칸 = document.getElementById('본문칸');
  var 값 = document.getElementById('본문값');
  if(!폼 || !칸 || !값) return;
  function 담기(){
    값.value = (칸.tagName === 'TEXTAREA') ? 칸.value : 칸.innerHTML;
  }
  폼.addEventListener('submit', function(e){
    담기();
    /* 첨부 체크를 바꿔 다시 그리는 것은 확인창 없이 그냥 보낸다 */
    var 보내기 = e.submitter && e.submitter.name === 'send';
    if(보내기 && !window.confirm('이 내용으로 보냅니다. 되돌릴 수 없습니다.')){
      e.preventDefault();
    }
  });
  담기();
})();
