
(function(){
  var 목록 = window.수식목록 || {열: [], 행함수: [], 집계함수: [], 대상: []};
  var 상자 = null, 지금칸 = null, 후보 = [], 고른것 = -1;
  /* 함수 설명 {이름: [인자들, 되풀이, 설명, 예]} — `funcdocs.py` 가 채운다 */
  var 설명 = 목록.설명 || {};
  function 막기(t){ return String(t).replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }

  function 낱말(el){
    var 앞 = el.value.slice(0, el.selectionStart);
    var m = 앞.match(/[0-9A-Za-z_가-힣]+$/);
    return m ? m[0] : '';
  }
  function 살것(el){
    var 갈 = el.dataset.kind || 'row';
    /* 시트 칸은 **둘 다** 쓴다 — 집계로 사람을 세고, 행함수로 그 값을 다듬고,
       칸 주소로 옆 칸을 가져온다. 한쪽만 보여주면 반쪽짜리가 된다. */
    var 집계 = (갈 === 'agg' || 갈 === 'sheet');
    var 행 = (갈 === 'row' || 갈 === 'sheet');
    var 것 = [], 본것 = {};
    function 담기(n, 갈래, 함수){
      /* `채용` 은 열이면서 대상이다. 두 번 뜨면 잘못 만든 것처럼 보인다 —
         한 줄로 합치고 갈래만 이어 적는다. */
      if(본것[n]){
        if((' · ' + 본것[n].갈래 + ' · ').indexOf(' · ' + 갈래 + ' · ') < 0) 본것[n].갈래 += ' · ' + 갈래;
        if(함수) 본것[n].함수 = 1;
        return;
      }
      본것[n] = {글: n, 갈래: 갈래, 함수: 함수 || 0};
      것.push(본것[n]);
    }
    if(집계) 목록.대상.forEach(function(n){ 담기(n, '대상'); });
    목록.열.forEach(function(n){ 담기(n, '열'); });
    if(집계) 목록.집계함수.forEach(function(n){ 담기(n, '함수', 1); });
    if(행) 목록.행함수.forEach(function(n){ 담기(n, '함수', 1); });
    return 것;
  }
  function 고르기(el, 말){
    /* **한 글자는 있어야 뜬다.** 빈 칸에 목록이 통째로 뜨면 칸을 누르기만 해도
       화면을 가리고, `=COUNT(` 처럼 괄호 뒤에서도 떠서 Enter 를 가로챈다.
       무엇을 쓸 수 있는지는 «쓸 수 있는 열 이름 전부» 를 펼쳐 보면 된다. */
    if(!말) return [];
    /* 숫자만 친 것은 완성하지 않는다. `1` 을 치고 Enter 를 누르면
       `1저자_우수_제출처` 가 들어가 버렸다 — 칸에 숫자를 적는 건 흔한 일이다. */
    if(/^[0-9.]+$/.test(말)) return [];
    /* 시트 칸에 그냥 글(`이름`)을 적을 때는 완성하지 않는다. Tab 으로 옆 칸에
       가려다 `영문_이름` 으로 바뀌어 버렸다. 수식(`=` 로 시작)일 때만 돕는다. */
    if((el.dataset.kind || '') === 'sheet' && !/^\s*=/.test(el.value)) return [];
    var 전부 = 살것(el);
    var 낮 = 말.toLowerCase(), 앞 = [], 안 = [];
    전부.forEach(function(x){
      var t = x.글.toLowerCase();
      if(t.indexOf(낮) === 0) 앞.push(x);
      else if(t.indexOf(낮) > 0) 안.push(x);
    });
    return 앞.concat(안).slice(0, 12);
  }
  function 닫기(){
    if(상자){ 상자.remove(); 상자 = null; }
    지금칸 = null; 후보 = []; 고른것 = -1;
  }
  function 그리기(el){
    var 말 = 낱말(el);
    후보 = 고르기(el, 말);
    if(!후보.length){ 닫기(); return; }
    if(!상자){
      상자 = document.createElement('div');
      상자.id = 'fxdrop';
      document.body.appendChild(상자);
    }
    지금칸 = el;
    고른것 = 0;
    상자.innerHTML = 후보.map(function(x, i){
      var 뜻 = x.함수 && 설명[x.글] ? 설명[x.글][2] : '';
      return "<div class='it" + (i === 0 ? ' on' : '') + "' data-i='" + i + "'>"
           + '<b>' + 막기(x.글) + '</b>'
           + (뜻 ? "<span class='desc'>" + 막기(뜻) + '</span>' : '')
           + '<i>' + x.갈래 + '</i></div>';
    }).join('') + "<div class='foot'>Tab 넣기 · ↑↓ 고르기 · Esc 닫기</div>";
    var r = el.getBoundingClientRect();
    상자.style.left = (r.left + window.scrollX) + 'px';
    상자.style.top = (r.bottom + window.scrollY + 3) + 'px';
    상자.style.minWidth = Math.min(r.width, 520) + 'px';
    상자.querySelectorAll('.it').forEach(function(d){
      d.addEventListener('mousedown', function(e){
        e.preventDefault();                     /* blur 보다 먼저 잡는다 */
        넣기(+d.dataset.i);
      });
    });
  }
  function 표시(){
    if(!상자) return;
    상자.querySelectorAll('.it').forEach(function(d, i){
      d.classList.toggle('on', i === 고른것);
      if(i === 고른것 && d.scrollIntoView) d.scrollIntoView({block: 'nearest'});
    });
  }
  function 넣기(i){
    if(!지금칸 || !후보[i]) return;
    var el = 지금칸, x = 후보[i];
    var 끝 = el.selectionStart, 말 = 낱말(el);
    var 앞 = el.value.slice(0, 끝 - 말.length);
    var 뒤 = el.value.slice(끝);
    var 넣을것 = x.글 + (x.함수 ? '(' : '');
    el.value = 앞 + 넣을것 + 뒤;
    var 자리 = (앞 + 넣을것).length;
    닫기();
    el.focus();
    el.setSelectionRange(자리, 자리);
    if(window.fxPreview) fxPreview(el);         /* 미리보기도 따라 바뀐다 */
  }

  document.addEventListener('input', function(e){
    if(e.target.classList && e.target.classList.contains('fx')) 그리기(e.target);
  });
  document.addEventListener('focusout', function(e){
    if(e.target.classList && e.target.classList.contains('fx')) setTimeout(닫기, 120);
  });
  document.addEventListener('keydown', function(e){
    if(!상자 || e.target !== 지금칸) return;
    if(e.key === 'ArrowDown'){
      e.preventDefault(); 고른것 = (고른것 + 1) % 후보.length; 표시();
    }else if(e.key === 'ArrowUp'){
      e.preventDefault(); 고른것 = (고른것 - 1 + 후보.length) % 후보.length; 표시();
    }else if(e.key === 'Tab'){
      /* **완성은 Tab 으로만 한다.** Enter 로도 완성하던 동안에는, 다 적고
         저장하려고 Enter 를 쳤을 뿐인데 목록이 열려 있으면 엉뚱한 이름이
         들어가 버렸다. Enter 는 «다 적었다», Tab 은 «골라 달라» 로 가른다. */
      e.preventDefault(); 넣기(고른것);
    }else if(e.key === 'Enter'){
      /* 목록을 닫기만 하고 **비켜 준다.** 그래야 칸의 Enter 처리(저장)가
         이어서 돈다 — 그쪽은 `fxdrop` 이 없어야 움직인다. */
      닫기();
    }else if(e.key === 'Escape'){
      e.preventDefault(); 닫기();
    }
  }, true);
  window.addEventListener('scroll', 닫기, true);
  window.addEventListener('resize', 닫기);

  /* -- 인자 안내 (엑셀처럼) ---------------------------------------------------
     커서가 함수 괄호 안에 있으면 `COUNTIFS(범위1, 조건1, …)` 를 띄우고 **지금
     적는 인자**를 굵게 한다. 함수 안의 함수면 가장 안쪽 것을 보여 준다. */
  var 안내 = null;
  function 지금함수(글){
    var 쌓임 = [], 따 = '';
    for(var i = 0; i < 글.length; i++){
      var c = 글[i];
      if(따){ if(c === 따) 따 = ''; continue; }
      if(c === '"'){ 따 = c; continue; }
      if(c === '('){
        var m = 글.slice(0, i).match(/([A-Za-z_][A-Za-z0-9_]*)\s*$/);
        쌓임.push({이름: m ? m[1].toUpperCase() : '', 번째: 0});
      }else if(c === ')'){ 쌓임.pop(); }
      else if(c === ',' && 쌓임.length){ 쌓임[쌓임.length - 1].번째++; }
    }
    for(var j = 쌓임.length - 1; j >= 0; j--) if(설명[쌓임[j].이름]) return 쌓임[j];
    return null;
  }
  function 안내닫기(){ if(안내){ 안내.remove(); 안내 = null; } }
  function 안내그리기(el){
    if(!el || !el.classList || !el.classList.contains('fx') || el.classList.contains('nav')
       || document.activeElement !== el){ 안내닫기(); return; }
    var 지금 = 지금함수(el.value.slice(0, el.selectionStart));
    if(!지금){ 안내닫기(); return; }
    var d = 설명[지금.이름], 인자 = d[0], 되풀이 = d[1];
    /* 되풀이되는 짝(범위2, 조건2 …)도 몇 번째인지 맞춰 굵게 한다 */
    var 몇 = 지금.번째, 굵게 = 몇;
    if(몇 >= 인자.length && 되풀이) 굵게 = 인자.length - 되풀이 + ((몇 - 인자.length) % 되풀이);
    var 조각 = 인자.map(function(a, i){
      var t = 막기(a);
      if(몇 >= 인자.length && 되풀이 && i === 굵게){
        /* 짝을 넘어섰으면 번호를 맞춰 보인다: 범위1 → 범위3 */
        var 차례 = Math.floor((몇 - (인자.length - 되풀이)) / 되풀이) + 1;
        t = 막기(a.replace(/1(\]?)$/, 차례 + '$1').replace(/^\[/, '').replace(/\]$/, ''));
      }
      return i === 굵게 ? '<b>' + t + '</b>' : t;
    });
    if(되풀이) 조각.push(몇 >= 인자.length ? '<b>…</b>' : '…');
    if(!안내){
      안내 = document.createElement('div'); 안내.id = 'fxsig';
      document.body.appendChild(안내);
    }
    안내.innerHTML = "<div class='sig'><span class='fn'>" + 지금.이름 + '</span>('
      + 조각.join(', ') + ')</div>'
      + "<div class='what'>" + 막기(d[2]) + '</div>'
      + (d[3] ? "<div class='ex'>예: <code>" + 막기(d[3]) + '</code></div>' : '');
    var r = el.getBoundingClientRect();
    안내.style.left = (r.left + window.scrollX) + 'px';
    안내.style.top = (r.top + window.scrollY - 안내.offsetHeight - 4) + 'px';
  }
  ['input', 'keyup', 'click', 'focusin'].forEach(function(ev){
    document.addEventListener(ev, function(e){
      if(e.target.classList && e.target.classList.contains('fx')) 안내그리기(e.target);
    });
  });
  document.addEventListener('focusout', function(e){
    if(e.target.classList && e.target.classList.contains('fx')) setTimeout(function(){
      if(!document.activeElement || !document.activeElement.classList
         || !document.activeElement.classList.contains('fx')) 안내닫기();
    }, 120);
  });
  document.addEventListener('keydown', function(e){
    if(e.key === 'Escape' && 안내 && !상자) 안내닫기();
  });
  window.addEventListener('scroll', function(){ if(안내) 안내그리기(document.activeElement); }, true);
})();
