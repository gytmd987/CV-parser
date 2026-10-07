
function syncProjects(sel){
  var cid = sel.dataset.cid;
  var proj = document.getElementById('proj-' + cid);
  if(!proj) return;
  var 목록 = (window.과제표 || 과제표)[sel.value] || [];
  proj.innerHTML = '';
  var 빈 = document.createElement('option');
  빈.value = ''; 빈.textContent = '-';
  proj.appendChild(빈);
  목록.forEach(function(pair){
    var op = document.createElement('option');
    op.value = pair[0]; op.textContent = pair[1];
    proj.appendChild(op);
  });
  proj.value = '';
  markDirty(proj);
}

/* 단계 요약의 칩을 누르면 그 단계·상태인 사람만 보인다. 한 번 더 누르거나
   «전체 보기» 를 누르면 다 보인다. */
function stFilter(btn){
  var 요약 = btn.closest('.stsum');
  var 켤것 = btn.dataset.f && !btn.classList.contains('on') ? btn.dataset.f : '';
  요약.querySelectorAll('.stchip').forEach(function(b){
    b.classList.toggle('on', !!켤것 && b === btn); });
  var 전체 = 요약.querySelector('.stchip.all');
  if(전체) 전체.style.display = 켤것 ? '' : 'none';
  document.querySelectorAll("table[data-name='채용현황'] tr").forEach(function(tr, i){
    if(i === 0) return;                         /* 머리줄 */
    tr.style.display = (!켤것 || tr.classList.contains(켤것)) ? '' : 'none';
  });
}
