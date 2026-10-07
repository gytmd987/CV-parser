/* 오른쪽 위 «?» — 사용법을 옆 창으로 연다. 지금 보고 있는 화면의 설명부터 보여 준다.
   창 밖을 누르거나 Esc 를 누르면 닫힌다. 새 창(/help)으로도 열 수 있다. */
(function(){
  var 내용 = null;
  function 닫기(){
    ['helpdrawer', 'helpdim'].forEach(function(id){
      var el = document.getElementById(id); if(el) el.remove(); });
  }
  function 열기(장){
    닫기();
    var 어둠 = document.createElement('div'); 어둠.id = 'helpdim'; 어둠.onclick = 닫기;
    var 창 = document.createElement('aside'); 창.id = 'helpdrawer';
    창.setAttribute('role', 'dialog'); 창.setAttribute('aria-label', '사용법');
    창.innerHTML = "<div class='hhead'><b>사용법</b>"
      + "<a href='/help#" + 장 + "' target='_blank' rel='noopener'>새 창으로 보기 ↗</a>"
      + "<button type='button' aria-label='닫기' title='닫기 (Esc)'>×</button></div>"
      + "<div class='hscroll'>불러오는 중…</div>";
    창.querySelector('button').onclick = 닫기;
    document.body.appendChild(어둠); document.body.appendChild(창);
    var 자리 = 창.querySelector('.hscroll');
    function 채우기(html){
      자리.innerHTML = html;
      var 장면 = 자리.querySelector('#' + 장);
      if(장면){ 장면.classList.add('now'); 자리.scrollTop = 장면.offsetTop - 자리.offsetTop - 8; }
      자리.querySelectorAll('.htoc a').forEach(function(a){
        a.onclick = function(e){
          e.preventDefault();
          var 대상 = 자리.querySelector(a.getAttribute('href'));
          if(대상) 자리.scrollTo({top: 대상.offsetTop - 자리.offsetTop - 8, behavior: 'smooth'});
        };
      });
    }
    if(내용){ 채우기(내용); return; }
    fetch('/help?part=1', {credentials: 'same-origin'})
      .then(function(r){ return r.text(); })
      .then(function(t){ 내용 = t; 채우기(t); })
      .catch(function(){ 자리.textContent = '사용법을 불러오지 못했습니다. 새 창으로 열어 보세요.'; });
  }
  document.addEventListener('click', function(e){
    var a = e.target.closest && e.target.closest('a[data-help]');
    if(!a || e.ctrlKey || e.metaKey || e.shiftKey) return;   /* 새 탭으로 열려는 것은 그대로 */
    e.preventDefault(); 열기(a.dataset.help || 'start');
  });
  document.addEventListener('keydown', function(e){
    if(e.key === 'Escape' && document.getElementById('helpdrawer')) 닫기();
  });
  /* /help 화면에서 #조각으로 온 장을 표시 */
  if(location.pathname === '/help' && location.hash){
    var 장면 = document.querySelector(location.hash);
    if(장면) 장면.classList.add('now');
  }
})();
