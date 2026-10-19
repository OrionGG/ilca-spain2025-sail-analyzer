// Browser snapshot of the app's rendered output, for refactors that must not change it.
//
// It drives the page only through its DOM (race / focus / compare selectors, tab buttons,
// #tabbody), so it keeps working while the scripts behind the page are restructured.
// The page contract it relies on: #raceSel, #focusSel, #partnerSel, #tabs button[data-tab],
// #tabbody, #resetOrigBtn (shown when a race has hand-placed marks).
//
// Use (see tests/README.md): open the app on the copa2025 regatta, paste this file into the
// browser console (or run it through a browser tool), then
//     const snap = await uiSnapshot();                 // {races: [{race, tabs: {tab: {h, n}}}]}
//     uiCompare(REFERENCE, snap)                       // [] when nothing changed
//     await uiSnapshot({full: true, races: ['Prueba 3'], tabs: ['debrief']})   // texts, to diff by eye

async function uiSnapshot(opt = {}) {
  const FOCUS = opt.focus || '206341';
  const $ = s => document.querySelector(s);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const hash = async s => {
    const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
    return [...new Uint8Array(d)].slice(0, 10).map(b => b.toString(16).padStart(2, '0')).join('');
  };
  const settle = async (ok, ms = 20000) => {            // until ok() and #tabbody stops changing
    const t0 = Date.now(); let last = null, since = Date.now();
    while (Date.now() - t0 < ms) {
      const txt = $('#tabbody').innerText;
      if (txt !== last) { last = txt; since = Date.now(); }
      if (ok() && Date.now() - since > 400) return;
      await sleep(100);
    }
    throw new Error('page did not settle');
  };
  const pick = async (sel, value) => {
    const el = $(sel);
    if (el.value === String(value)) return;
    el.value = String(value);
    el.dispatchEvent(new Event('change'));
  };
  const raceSel = $('#raceSel');
  const races = [...raceSel.options].map(o => ({ value: o.value, name: o.textContent.split('·')[0].trim() }));
  const tabs = [...document.querySelectorAll('#tabs button[data-tab]')].map(b => b.dataset.tab);
  const out = { app: 'sail-analyzer-ui-snapshot', version: 1, focus: FOCUS, tabs, races: [] };
  for (const r of races) {
    if (opt.races && !opt.races.includes(r.name)) continue;
    if (raceSel.value !== r.value) {
      // the loaded race re-renders the tab body: wait for that, not just for the select
      $('#tabbody').innerHTML = '<i data-snapshot-wait></i>';
      await pick('#raceSel', r.value);
      await settle(() => raceSel.value === r.value && !$('#tabbody [data-snapshot-wait]'));
    }
    if (![...$('#focusSel').options].some(o => o.value === FOCUS))
      throw new Error(`${r.name}: focus boat ${FOCUS} is not in this race`);
    await pick('#focusSel', FOCUS);
    await pick('#partnerSel', '');
    const row = { race: r.name, manualMarks: $('#resetOrigBtn') ? $('#resetOrigBtn').style.display !== 'none' : null, tabs: {} };
    for (const t of tabs) {
      if (opt.tabs && !opt.tabs.includes(t)) continue;
      $(`#tabs button[data-tab="${t}"]`).click();
      await settle(() => $(`#tabs button[data-tab="${t}"]`).classList.contains('on'));
      const txt = norm($('#tabbody').innerText);
      row.tabs[t] = opt.full ? txt : { h: await hash(txt), n: txt.length };
    }
    out.races.push(row);
  }
  return out;
}

function uiCompare(ref, cur) {
  const diffs = [];
  if (ref.focus !== cur.focus) diffs.push(`focus ${ref.focus} -> ${cur.focus}`);
  if (String(ref.tabs) !== String(cur.tabs)) diffs.push(`tabs ${ref.tabs} -> ${cur.tabs}`);
  const byName = Object.fromEntries(cur.races.map(r => [r.race, r]));
  for (const r of ref.races) {
    const c = byName[r.race];
    if (!c) { diffs.push(`${r.race}: missing`); continue; }
    if (c.manualMarks) diffs.push(`${r.race}: has hand-placed marks — reset them before comparing`);
    for (const [t, v] of Object.entries(r.tabs)) {
      const w = c.tabs[t];
      if (!w) diffs.push(`${r.race} / ${t}: missing`);
      else if (w.h !== v.h) diffs.push(`${r.race} / ${t}: text changed (${v.n} -> ${w.n} chars)`);
    }
  }
  for (const c of cur.races) if (!ref.races.some(r => r.race === c.race)) diffs.push(`${c.race}: new race`);
  return diffs;
}
