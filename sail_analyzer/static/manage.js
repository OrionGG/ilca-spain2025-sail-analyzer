'use strict';
// Regatta & race manager: create regattas, add races from a folder of TracTrac
// KML tracks (copied into the app by upload), set each race's course (legs +
// marks, from a template or custom) and an optional gun time.

const MG = { rid: null, form: null };

function courseText(c){
  return (c||[]).map((l,i)=>`${l.type}→${esc(l.mark)}${l.gate?' (gate)':''}${i===c.length-1?' (finish)':''}`).join(' · ');
}
function mgRegatta(){ return S.regattas.find(g=>g.id===MG.rid); }
function viewedRaceId(){ return S.index&&S.index[S.raceIdx]?S.index[S.raceIdx].id:undefined; }

function openManager(){
  MG.rid = S.regatta || (S.regattas[0]&&S.regattas[0].id) || null;
  MG.form = null;
  $('#mgModal').style.display='flex';
  renderManager();
}
function closeManager(){ $('#mgModal').style.display='none'; }

function renderManager(msg){
  const g = mgRegatta(), body = $('#mgBody');
  let h = `<div class="filterbar"><b style="color:var(--fg)">Regatta</b>
    <select id="mgReg">${S.regattas.map(x=>`<option value="${x.id}" ${x.id===MG.rid?'selected':''}>${esc(x.name)}</option>`).join('')}</select>
    <button class="btn" id="mgNewReg">+ New regatta</button>
    ${g?'<button class="btn" id="mgRenReg">Rename</button><button class="btn danger" id="mgDelReg">Delete</button>':''}</div>`;
  if(g){
    h += `<table class="mgt"><tr><th>Race</th><th style="text-align:left">Course</th><th>Gun (UTC)</th><th>Tracks</th><th></th></tr>`;
    g.races.forEach(r=>{
      h += `<tr><td>${esc(r.name)}${r.error?`<div class="bad" style="font-size:11px;max-width:260px">${esc(r.error)}</div>`:''}</td><td style="text-align:left;font-size:11.5px">${courseText(r.course)}</td>
        <td>${r.gun?esc(r.gun):'<span class="muted">auto</span>'}</td>
        <td>${r.n_files}${r.analysed?'':` <span class="bad" title="${esc(r.error||'not analysed yet')}">!</span>`}</td>
        <td style="white-space:nowrap"><button class="btn" data-edit="${r.id}">Edit</button><button class="btn danger" data-del="${r.id}">✕</button></td></tr>`;
    });
    if(!g.races.length) h += `<tr><td colspan=5 class="muted" style="text-align:left">No races yet.</td></tr>`;
    h += `</table><div style="margin-top:8px"><button class="btn primary" id="mgAdd">+ Add race</button></div>`;
    h += resultsBox(g);
  } else h += `<div class="muted">Create a regatta to start.</div>`;
  h += `<div id="mgForm"></div><div id="mgMsg" class="muted" style="margin-top:8px">${msg?esc(msg):''}</div>`;
  body.innerHTML = h;
  $('#mgReg') && ($('#mgReg').onchange = e => { MG.rid = e.target.value; MG.form = null; renderManager(); });
  $('#mgNewReg').onclick = newRegatta;
  if(g){
    $('#mgRenReg').onclick = renameRegatta;
    $('#mgDelReg').onclick = deleteRegatta;
    $('#mgAdd').onclick = () => { MG.form = blankForm(); renderForm(); };
    body.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => {
      const r = g.races.find(x=>x.id===b.dataset.edit);
      MG.form = {mode:'edit', id:r.id, name:r.name, course:r.course.map(l=>({...l})), gun:r.gun||'', files:[], folder:''};
      renderForm(); });
    body.querySelectorAll('[data-del]').forEach(b => b.onclick = () => deleteRace(b.dataset.del));
    wireResultsBox(g);
  }
  if(MG.form) renderForm();
}

function blankForm(){
  const g = mgRegatta(), prev = g&&g.races.length ? g.races[g.races.length-1] : null;
  const course = prev ? prev.course.map(l=>({...l})) : S.templates[0].course.map(l=>({...l}));
  return {mode:'add', name:`Race ${(g?g.races.length:0)+1}`, course, gun:'', files:[], folder:''};
}

