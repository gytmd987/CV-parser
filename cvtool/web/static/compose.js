
function 작성창(tid, cid){
  var 주소 = '/mail/draft?tpl=' + tid + '&id=' + encodeURIComponent(cid);
  var 창 = window.open(주소, 'maildraft_' + cid,
    'width=920,height=900,scrollbars=yes,resizable=yes');
  if(창) 창.focus();
  else 토스트('창이 막혔습니다. 브라우저 주소창 옆의 팝업 차단을 풀어 주세요.', 1);
}
/* 작성창이 보내고 나서 부른다 */
function 보냄표시(cid, 글){
  var 줄 = document.querySelector("#보낼목록 tr[data-cid='" + cid + "']");
  if(!줄) return;
  var 칸 = 줄.querySelector('.보냄칸');
  if(칸) 칸.innerHTML = "<b class='ok'>" + (글 || '보냄') + "</b>";
  var 단추 = 줄.querySelector('button');
  if(단추){ 단추.disabled = true; 단추.textContent = '보냄'; }
  토스트((줄.cells[0].textContent || '') + ' — 보냈습니다.');
}
