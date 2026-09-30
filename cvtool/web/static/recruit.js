
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