function renderForm(){
  const f = MG.form, g = mgRegatta(), el = $('#mgForm'); if(!el) return;
  const opts = S.templates.map(t=>`<option value="t:${t.id}">${esc(t.name)}</option>`).join('') +
    (g.races.filter(r=>r.id!==f.id).map(r=>`<option value="r:${r.id}">Copy from ${esc(r.name)}</option>`).join(''));
  let rows = '';
  f.course.forEach((l,i)=>{ const last = i===f.course.length-1;
    rows += `<tr><td>${i+1}</td>
      <td><select data-k="type" data-i="${i}">${[['U','Upwind'],['R','Reach'],['D','Downwind']].map(([v,n])=>`<option value="${v}" ${l.type===v?'selected':''}>${n}</option>`).join('')}</select></td>
      <td><input data-k="mark" data-i="${i}" value="${esc(l.mark)}" maxlength="6" size="5"></td>
      <td>${last?'<span class="muted">finish</span>':`<input type="checkbox" data-k="gate" data-i="${i}" ${l.gate?'checked':''}>`}</td>
      <td style="white-space:nowrap"><button class="btn" data-mv="${i}" data-d="-1" ${i?'':'disabled'}>↑</button><button class="btn" data-mv="${i}" data-d="1" ${last?'disabled':''}>↓</button><button class="btn danger" data-rm="${i}">✕</button></td></tr>`; });
  el.innerHTML = `<div class="mgform">
    <div class="sectitle" style="margin-top:4px">${f.mode==='add'?'Add race':'Edit race'}</div>
    <label>Name <input id="mgName" value="${esc(f.name)}" maxlength="60"></label>
    <label>${f.mode==='add'?'Tracks folder':'Replace tracks (optional)'}
      <input type="file" id="mgDir" webkitdirectory directory multiple>
      <span class="muted" id="mgDirInfo">${f.dirError?`<span class="bad">${esc(f.dirError)}</span>`:f.files.length?`${f.files.length} .kml files${f.folder?' in '+esc(f.folder):''}`:(f.mode==='add'?'choose the folder with the race\'s .kml files':'keep current tracks')}</span></label>
    <label>Course <select id="mgTpl"><option value="">— start from a template —</option>${opts}</select></label>
    <table class="mgt"><tr><th>#</th><th>Leg</th><th>Ends at mark</th><th>Gate</th><th></th></tr>${rows}</table>
    <div style="margin:6px 0"><button class="btn" id="mgAddLeg">+ Add leg</button>
      <span class="muted">Each leg ends at its mark; the last leg ends at the finish line. Same label = same mark (e.g. 2 rounded twice);
      a moved mark gets a new label (2a / 2b). Shortened races are detected automatically.</span></div>
    <label>Gun time <input id="mgGun" value="${esc(f.gun)}" placeholder="auto" size="9">
      <span class="muted">UTC, HH:MM:SS — leave empty to auto-detect (assumes tracking starts ~5 min before the gun)</span></label>
    <div style="margin-top:10px"><button class="btn primary" id="mgSave" ${f.saving?'disabled':''}>${f.mode==='add'?'Add & analyse':'Save & re-analyse'}</button>
      <button class="btn" id="mgCancel">Cancel</button></div></div>`;
  const sync = () => { f.name = $('#mgName').value; f.gun = $('#mgGun').value; };
  $('#mgDir').onchange = e => {
    const all = [...e.target.files].filter(x=>/\.kml$/i.test(x.name));
    const top = all.filter(x=>(x.webkitRelativePath||x.name).split('/').length<=2);
    const use = top.length ? top : all, seen = new Set();
    const dup = use.some(x=>{ const k = x.name.toLowerCase(); if(seen.has(k)) return true; seen.add(k); return false; });
    f.dirError = dup ? 'This folder has same-named .kml files in different subfolders — choose the race\'s own folder.' : '';
    f.files = dup ? [] : use;
    f.folder = f.files.length ? (f.files[0].webkitRelativePath||'').split('/')[0] : '';
    if(f.mode==='add' && f.folder && /^Race \d+$/.test(f.name)) f.name = f.folder.replace(/^\d+[_ -]+/,'');
    sync(); renderForm(); };
  $('#mgTpl').onchange = e => { const v = e.target.value; if(!v) return; sync();
    const src = v.startsWith('t:') ? S.templates.find(t=>'t:'+t.id===v).course : g.races.find(r=>'r:'+r.id===v).course;
    f.course = src.map(l=>({...l})); renderForm(); };
  el.querySelectorAll('[data-k]').forEach(inp => inp.onchange = () => { const l = f.course[+inp.dataset.i];
    if(inp.dataset.k==='gate') l.gate = inp.checked; else l[inp.dataset.k] = inp.value.trim(); });
  el.querySelectorAll('[data-mv]').forEach(b => b.onclick = () => { sync(); const i=+b.dataset.mv, j=i+(+b.dataset.d);
    [f.course[i], f.course[j]] = [f.course[j], f.course[i]]; renderForm(); });
  el.querySelectorAll('[data-rm]').forEach(b => b.onclick = () => { sync(); f.course.splice(+b.dataset.rm,1); renderForm(); });
  $('#mgAddLeg').onclick = () => { sync(); f.course.push({type:'U', mark:'', gate:false}); renderForm(); };
  $('#mgCancel').onclick = () => { MG.form = null; renderManager(); };
  $('#mgSave').onclick = () => { sync(); saveRace(); };
}

function mgStatus(t){ const m = $('#mgMsg'); if(m) m.textContent = t; }

// Upload with 4 parallel workers; after the first failure no new file starts and
// the error is raised only once every in-flight upload has finished.
async function uploadAll(rid, raceId, files){
  let done = 0, err = null;
  const queue = files.slice();
  const worker = async () => { while(queue.length && !err){ const file = queue.shift();
    try{
      await api(`/api/regatta/${rid}/race/${raceId}/file?name=${encodeURIComponent(file.name)}`, file, {raw:true});
      mgStatus(`Uploading tracks… ${++done}/${files.length}`);
    }catch(e){ err = err || new Error(`${file.name}: ${e.message}`); } } };
  await Promise.all([worker(), worker(), worker(), worker()]);
  if(err) throw err;
}

// New tracks go to a staging folder and are analysed there; the race's tracks, course,
// gun and name change only if that succeeds, and a failed add removes the new race again.
async function saveRace(){
  const f = MG.form, rid = MG.rid;
  if(f.saving) return;
  if(f.mode==='add' && !f.files.length) return mgStatus('Choose the folder with the race\'s .kml files.');
  if(f.course.some(l=>!l.mark)) return mgStatus('Every leg needs the mark it ends at.');
  f.saving = true; $('#mgSave').disabled = true;
  let fresh = false;
  try{
    const payload = {name:f.name, course:f.course.map((l,i)=>({...l, gate:i<f.course.length-1&&!!l.gate})), gun:f.gun};
    if(f.mode==='add'){ f.id = (await api(`/api/regatta/${rid}/races`, payload)).id; f.mode = 'edit'; fresh = true; }
    const base = `/api/regatta/${rid}/race/${f.id}`;
    if(f.files.length){                        // new tracks: analysed first, swapped in only if good
      await api(`${base}/stage`, {});
      await uploadAll(rid, f.id, f.files);
      mgStatus('Analysing… (10–30 s for a big fleet)');
      await api(`${base}/commit`, payload);
    } else {                                   // course/gun change is analysed before it's saved
      mgStatus('Analysing… (10–30 s for a big fleet)');
      const res = await api(`${base}/update`, payload);
      if(res.warning) f.warning = res.warning;
    }
    fresh = false;                             // saved: nothing to roll back from here on
    await loadRegattas(rid, f.id);
    f.saving = false;
    if(MG.form !== f) return;                  // the user opened another form meanwhile: keep it
    MG.form = null;
    renderManager(`Saved “${f.name}”.`+(f.warning?` Analysis not updated: ${f.warning}`:''));
  }catch(e){
    if(fresh){                                 // roll back the race created by this attempt
      try{ await api(`/api/regatta/${rid}/race/${f.id}/delete`, {}); }catch(_){}
      f.mode = 'add'; delete f.id;
    } else if(f.files.length){                 // drop the uploaded copies of the failed edit
      try{ await api(`/api/regatta/${rid}/race/${f.id}/unstage`, {}); }catch(_){}
    }
    f.saving = false;
    if(MG.form !== f){ await fetchRegattas(); return; }
    $('#mgSave').disabled = false;
    await fetchRegattas();
    mgStatus('Error: '+e.message+' — fix it and save again.');
  }
}

// Official results: download from the regatta's results page, map result columns to races
function resultsBox(g){
  const R = g.results;
  let h = `<div class="sectitle">Official results</div>
    <div class="filterbar"><input id="mgResUrl" size="52" placeholder="link to the results page (e.g. Sailti)" value="${esc(R&&R.url||'')}">
    <button class="btn primary" id="mgResGet">${R?'Download again':'Download'}</button>${R?'<button class="btn danger" id="mgResDel">Remove</button>':''}</div>`;
  if(R){
    h += `<div class="muted">${R.n} boats, ${R.columns.length} race columns · downloaded ${esc((R.fetched||'').replace('T',' ').slice(0,16))} UTC.
      Finish places in the analysis use these results; the GPS estimate is shown alongside.</div>
      <table class="mgt" style="margin-top:6px"><tr><th>Results column</th><th style="text-align:left">Race</th></tr>`+
      R.columns.map((c,i)=>`<tr><td>${esc(c)}</td><td><select data-rmap="${i}"><option value="">— not a tracked race —</option>`+
        g.races.map(r=>`<option value="${r.id}" ${R.map[i]===r.id?'selected':''}>${esc(r.name)}</option>`).join('')+`</select></td></tr>`).join('')+'</table>';
  }
  return h;
}
function wireResultsBox(g){
  $('#mgResGet').onclick = async () => {
    const url = $('#mgResUrl').value.trim(); if(!url) return mgStatus('Paste the link of the results page.');
    $('#mgResGet').disabled = true; mgStatus('Downloading results…');
    try{ const j = await api(`/api/regatta/${g.id}/results`, {url}); await fetchRegattas(); await reloadResults(g.id);
      renderManager(`Official results downloaded: ${j.n} boats, ${j.columns.length} races.`); }
    catch(e){ $('#mgResGet').disabled = false; mgStatus('Error: '+e.message); }
  };
  const del = $('#mgResDel');
  if(del) del.onclick = async () => {
    if(!confirm('Remove the official results of this regatta?')) return;
    try{ await api(`/api/regatta/${g.id}/results/delete`, {}); await fetchRegattas(); await reloadResults(g.id); renderManager('Official results removed.'); }
    catch(e){ mgStatus('Error: '+e.message); }
  };
  document.querySelectorAll('#mgBody [data-rmap]').forEach(sel => sel.onchange = async () => {
    const map = [...document.querySelectorAll('#mgBody [data-rmap]')].map(x => x.value || null);
    try{ await api(`/api/regatta/${g.id}/results/map`, {map}); await fetchRegattas(); await reloadResults(g.id); mgStatus('Mapping saved.'); }
    catch(e){ mgStatus('Error: '+e.message); }
  });
}
async function reloadResults(rid){
  if(S.regatta !== rid) return;
  const tok = S._ixTok;
  let res = null; try{ res = (await api('/api/results/'+rid)).results; }catch(e){}
  if(S.regatta !== rid || tok !== S._ixTok) return;     // another regatta (or a reload) took over
  S.results = res;
  if(S.race) setTab(S.tab);
}

async function newRegatta(){
  const name = prompt('Regatta name (e.g. "Europa Cup Hyères 2026"):'); if(!name||!name.trim()) return;
  try{ const j = await api('/api/regattas', {name}); MG.rid = j.id; await fetchRegattas();
    MG.form = blankForm(); renderManager('Regatta created — add its first race.'); }
  catch(e){ mgStatus('Error: '+e.message); }
}
async function renameRegatta(){
  const g = mgRegatta(), name = prompt('New name:', g.name); if(!name||!name.trim()) return;
  try{ await api(`/api/regatta/${g.id}/rename`, {name}); await fetchRegattas();
    if(S.regatta===g.id){ $('#regTitle').textContent=name.trim(); document.title='Sail Analyzer — '+name.trim(); }
    renderManager(); }
  catch(e){ mgStatus('Error: '+e.message); }
}
async function deleteRegatta(){
  const g = mgRegatta();
  if(!confirm(`Delete regatta “${g.name}” and its ${g.races.length} race(s)?\nUploaded track copies and caches are removed; your original folders are not touched.`)) return;
  try{ await api(`/api/regatta/${g.id}/delete`, {}); MG.rid = null; MG.form = null;   // the form belonged to it
    if(S.regatta===g.id) await loadRegattas(null); else await fetchRegattas();
    MG.rid = S.regatta||(S.regattas[0]&&S.regattas[0].id)||null; renderManager(); }
  catch(e){ mgStatus('Error: '+e.message); }
}
async function deleteRace(raceId){
  const g = mgRegatta(), r = g.races.find(x=>x.id===raceId);
  if(!confirm(`Delete race “${r.name}”?\n${r.uploaded?'Its uploaded track copies are removed.':'Your original folder is not touched.'}`)) return;
  try{ await api(`/api/regatta/${g.id}/race/${raceId}/delete`, {});
    if(S.regatta===g.id) await loadRegattas(g.id, viewedRaceId()===raceId?undefined:viewedRaceId());
    else await fetchRegattas();
    MG.form = null; renderManager(`Deleted “${r.name}”.`); }
  catch(e){ mgStatus('Error: '+e.message); }
}

$('#mgBtn').onclick = openManager;
$('#mgClose').onclick = closeManager;
$('#mgModal').onclick = e => { if(e.target.id==='mgModal') closeManager(); };
