'use strict';
// ILCA Sail Analyzer — offline client. Vanilla JS, canvas rendering.

const S = {
  regattas: [], regatta: null,  // registry from /api/regattas; current regatta id
  index: [], race: null, raceIdx: 0,
  focus: '206341', partner: '',
  tMin: 0, tMax: 1, t: 0,
  playing: false, colorMode: 'tack', showFleet: true,
  view: null, tab: 'overview', charts: [],
  legFilter: 'all', liftCmp: 'fleet',
  byS: {},                       // sail -> boat
};
const KN = 1.94384, ILCA = 4.2;
const $ = s => document.querySelector(s);
const map = $('#map'), ctx = map.getContext('2d');
let bg = document.createElement('canvas'), bgx = bg.getContext('2d');
let DPR = Math.min(devicePixelRatio || 1, 2);

// ---------- utils ----------
const fmtT = s => { s = Math.max(0, s|0); return String(s/60|0).padStart(2,'0')+':'+String(s%60).padStart(2,'0'); };
const fmtClock = ep => new Date(ep*1000).toISOString().slice(11,19)+'Z';   // HH:MM:SS UTC
const med = a => { a = a.filter(v=>v!=null).sort((x,y)=>x-y); return a.length? a[a.length>>1] : null; };
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// Server call: GET without a body, else POST (JSON, or the raw body with {raw:true}).
// Throws the server's error message.
async function api(path, body, opt={}){
  const init = body===undefined ? {} : {method:'POST', body:opt.raw?body:JSON.stringify(body),
    headers:{'X-Sail-Analyzer':'1', ...(opt.raw?{}:{'Content-Type':'application/json'})}};   // server rejects cross-site POSTs
  const r=await fetch(path, init), j=await r.json().catch(()=>null);
  if(!r.ok || !j || j.ok===false) throw new Error((j&&j.error)||('HTTP '+r.status));
  return j;
}
function angColor(deg){ return `hsl(${deg},70%,55%)`; }
function lerp(a,b,f){ return a+(b-a)*f; }

// binary search for time index in sorted boat.t
function idxAt(b, t){
  const T=b.t; let lo=0, hi=T.length-1;
  if(t<=T[0]) return 0; if(t>=T[hi]) return hi;
  while(hi-lo>1){ const m=(lo+hi)>>1; if(T[m]<=t) lo=m; else hi=m; }
  return lo;
}
function sampleAt(b, t){
  const i=idxAt(b,t), j=Math.min(i+1,b.t.length-1);
  const f = b.t[j]>b.t[i] ? (t-b.t[i])/(b.t[j]-b.t[i]) : 0;
  return {
    i, lat:lerp(b.lat[i],b.lat[j],f), lon:lerp(b.lon[i],b.lon[j],f),
    sog:lerp(b.sog[i],b.sog[j],f), cog:b.cog[i], twa:lerp(b.twa[i],b.twa[j],f),
    vmg:lerp(b.vmg[i],b.vmg[j],f), tack:b.tack[i],
    inrace: t>=b.t[0] && t<=b.t[b.t.length-1]
  };
}

// ---------- map projection ----------
function pct(sorted,p){ const i=(sorted.length-1)*p; const lo=i|0; const f=i-lo;
  return sorted[lo]+(sorted[Math.min(lo+1,sorted.length-1)]-sorted[lo])*f; }
function computeView(){
  const r=S.race;
  // Frame on what is reliably on-course: the focus track (it sails the whole
  // course), the marks and the start line. The fleet is then clipped to a robust
  // percentile so a parked/glitched tracker km away can't blow out the view.
  let mnLa=1e9,mxLa=-1e9,mnLo=1e9,mxLo=-1e9;
  const inc=(la,lo)=>{ if(la<mnLa)mnLa=la; if(la>mxLa)mxLa=la; if(lo<mnLo)mnLo=lo; if(lo>mxLo)mxLo=lo; };
  const f=S.byS[S.focus], p=S.partner?S.byS[S.partner]:null;
  for(const b of [f,p]) if(b) for(let k=0;k<b.lat.length;k++) inc(b.lat[k],b.lon[k]);
  for(const m of effMarks()) if(m.ll) inc(m.ll[0],m.ll[1]);
  if(r.start_line) for(const e of r.start_line) inc(e[0],e[1]);
  if(mnLa>mxLa){ // fallback (no focus): robust fleet percentile
    const las=[],los=[]; for(const b of r.boats) for(let k=0;k<b.lat.length;k+=4){ las.push(b.lat[k]); los.push(b.lon[k]); }
    las.sort((a,b)=>a-b); los.sort((a,b)=>a-b);
    mnLa=pct(las,0.02);mxLa=pct(las,0.98);mnLo=pct(los,0.02);mxLo=pct(los,0.98);
  }
  S.view={mnLa,mxLa,mnLo,mxLo,lat0:r.lat0,cosL:Math.cos(r.lat0*Math.PI/180),
          zoom:1,panX:0,panY:0};
  layout();
}
function layout(){
  const w=mapwrap().clientWidth, h=mapwrap().clientHeight;
  for(const c of [map,bg]){ c.width=w*DPR; c.height=h*DPR; }
  map.style.width=w+'px'; map.style.height=h+'px';
  const v=S.view, pad=40*DPR;
  const gx=(v.mxLo-v.mnLo)*v.cosL, gy=(v.mxLa-v.mnLa);
  const sx=(map.width-2*pad)/gx, sy=(map.height-2*pad)/gy;
  v.scale=Math.min(sx,sy);
  v.ox=(map.width-gx*v.scale)/2; v.oy=(map.height-gy*v.scale)/2;
}
function mapwrap(){ return $('#mapwrap'); }
function proj(lat,lon){
  const v=S.view;
  const px=v.ox+((lon-v.mnLo)*v.cosL)*v.scale;
  const py=map.height - (v.oy+((lat-v.mnLa))*v.scale);   // invert Y (north up)
  return [ px*v.zoom+v.panX, py*v.zoom+v.panY ];          // user zoom + pan
}
function unproj(sx,sy){ const v=S.view;
  const px=(sx-v.panX)/v.zoom, py=(sy-v.panY)/v.zoom;
  return [ v.mnLa+((map.height-py)-v.oy)/v.scale, v.mnLo+((px-v.ox)/v.scale)/v.cosL ]; }

// ---------- marks: auto-detected, user-editable ----------
// Three states per race: ORIGINAL (auto-detected, from server), SAVED (an explicit
// snapshot), and WORKING (live edits, auto-persisted so a reload keeps them).
function marksKey(){ return 'sa_marks_'+(S.race?S.race.key:''); }       // working
function savedKey(){ return 'sa_marksaved_'+(S.race?S.race.key:''); }   // saved snapshot
function autoMarks(){ return (S.race&&S.race.marks)?S.race.marks.map(m=>({label:m.label,ll:m.ll.slice()})):[]; }
function effMarks(){
  if(S._marks) return S._marks;
  let ov=null; try{ const s=localStorage.getItem(marksKey()); if(s) ov=JSON.parse(s); }catch(e){}
  S._marks = ov || autoMarks();
  return S._marks;
}
function marksOverridden(){ try{ return !!localStorage.getItem(marksKey()); }catch(e){ return false; } }
function hasSaved(){ try{ return !!localStorage.getItem(savedKey()); }catch(e){ return false; } }
function saveMarks(){ try{ localStorage.setItem(marksKey(), JSON.stringify(S._marks)); }catch(e){} }  // working autosave
function saveSnapshot(){ try{ localStorage.setItem(savedKey(), JSON.stringify(effMarks())); }catch(e){}
  const b=$('#saveMarksBtn'); if(b){ const o=b.textContent; b.textContent='Saved ✓'; setTimeout(()=>b.textContent=o,1200); }
  updateMarkButtons(); }
function resetOriginal(){ try{ localStorage.removeItem(marksKey()); }catch(e){} S._marks=null; afterMarksChanged(); updateMarkButtons(); }
function resetSaved(){ let s=null; try{ s=localStorage.getItem(savedKey()); }catch(e){}
  if(!s) return; try{ localStorage.setItem(marksKey(), s); }catch(e){} S._marks=null; afterMarksChanged(); updateMarkButtons(); }
function updateMarkButtons(){
  const on=S.editMarks;
  $('#saveMarksBtn').style.display = on?'':'none';
  $('#resetOrigBtn').style.display = on?'':'none';
  const rs=$('#resetSavedBtn'); rs.style.display = (on&&hasSaved())?'':'none';
}
function nearestMark(px,py){ let bi=-1,bd=1e9; effMarks().forEach((m,i)=>{ const [x,y]=proj(m.ll[0],m.ll[1]);
  const d=Math.hypot(x-px,y-py); if(d<bd){bd=d;bi=i;} }); return {i:bi,d:bd}; }
function nearestMark(px,py){ let bi=-1,bd=1e9; effMarks().forEach((m,i)=>{ const [x,y]=proj(m.ll[0],m.ll[1]);
  const d=Math.hypot(x-px,y-py); if(d<bd){bd=d;bi=i;} }); return {i:bi,d:bd}; }

// ---------- legs derived from the (possibly hand-placed) marks ----------
function toXY(lat,lon){ const r=S.race, cosL=Math.cos(r.lat0*Math.PI/180);
  return [(lon-r.lon0)*111320*cosL, (lat-r.lat0)*111320]; }
function distM(la1,lo1,la2,lo2){ const cosL=Math.cos(la1*Math.PI/180);
  return Math.hypot((lo1-lo2)*111320*cosL,(la1-la2)*111320); }
function markBy(label){ return effMarks().filter(m=>m.label===label).map(m=>m.ll); }

// The race's course: legs {type U|R|D, mark, gate}; each leg ends at its mark,
// the last one at the finish.
function courseLegs(){ return (S.race&&S.race.course)||[]; }
const LEG_KIND={U:'upwind',R:'reach',D:'downwind'};
function legMarkLabel(i){ const c=courseLegs(), leg=c[i]; if(!leg) return 'L'+(i+1);
  if(i===c.length-1) return 'Finish';
  const n=c.slice(0,i).filter(x=>x.mark===leg.mark).length;
  const base=leg.gate?(leg.mark==='G'?'Gate':'Gate '+leg.mark):'M'+leg.mark;
  return n?`${base} (${n===1?'2nd':n===2?'3rd':(n+1)+'th'})`:base; }
// For a boat, each leg ends at the closest approach to its target mark; if a
// later mark is never reached (shortened race / retirement) we stop there.
function legsFromMarks(b){
  const c=courseLegs(); if(c.length<2) return null;
  const seq=[];
  for(let i=0;i<c.length;i++){ const ll=markBy(c[i].mark);
    if(!ll.length) break;
    seq.push({ll,k:LEG_KIND[c[i].type]}); }
  const n=b.lat.length, R=70;
  const segD=(i,A,B)=>{ const [px,py]=toXY(b.lat[i],b.lon[i]), [ax,ay]=toXY(A[0],A[1]), [bx,by]=toXY(B[0],B[1]);
    const vx=bx-ax, vy=by-ay, L=vx*vx+vy*vy, f=L?Math.max(0,Math.min(1,((px-ax)*vx+(py-ay)*vy)/L)):0;
    return Math.hypot(px-ax-f*vx, py-ay-f*vy); };
  const md=(i,lls,line)=>line&&lls.length===2?segD(i,lls[0],lls[1]):Math.min(...lls.map(ll=>distM(b.lat[i],b.lon[i],ll[0],ll[1])));
  const g0 = S.race.gun_t ? idxAt(b,S.race.gun_t) : 0;   // leg 1 starts at the gun
  const legs=[]; let start=g0, prev=g0;
  for(const s of seq){
    const line=s===seq[seq.length-1]&&seq.length===c.length;   // the finish: a line between its two marks
    let i=prev+1; while(i<n && md(i,s.ll,line)>R) i++;        // enter the mark's zone
    if(i>=n) break;
    let bi=i, bd=md(i,s.ll,line);
    while(i<n && md(i,s.ll,line)<=R*1.6){ const d=md(i,s.ll,line); if(d<bd){bd=d;bi=i;} i++; }  // closest pt in pass
    if(bi-start<2) break;                                 // degenerate (e.g. finish == gate)
    legs.push([start,bi,s.k]); start=bi; prev=bi;
  }
  return legs.length>=2 ? legs : null;
}
function courseFromLegs(b,legs){      // cumulative made-good along each leg axis, on the grid
  const n=b.lat.length, course=new Array(n).fill(0); let acc=0;
  for(const [a,c] of legs){
    const [x0,y0]=toXY(b.lat[a],b.lon[a]), [x1,y1]=toXY(b.lat[c],b.lon[c]);
    let dx=x1-x0, dy=y1-y0; const nrm=Math.hypot(dx,dy);
    if(c<=a||nrm<1){ for(let i=a;i<n;i++)course[i]=acc; continue; }
    dx/=nrm; dy/=nrm;
    for(let i=a;i<=c;i++){ const [x,y]=toXY(b.lat[i],b.lon[i]); course[i]=acc+((x-x0)*dx+(y-y0)*dy); }
    acc=course[c]; for(let i=c+1;i<n;i++)course[i]=acc;
  }
  return S.race.fleet_stats.t.map(t=>{ if(t<b.t[0]||t>b.t[n-1]) return null;
    const i=idxAt(b,t), j=Math.min(i+1,n-1), f=b.t[j]>b.t[i]?(t-b.t[i])/(b.t[j]-b.t[i]):0;
    return course[i]+(course[j]-course[i])*f; });
}
function applyManualMarks(){
  const on=marksOverridden(); if(S.race) S.race._finCi=null;
  for(const b of S.race.boats){
    if(on){ const lg=legsFromMarks(b); b._mlegs=lg; b._mcourse=lg?courseFromLegs(b,lg):null; }
    else { b._mlegs=null; b._mcourse=null; }
  }
  S.usingManualMarks=on;
}
function boatLegs(b){ return b._mlegs||b.legs; }
function boatCourse(b){ return b._mcourse||b.course; }

// ---------- ideal route = REALISTIC: fastest actually-sailed track per leg ----------
// Per leg index, pick the boat that did that leg in least time; stitch its real
// track. So the ideal = best demonstrated path+speed each leg, not a straight line.
function legBest(){
  const f=focusB(), best=[];
  boatLegs(f).forEach((_,li)=>{ const ci=courseIdx(f,li); let bt=Infinity,bb=null;
    for(const o of finishers()){ const lj=ciMap(o)[ci]; if(lj==null) continue;     // same course leg
      const [a,c]=boatLegs(o)[lj], tm=o.t[c]-o.t[a]; if(tm>0 && tm<bt){ bt=tm; bb={sail:o.sail,a,c,t:tm}; } }
    best.push(bb); });
  return best;
}
function idealRoute(){ const wps=[];
  for(const bb of legBest()){ if(!bb) continue; const o=S.byS[bb.sail];
    for(let j=bb.a;j<=bb.c;j+=2) wps.push([o.lat[j],o.lon[j]]); } return wps; }
function idealTime(){ return legBest().reduce((s,bb)=>s+(bb?bb.t:0),0); }      // sum of fastest leg times
function raceTime(b){ const lg=boatLegs(b); return lg.length? b.t[lg[lg.length-1][1]]-b.t[lg[0][0]] : 0; }
function sailedDist(b){ const lg=boatLegs(b); if(!lg.length) return 0; const a=lg[0][0],c=lg[lg.length-1][1];
  let s=0; for(let j=a+1;j<=c;j++) s+=distM(b.lat[j-1],b.lon[j-1],b.lat[j],b.lon[j]); return s; }
function afterMarksChanged(){ applyManualMarks(); renderBG(); render(); if(S.race) setTab(S.tab); }

// ---------- background layer (static tracks/marks) ----------
function renderBG(){
  const r=S.race; bgx.clearRect(0,0,bg.width,bg.height); if(!r) return;
  // faint fleet tracks
  bgx.lineWidth=1*DPR; bgx.strokeStyle='rgba(120,135,155,.18)';
  for(const b of r.boats){
    if(b.sail===S.focus||b.sail===S.partner) continue;
    drawTrackPlain(b);
  }
  // start line drawn between the (editable) RC and Pin marks
  const rc=markBy('RC')[0], pin=markBy('Pin')[0];
  if(rc&&pin){ const p1=proj(rc[0],rc[1]), p2=proj(pin[0],pin[1]);
    bgx.strokeStyle='rgba(245,166,35,.75)'; bgx.lineWidth=2*DPR;
    bgx.setLineDash([6*DPR,5*DPR]); bgx.beginPath();
    bgx.moveTo(p1[0],p1[1]); bgx.lineTo(p2[0],p2[1]); bgx.stroke(); bgx.setLineDash([]); }
  // finish line between its two marks (where the finishers' tracks converge)
  const cl=courseLegs(), fin=cl.length?markBy(cl[cl.length-1].mark):[];
  if(fin.length===2){ const p1=proj(fin[0][0],fin[0][1]), p2=proj(fin[1][0],fin[1][1]);
    bgx.strokeStyle='rgba(120,220,140,.8)'; bgx.lineWidth=2*DPR; bgx.setLineDash([6*DPR,5*DPR]); bgx.beginPath();
    bgx.moveTo(p1[0],p1[1]); bgx.lineTo(p2[0],p2[1]); bgx.stroke(); bgx.setLineDash([]); }
  // ideal route = fastest demonstrated track per leg (real paths, stitched)
  if(S.showIdeal){ const wps=idealRoute();
    if(wps.length>1){ bgx.strokeStyle='rgba(90,210,255,.9)'; bgx.lineWidth=2.4*DPR; bgx.setLineDash([3*DPR,4*DPR]);
      bgx.beginPath(); wps.forEach((p,i)=>{ const [x,y]=proj(p[0],p[1]); i?bgx.lineTo(x,y):bgx.moveTo(x,y); }); bgx.stroke(); bgx.setLineDash([]); } }
  // course marks (labels from the race's course; RC/Pin = start line ends)
  for(const m of effMarks()) drawMark(m.ll, m.label);
  if(S.showTacks){ drawTackCalls(S.byS[S.focus],false);       // tack-decision markers
    if(S.partner&&S.byS[S.partner]) drawTackCalls(S.byS[S.partner],true); }
  // partner track: same per-segment colouring as focus, thinner + dimmed
  if(S.partner && S.byS[S.partner]) drawTrackColored(S.byS[S.partner], true);
  // focus full track (colored by mode)
  if(S.byS[S.focus]) drawTrackColored(S.byS[S.focus], false);
}
function drawTrackPlain(b){
  bgx.beginPath();
  for(let k=0;k<b.lat.length;k++){ const [x,y]=proj(b.lat[k],b.lon[k]); k?bgx.lineTo(x,y):bgx.moveTo(x,y); }
  bgx.stroke();
}
// per-point leg kind + per-kind averages (cached per boat; reset on marks edit)
function kindArr(b){ if(b._ka && b._kaRef===boatLegs(b)) return b._ka;
  const ka=new Array(b.lat.length).fill(null);
  for(const [a,c,k] of boatLegs(b)) for(let j=a;j<=c;j++) ka[j]=k;
  b._ka=ka; b._kaRef=boatLegs(b); b._kavg=null; return ka; }
function madeGood(b,i,kind){ return kind==='downwind'?-b.vmg[i]:kind==='reach'?b.sog[i]:b.vmg[i]; }
function kindAvgs(b){ if(b._kavg) return b._kavg;
  const ka=kindArr(b), s={},v={},n={};
  for(let i=0;i<b.lat.length;i++){ const k=ka[i]; if(!k) continue;
    s[k]=(s[k]||0)+b.sog[i]; v[k]=(v[k]||0)+madeGood(b,i,k); n[k]=(n[k]||0)+1; }
  const o={}; for(const k in n) o[k]={sog:s[k]/n[k], mg:v[k]/n[k]};
  b._kavg=o; return o; }
function drawTrackColored(b, isPartner){
  bgx.lineWidth=(isPartner?1.8:2.6)*DPR; bgx.lineJoin='round';
  if(S.colorMode==='fleet'){ b._gain=computeGain(b,fleetRef()); }
  else if(S.colorMode==='cmp'){ const other=isPartner?focusB():partnerB();
    b._gain=other?computeGain(b,boatRef(other)):null; }        // null = no reference
  kindArr(b);
  if(isPartner) bgx.globalAlpha=0.78;
  for(let k=1;k<b.lat.length;k++){
    const [x0,y0]=proj(b.lat[k-1],b.lon[k-1]), [x1,y1]=proj(b.lat[k],b.lon[k]);
    bgx.strokeStyle=segColor(b,k);
    bgx.beginPath(); bgx.moveTo(x0,y0); bgx.lineTo(x1,y1); bgx.stroke();
  }
  bgx.globalAlpha=1;
}
// Per-point gain vs a reference (fleet or another boat) for the current leg type:
// upwind/downwind use VMG-to-mark (downwind better = more negative VMG), reaches use SOG. >0 = ahead.
function computeGain(b, ref){    // ref(t) -> {vmg,sog} | null
  const kind=kindArr(b);
  const gain=new Array(b.lat.length).fill(null);
  for(let k=0;k<b.lat.length;k++){ const rf=ref(b.t[k]); if(!rf) continue;
    const kd=kind[k]||'upwind';
    if(kd==='downwind') gain[k]=rf.vmg-b.vmg[k];
    else if(kd==='reach') gain[k]=b.sog[k]-rf.sog;
    else gain[k]=b.vmg[k]-rf.vmg; }
  return gain;
}
function fleetRef(){ const fs=S.race.fleet_stats, G=fs.t;
  const it=(arr,t)=>{ if(t<=G[0])return arr[0]; if(t>=G[G.length-1])return arr[G.length-1];
    let i=0; while(i<G.length-1&&G[i+1]<t)i++; const a=arr[i],c=arr[i+1];
    if(a==null||c==null)return a==null?c:a; return a+(c-a)*(t-G[i])/(G[i+1]-G[i]); };
  return t=>({vmg:it(fs.vmg_p50,t), sog:it(fs.sog_p50,t)}); }
function boatRef(o){ if(!o) return null;
  return t=>{ const s=sampleAt(o,t); return s.inrace?{vmg:s.vmg,sog:s.sog}:null; }; }
function segColor(b,k){
  if(S.colorMode==='tack') return b.tack[k]<0 ? '#3f9bff' : '#ff5e7a';   // tack<0 = starboard (blue), >0 = port (red)
  if(S.colorMode==='air'){ const c=b.shadow?b.shadow[gk(b.t[k])]:null;     // dirty air = another boat upwind within ~8 BL
    return c==null||c==='-'?'rgba(150,160,175,.35)':c==='0'?'rgba(225,230,238,.75)':'rgba(190,90,255,.95)'; }
  const kd=(b._ka&&b._ka[k])||null;
  if(S.colorMode==='speed'){                     // speed RELATIVE to own avg for this leg type
    if(!kd) return 'rgba(150,160,175,.45)';
    const av=kindAvgs(b)[kd]; if(!av) return 'rgba(150,160,175,.45)';
    const f=Math.max(-1,Math.min(1,(b.sog[k]/av.sog-1)/0.25));
    return `hsl(${(1-(f+1)/2)*220+10},85%,55%)`; }             // blue slow -> red fast, per kind
  if(S.colorMode==='fleet'||S.colorMode==='cmp'){              // green=ahead, red=behind reference
    if(!b._gain) return 'rgba(150,160,175,.55)';               // no comparison (e.g. no partner)
    const g=b._gain[k]; if(g==null) return 'rgba(150,160,175,.5)';
    const m=0.4+0.55*Math.min(1,Math.abs(g)/1.2);
    return g>=0?`rgba(63,185,80,${m})`:`rgba(248,81,73,${m})`; }
  // vmg: made-good to the mark RELATIVE to own avg for this leg type (works downwind too)
  if(!kd) return 'rgba(150,160,175,.45)';
  const av=kindAvgs(b)[kd]; if(!av||!av.mg) return 'rgba(150,160,175,.45)';
  const f=Math.max(-1,Math.min(1,(madeGood(b,k,kd)/av.mg-1)/0.25));
  return f>=0?`rgba(80,200,255,${.35+.6*f})`:`rgba(255,120,84,${.35+.6*-f})`;
}
function drawMark(ll,label){
  if(!ll) return; const [x,y]=proj(ll[0],ll[1]);
  bgx.fillStyle='#ffd166'; bgx.beginPath(); bgx.arc(x,y,(S.editMarks?7:5)*DPR,0,7); bgx.fill();
  if(S.editMarks){ bgx.strokeStyle='#fff'; bgx.lineWidth=1.5*DPR; bgx.stroke(); }
  bgx.fillStyle='#ffd166'; bgx.font=`${12*DPR}px sans-serif`; bgx.fillText(label,x+9*DPR,y-7*DPR);
}

// ---------- dynamic layer (boat dots at current time) ----------
function render(){
  if(!map.width||!map.height||!bg.width||!bg.height) return;   // not laid out yet
  ctx.clearRect(0,0,map.width,map.height);
  if(!S.race) return;
  ctx.drawImage(bg,0,0);
  const r=S.race;
  if(S.showFleet){
    for(const b of r.boats){
      if(b.sail===S.focus||b.sail===S.partner) continue;
      const s=sampleAt(b,S.t); if(!s.inrace) continue;
      const [x,y]=proj(s.lat,s.lon);
      ctx.fillStyle='rgba(150,165,185,.55)'; ctx.beginPath(); ctx.arc(x,y,2.6*DPR,0,7); ctx.fill();
    }
  }
  if(S.partner&&S.byS[S.partner]) dot(S.byS[S.partner],'#3aa0ff',5.5);
  if(S.byS[S.focus]) dot(S.byS[S.focus],'#f5a623',6.5);
}
function dot(b,col,rad){
  const s=sampleAt(b,S.t); if(!s.inrace) return;
  const [x,y]=proj(s.lat,s.lon);
  // heading tick
  const a=(s.cog-90)*Math.PI/180;
  ctx.strokeStyle=col; ctx.lineWidth=2.5*DPR; ctx.beginPath();
  ctx.moveTo(x,y); ctx.lineTo(x+Math.cos(a)*14*DPR,y+Math.sin(a)*14*DPR); ctx.stroke();
  ctx.fillStyle=col; ctx.beginPath(); ctx.arc(x,y,rad*DPR,0,7); ctx.fill();
  ctx.strokeStyle='#0d1117'; ctx.lineWidth=1.5*DPR; ctx.stroke();
}

// ---------- playback ----------
let raf=null, lastTs=0;
function tick(ts){
  if(!S.playing) return;
  const dt=lastTs?(ts-lastTs)/1000:0; lastTs=ts;
  S.t+=dt*12;                                   // 12x real-time
  if(S.t>=S.tMax){ S.t=S.tMax; setPlaying(false); }
  syncTime(); raf=requestAnimationFrame(tick);
}
function setPlaying(p){
  S.playing=p; $('#playBtn').textContent=p?'❚❚':'▶';
  if(p){ lastTs=0; raf=requestAnimationFrame(tick); } else if(raf) cancelAnimationFrame(raf);
}
function syncTime(){
  const rel=S.t-S.tMin;
  $('#timeRange').value=(rel/(S.tMax-S.tMin)*1000)|0;
  $('#tLabel').textContent=fmtT(rel);
  render(); updateHover(); S.charts.forEach(c=>c.cursor&&c.cursor());
}

// live fleet position at time S.t (by course progress, finishers + focus/partner)
function liveRank(boat){
  if(!boat||!S.race) return null;
  const G=S.race.fleet_stats.t;
  const k=Math.max(0,Math.min(G.length-1,Math.round((S.t-G[0])/5)));
  const pool=finishers().slice(); for(const b of [focusB(),partnerB()]) if(b&&!pool.includes(b)) pool.push(b);
  const my=boatCourse(boat)[k]; if(my==null) return null;
  let r=1; for(const o of pool){ if(o===boat) continue; const v=boatCourse(o)[k]; if(v!=null&&v>my) r++; }
  return {rank:r, of:pool.length};
}

// ---------- hover readout ----------
function updateHover(){
  const f=S.byS[S.focus]; if(!f) return;
  const s=sampleAt(f,S.t);
  let html=`<div class="h-name">${f.name}</div>`;
  if(s.inrace){
    const av=w=>{ const lo=idxAt(f,S.t-w), hi=idxAt(f,S.t); let q=0,n=0; for(let k=lo;k<=hi;k++){q+=f.vmg[k];n++;} return n?(q/n).toFixed(2):'–'; };
    // direction-aware "gain to mark": upwind=+VMG, downwind=-VMG, reach=SOG (higher always better)
    const ci=idxAt(f,S.t); let kn='upwind', legA=null, legN=0;
    boatLegs(f).forEach(([a,c,k],i)=>{ if(legA==null && ci>=a && ci<=c){ kn=k; legA=a; legN=legNo(f,i); } });
    let legAvg='–'; if(legA!=null){ let q=0,nq=0; for(let k=legA;k<=ci;k++){q+=f.vmg[k];nq++;} legAvg=(q/nq).toFixed(2); }
    const am=w=>{ const lo=idxAt(f,S.t-w), hi=idxAt(f,S.t); let q=0,n=0;
      for(let k=lo;k<=hi;k++){ q+=(kn==='downwind'?-f.vmg[k]:kn==='reach'?f.sog[k]:f.vmg[k]); n++; } return n?q/n:0; };
    const m5=am(5),m30=am(30),m180=am(180);
    // 3-state vs the reference tier (the longer window): neutral = within ±3% of it
    // (self-scaling dead zone, so steady sailing reads neutral; colour = real change).
    // steady (within ±3% of reference) or rising = green ▲ ("fine, keep going"); only a
    // genuine drop below the dead zone = red ▼.
    const arr=(short,long)=>{ const dead=0.03*Math.abs(long), d=short-long;
      return d<-dead?'<b style="color:#f85149">▼</b>':'<b style="color:#3fb950">▲</b>'; };
    const flr=liveRank(f);
    html+=(flr?`Pos <b>P${flr.rank}</b>/${flr.of}<br>`:'')+
          `SOG <b>${s.sog.toFixed(2)}</b> kt<br>TWA ${s.twa.toFixed(0)}° (${s.tack<0?'Stbd':'Port'})<br>`+
          `VMG ${s.vmg.toFixed(2)} kt<br>COG ${s.cog.toFixed(0)}°<br>`+
          `<span style="color:var(--mut)">avg VMG 5s ${av(5)} · 30s ${av(30)} · 3m ${av(180)}</span><br>`+
          (legA!=null?`<span style="color:var(--mut)">leg ${legN} (${kn}) avg VMG so far ${legAvg} kt</span><br>`:'')+
          `<span style="color:var(--mut)">Technique 5v30 ${arr(m5,m30)} &nbsp; Strategy 30v3m ${arr(m30,m180)}</span>`;
    if(S.partner&&S.byS[S.partner]){
      const ps=sampleAt(S.byS[S.partner],S.t);
      if(ps.inrace){
        // row: real partner value + delta vs focus
        const row=(lbl,val,d,u,dp=2,good=null)=>{ const sign=d>=0?'+':''; const cls=good==null?'':(good?'good':'bad');
          return `${lbl} ${val.toFixed(dp)}${u} <b class="${cls}">(${sign}${d.toFixed(dp)})</b><br>`; };
        const dCog=((s.cog-ps.cog+540)%360)-180;          // signed heading diff
        const plr=liveRank(S.byS[S.partner]);
        html+=`<hr style="border-color:#2b333f;margin:5px 0">`+
          `<span style="color:#3aa0ff">${S.byS[S.partner].name}</span><br>`+
          (plr?`Pos <b>P${plr.rank}</b>/${plr.of}<br>`:'')+
          row('SOG',ps.sog,s.sog-ps.sog,' kt',2,s.sog-ps.sog>=0)+
          row('TWA',ps.twa,s.twa-ps.twa,'°',0)+
          row('VMG',ps.vmg,s.vmg-ps.vmg,' kt',2)+
          `COG ${ps.cog.toFixed(0)}° <b>(${dCog>=0?'+':''}${dCog.toFixed(0)})</b>`;
      }
    }
  } else html+='<span class="muted">not on course</span>';
  $('#hover').innerHTML=html;
}

// ---------- charts ----------
function makeChart(parent, opts){
  const cv=document.createElement('canvas'); cv.className='chart';
  cv.style.height=(opts.h||150)+'px'; parent.appendChild(cv);
  const c=cv.getContext('2d');
  function draw(){
    const w=cv.clientWidth, h=opts.h||150; cv.width=w*DPR; cv.height=h*DPR; c.setTransform(DPR,0,0,DPR,0,0);
    c.clearRect(0,0,w,h);
    const L=42,R=10,T=8,B=20, pw=w-L-R, ph=h-T-B;
    let xmin=opts.xmin, xmax=opts.xmax, ymin=opts.ymin, ymax=opts.ymax;
    if(ymin==null){ ymin=1e9;ymax=-1e9; for(const s of opts.series) for(const p of s.data){ if(p[1]==null)continue; if(p[1]<ymin)ymin=p[1]; if(p[1]>ymax)ymax=p[1]; } const pad=(ymax-ymin)*.1||1; ymin-=pad;ymax+=pad; }
    const X=t=>L+(t-xmin)/(xmax-xmin)*pw, Y=v=>T+(1-(v-ymin)/(ymax-ymin))*ph;
    const isTime=!opts.fmtX;                       // race-number axes: no gun/pre-start shading
    const gun=isTime&&S.race&&S.race.gun_t;
    const fx=opts.fmtX||(t=>fmtT(t-S.tMin));
    // gridlines + axis labels (drawn first, unclipped so labels sit in the margins)
    c.strokeStyle='#222a35'; c.fillStyle='#8b949e'; c.font='10px sans-serif'; c.lineWidth=1;
    for(let g=0;g<=4;g++){ const v=ymin+(ymax-ymin)*g/4, y=Y(v); c.beginPath();c.moveTo(L,y);c.lineTo(w-R,y);c.stroke(); c.fillText((opts.fmtY?opts.fmtY(v):v.toFixed(1)),2,y+3); }
    for(let g=0;g<=5;g++){ const t=xmin+(xmax-xmin)*g/5; c.fillText(fx(t),X(t)-12,h-6); }
    // all data drawing is clipped to the plot rect (so a zoomed leg window can't overflow)
    c.save(); c.beginPath(); c.rect(L,T,pw,ph); c.clip();
    if(gun && gun>xmin){ c.fillStyle='rgba(140,150,165,.10)'; c.fillRect(X(xmin),T,X(Math.min(gun,xmax))-X(xmin),ph); }
    const SH={upwind:'rgba(74,163,255,.09)',reach:'rgba(110,220,140,.10)',downwind:'rgba(255,123,84,.09)'};
    if(opts.legs) for(const lg of opts.legs){ c.fillStyle=SH[lg.kind]||'rgba(0,0,0,0)'; c.fillRect(X(lg.a),T,X(lg.b)-X(lg.a),ph); }
    if(gun && gun>xmin && gun<xmax){ c.strokeStyle='rgba(245,166,35,.55)'; c.setLineDash([3,3]);
      c.beginPath(); c.moveTo(X(gun),T); c.lineTo(X(gun),T+ph); c.stroke(); c.setLineDash([]); }
    if(ymin<0&&ymax>0){ c.strokeStyle='#3a4452'; c.beginPath();c.moveTo(L,Y(0));c.lineTo(w-R,Y(0));c.stroke(); }
    if(opts.band){ c.fillStyle=opts.band.color||'rgba(120,135,155,.18)'; c.beginPath();
      const lo=opts.band.lo,hi=opts.band.hi; let started=false;
      for(let k=0;k<lo.length;k++){ if(hi[k][1]==null)continue; const x=X(hi[k][0]),y=Y(hi[k][1]); started?c.lineTo(x,y):c.moveTo(x,y); started=true; }
      for(let k=lo.length-1;k>=0;k--){ if(lo[k][1]==null)continue; c.lineTo(X(lo[k][0]),Y(lo[k][1])); }
      c.closePath(); c.fill();
    }
    for(const s of opts.series){ c.strokeStyle=s.color; c.lineWidth=s.w||1.6; c.beginPath(); let st=false;
      for(const p of s.data){ if(p[1]==null){st=false;continue;} const x=X(p[0]),y=Y(p[1]); st?c.lineTo(x,y):c.moveTo(x,y); st=true; } c.stroke(); }
    if(opts.marks) for(const m of opts.marks){ c.fillStyle=m.color; c.beginPath(); c.arc(X(m.t),Y(m.y),3,0,7); c.fill(); }
    c.strokeStyle='rgba(245,166,35,.9)'; c.lineWidth=1; c.beginPath(); c.moveTo(X(S.t),T); c.lineTo(X(S.t),T+ph); c.stroke();  // cursor
    if(opts.cursorLabels && S.t>=xmin && S.t<=xmax){          // live values at the cursor
      c.font='bold 11px sans-serif';
      for(const L of opts.cursorLabels()){ if(!L||L.y==null) continue;
        c.fillStyle='#0d1117'; const tw=c.measureText(L.text).width;
        c.fillRect(X(S.t)+4,Y(L.y)-14,tw+6,14);
        c.fillStyle=L.color; c.fillText(L.text,X(S.t)+7,Y(L.y)-3); } }
    c.restore();
    chart._X=X; chart._Y=Y;
  }
  const chart={draw, cursor:draw, el:cv};
  return chart;
}

// ---------- tabs ----------
const LEG_TABS=['speed','vmg','wind','maneuvers','position'];   // tabs that support a leg filter
function setTab(name){
  S.tab=name; S.charts=[];
  document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.dataset.tab===name));
  buildLegBar();
  const body=$('#tabbody'); body.innerHTML='';
  if(!S.race){ body.innerHTML=`<div class="muted" style="padding:20px">No analysed races in this regatta yet —
    open <b>Regattas</b> (top bar) to add a race from a folder of KML tracks.</div>`; return; }
  ({overview:tabOverview,speed:tabSpeed,vmg:tabVMG,wind:tabWind,maneuvers:tabManeuvers,start:tabStart,position:tabPosition,regatta:tabRegatta,debrief:tabDebrief}[name])(body);
  S.charts.forEach(c=>c.draw());
}
function focusB(){ return S.byS[S.focus]; }
function partnerB(){ return S.partner?S.byS[S.partner]:null; }
function legSpans(b){ return boatLegs(b).map(([a,c,k])=>({a:b.t[a],b:b.t[c],kind:k})); }

// ---------- leg filter (drill charts/data into one leg) ----------
function legWindow(){          // [xmin,xmax] for the current leg filter
  if(S.legFilter==='all'||!focusB()) return [S.tMin,S.tMax];
  const lg=boatLegs(focusB())[S.legFilter]; if(!lg) return [S.tMin,S.tMax];
  const f=focusB(), pad=(f.t[lg[1]]-f.t[lg[0]])*0.03;
  return [f.t[lg[0]]-pad, f.t[lg[1]]+pad];
}
function buildLegBar(){
  const bar=$('#legbar');
  if(!S.race || !LEG_TABS.includes(S.tab)){ bar.style.display='none'; return; }
  bar.style.display='flex';
  const COL={upwind:'#4aa3ff',reach:'#6edc8c',downwind:'#ff7b54'};
  let h='<span>Leg:</span>'+`<span class="lp ${S.legFilter==='all'?'on':''}" data-leg="all">All</span>`;
  boatLegs(focusB()).forEach((lg,i)=>{ h+=`<span class="lp ${S.legFilter===i?'on':''}" data-leg="${i}"
    style="border-left:3px solid ${COL[lg[2]]}" title="${lg[2]}">${legNo(focusB(),i)}</span>`; });
  bar.innerHTML=h;
  bar.querySelectorAll('.lp').forEach(p=>p.onclick=()=>{
    S.legFilter = p.dataset.leg==='all' ? 'all' : +p.dataset.leg; setTab(S.tab); });
}

// ---------- official results (downloaded per regatta; the reference for finish places) ----------
// score {pts, code, disc} of a boat (by sail) in a race (by id); low-point: pts = place
function officialRow(sail){ const R=S.results; if(!R||sail==null) return null;
  if(R._ix!==S.index) linkResults();
  return R._link.get(String(sail))||R._bySail.get(String(sail))||R._bySail.get(sailDigits(sail))||null; }
// Tracked boats -> official rows, built once per results/index: by sail number, else by name when
// the numbers differ (a typo on either side): each word of the tracker's name starts a different
// word of the row's name (tracker names are cut short); the row must be the only one in the whole
// table that fits, not yet claimed, and fit by no other unmatched boat. A one-word name must be a
// whole word (a cut single word can't tell sailors apart), and only a name that really is one word
// ("LI MING", "GARCIA G" shrink to one word that could be anyone's first name).
function nameWords(s,all){ return String(s||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toUpperCase()
  .split(/[^A-Z]+/).filter(w=>w.length>=(all?1:3)); }
function linkResults(){ const R=S.results, bySail=new Map(), link=new Map(), byName=new Map(), used=new Set();
  for(const r of R.rows) if(!bySail.has(r.sail)) bySail.set(r.sail,r);
  const tracked=new Map(); for(const x of S.index||[]) for(const [sail,m] of Object.entries(x.boats||{})) if(!tracked.get(sail)) tracked.set(sail,(m&&m.name)||'');
  const left=[];
  for(const [sail,name] of tracked){ const r=bySail.get(sail)||bySail.get(sailDigits(sail));
    if(r){ link.set(sail,r); used.add(r); } else left.push([sail,nameWords(name),nameWords(name,true).length]); }
  const fits=(tk,ws)=>{ if(tk.length<2) return tk.length===1&&ws.includes(tk[0]);
    const free=ws.slice();                           // longest words first, whole words before prefixes
    return [...tk].sort((a,b)=>b.length-a.length).every(t=>{ let i=free.indexOf(t); if(i<0) i=free.findIndex(w=>w.startsWith(t));
      return i>=0&&free.splice(i,1).length; }); };
  const words=new Map(R.rows.map(r=>[r,nameWords(r.name)]));
  const cands=left.map(([sail,tk,n])=>[sail, tk.length>1||n===1?R.rows.filter(r=>fits(tk,words.get(r))):[]]);
  for(const [sail,c] of cands) if(c.length===1&&!used.has(c[0])&&cands.filter(([,c2])=>c2.includes(c[0])).length===1){ link.set(sail,c[0]); byName.set(c[0],sail); }
  R._bySail=bySail; R._link=link; R._byName=byName; R._ix=S.index; }
function officialFor(sail, raceId){ const R=S.results, row=officialRow(sail);
  const col=!row||raceId==null?-1:R.map.indexOf(raceId); return row&&col>=0?row.scores[col]:null; }
function sailDigits(s){ return (String(s).match(/\d+/g)||['']).reduce((a,b)=>b.length>a.length?b:a,''); }
function curRaceId(){ return S.index&&S.index[S.raceIdx]?S.index[S.raceIdx].id:null; }
function officialScore(b){ return b?officialFor(b.sail, curRaceId()):null; }
// codes for boats that did not finish / were removed; other codes (ZFP, SCP, DPI, RDG...) finished
const NO_FINISH=new Set(['DNC','DNS','OCS','UFD','BFD','DNF','RET','DSQ','DNE','DGM','NSC','RAF','TLE']);
const noFinish=s=>!!(s&&s.code&&NO_FINISH.has(s.code));
function officialPlace(b){ const s=officialScore(b); return s&&!s.code&&s.pts!=null?Math.round(s.pts):null; }
function officialPlaceBySail(sail, raceId){ const s=officialFor(sail, raceId); return s&&!s.code&&s.pts!=null?Math.round(s.pts):null; }
function scoreText(s){ return s?(s.code?s.code:'P'+Math.round(s.pts)):'–'; }

function finishers(){ const f=S.race.boats.filter(b=>b.summary.finished); return f.length?f:S.race.boats; }
function fleetMedians(){
  const keys=['sog_mean','sog_up','sog_dn','twa_up','twa_dn','vmg_up','vmg_dn','n_tacks','n_gybes','tack_loss','gybe_loss','dist_nm'];
  const fl=finishers(), o={}; for(const k of keys) o[k]=med(fl.map(b=>b.summary[k]));   // finishers only
  return o;
}

// dir: +1 higher=better, -1 lower=better, 0 neutral (no colour). pname = partner label.
function card(parent,k,v,unit,fleetV,partnerV,dir,pname){
  dir = dir==null?0:dir;
  const line=(d,who)=>{ if(d==null||!isFinite(d)) return '';
    const cls = dir===0?'':((dir>0?d>=0:d<=0)?'pos':'neg');
    return `<div class="d ${cls}">${d>=0?'▲':'▼'} ${Math.abs(d).toFixed(2)} vs ${who}</div>`; };
  let dh='';
  if(v!=null&&fleetV!=null) dh+=line(v-fleetV,'fleet');
  if(v!=null&&partnerV!=null) dh+=line(v-partnerV,pname);
  const d=document.createElement('div'); d.className='card';
  d.innerHTML=`<div class="k">${k}</div><div class="v">${v==null?'–':v}${unit?`<span style="font-size:13px;color:var(--mut)"> ${unit}</span>`:''}</div>${dh}`;
  parent.appendChild(d);
}

const mean = a => { a=a.filter(v=>v!=null); return a.length? a.reduce((x,y)=>x+y,0)/a.length : null; };
function startDistSpeed(boat){
  const r=S.race, rc=markBy('RC')[0], pin=markBy('Pin')[0]; if(!rc||!pin) return null;
  const wf=r.wind_from*Math.PI/180, ux=Math.sin(wf), uy=Math.cos(wf);
  const [ax,ay]=toXY(pin[0],pin[1]); const lp=ax*ux+ay*uy;
  const s=sampleAt(boat,(r.gun_t||r.start_t)+1); if(!s.inrace) return null;
  const [x,y]=toXY(s.lat,s.lon); return {behind:(x*ux+y*uy)-lp, sog:s.sog};
}
function keyFindings(body){
  const f=focusB(), s=f.summary, fm=fleetMedians(), p=partnerB(), r=S.race;
  const boats=finishers().filter(o=>o.summary.race_min!=null);   // exclude retirees/DNF
  const nStarted=r.boats.length, nRet=nStarted-r.boats.filter(o=>o.summary.finished).length;
  const n=boats.length, rt=s.race_min, medRT=med(boats.map(o=>o.summary.race_min));
  const rank=1+boats.filter(o=>o.summary.race_min<rt).length;     // finish by elapsed gun->finish time
  const B=[];   // bullets
  const close=boats.filter(o=>o.sail!==S.focus && Math.abs(o.summary.race_min-rt)*60<=20).length;
  const osc=officialScore(f);
  if(osc) B.push(`Official result: <b>${scoreText(osc)}</b>${osc.disc?' (discarded)':''}`+
    (rt!=null&&medRT!=null?` — GPS finish ~P${rank}/${n} by elapsed time (${rt} min, fleet median ${medRT.toFixed(1)}).`:'.'));
  else if(rt!=null&&medRT!=null) B.push(`Finished ~<b>P${rank}/${n}</b>${nRet?` (of ${nStarted} started; ${nRet} retired)`:''} by elapsed time (${rt} min, fleet median ${medRT.toFixed(1)}; ${fmtS(rt-medRT,1)} min)`+
    (close>0?` — but ${close} boat${close>1?'s are':' is'} within GPS finish error (~20 s), so the exact place is uncertain.`:'.'));
  // per-leg time vs fleet median, leg by leg of the course
  const legT=(o,ci)=>{ const li=ciMap(o)[ci]; if(li==null) return null; const lg=boatLegs(o)[li]; return o.t[lg[1]]-o.t[lg[0]]; };
  const fCis=boatLegs(f).map((_,li)=>courseIdx(f,li)).filter(ci=>ci>=0), fast=[], slow=[];
  for(const ci of fCis){ const mt=med(boats.map(o=>legT(o,ci)).filter(x=>x!=null)), ft=legT(f,ci);
    if(ft!=null&&mt!=null){ const d=ft-mt; if(d<-6)fast.push(ci+1); else if(d>6)slow.push(ci+1); } }
  if(fast.length) B.push(`Gained on the fleet on leg${fast.length>1?'s':''} <b class="good">${fast.join(', ')}</b>; lost on leg${slow.length>1?'s':''} <b class="bad">${slow.join(', ')||'none'}</b>.`);
  // speed/pointing/distance vs fleet
  const sp=[]; if(s.sog_up!=null)sp.push(`upwind ${fmtS(s.sog_up-fm.sog_up)} kt`); if(s.sog_dn!=null)sp.push(`downwind ${fmtS(s.sog_dn-fm.sog_dn)} kt`);
  B.push(`Speed vs fleet: ${sp.join(', ')}. Pointing ${fmtS(s.twa_up-fm.twa_up,0)}° (− = higher).`);
  B.push(`Sailed <b>${s.dist_nm} NM</b> (${fmtS(s.dist_nm-fm.dist_nm)} vs fleet — ${s.dist_nm<=fm.dist_nm?'less = tighter':'more = extra distance'}). ${s.n_tacks} tacks, loss ${s.tack_loss??'–'} BL (${s.tack_loss!=null?fmtS(s.tack_loss-fm.tack_loss):'–'}).`);
  const sd=startDistSpeed(f), flS=mean(boats.map(o=>{const m=startDistSpeed(o);return m?m.sog:null;}));
  if(sd) B.push(`Start: ${Math.abs(Math.min(0,sd.behind)).toFixed(0)} m behind line at gun, ${sd.sog.toFixed(1)} kt (${flS!=null?fmtS(sd.sog-flS,1):'–'} vs fleet).`);
  const lp=liftedPct(f), flp=mean(boats.map(liftedPct));
  if(lp!=null&&flp!=null) B.push(`On the lifted tack ${lp.toFixed(0)}% of upwind (${fmtS(lp-flp,0)} pts vs fleet).`);
  // headline: finish tier + dominant weakness
  const spd=mean([s.sog_up-fm.sog_up, s.sog_dn-fm.sog_dn]);
  const lp0=liftedPct(f), flp0=mean(boats.map(liftedPct)), sd0=startDistSpeed(f), flS0=mean(boats.map(o=>{const m=startDistSpeed(o);return m?m.sog:null;}));
  const weak=[];
  if(sd0&&flS0!=null&&sd0.sog-flS0<-0.4) weak.push('a slow start');
  if(lp0!=null&&flp0!=null&&lp0-flp0<-8) weak.push('the wrong shifts/side');
  if(spd<-0.05) weak.push('boatspeed');
  if(s.dist_nm-fm.dist_nm>0.1) weak.push('extra distance');
  let head;
  const op=officialPlace(f), offN=S.results?S.results.rows.filter(r=>{ const sc=officialFor(r.sail,curRaceId()); return sc&&!noFinish(sc); }).length:0;
  const hr=op??rank, hn=op!=null?offN:n;
  if(noFinish(osc)) head = `Scored ${osc.code} in the official results`+(weak.length?` — GPS also points to ${weak[0]}.`:'.');
  else if(hr<=hn/3) head = spd>=0.03?'Strong — fast and well sailed.':'Good result from tactics/shifts/start, not raw speed.';
  else if(hr>=2*hn/3) head = 'Tough race, near the back'+(weak.length?` — cost mainly by ${weak[0]}.`:'.');
  else head = spd>0.05?'Mid-fleet; boatspeed ok, places lost on tactics/start.':'Mid-fleet; small margins.';
  // partner line
  let pb='';
  if(p){ const ps=p.summary; const dt=(rt!=null&&ps.race_min!=null)?(rt-ps.race_min)*60:null;
    const pf=[]; for(const ci of fCis){ const a=legT(f,ci), b2=legT(p,ci); if(a!=null&&b2!=null&&a-b2<-4) pf.push(ci+1); }
    const finishTxt = dt==null ? '' :
      (Math.abs(dt)<=20 ? `<b>near-dead-heat</b> (~${Math.abs(dt).toFixed(0)} s — within GPS finish error, true order from official results), `
                        : `crossed ~<b>${Math.abs(dt).toFixed(0)} s ${dt<=0?'ahead':'behind'}</b>, `);
    pb=`<div class="muted" style="margin-top:6px"><b style="color:var(--partner)">vs ${p.name}:</b> `+ finishTxt +
      `sailed ${fmtS(s.dist_nm-ps.dist_nm)} NM (${s.dist_nm<=ps.dist_nm?'less':'more'}), faster on leg${pf.length>1?'s':''} ${pf.join(', ')||'—'}. `+
      `Speed up ${fmtS((s.sog_up||0)-(ps.sog_up||0))}, dn ${fmtS((s.sog_dn||0)-(ps.sog_dn||0))} kt.</div>`;
  }
  body.insertAdjacentHTML('beforeend',`<div class="sectitle">Key findings — ${f.name}</div>
    <div class="muted" style="font-size:13px;line-height:1.6"><b style="color:var(--fg)">${head}</b><ul style="margin:6px 0 0;padding-left:18px">`+
    B.map(x=>`<li>${x}</li>`).join('')+`</ul></div>${pb}
    <div class="muted" style="margin-top:6px;font-size:11px">Finish = GPS estimate by elapsed gun→finish time (±~20 s — the finish line isn't in the data, so close places may differ from official results). Per-leg "gained/lost" = leg time vs fleet median.</div>`);
}
function fmtS(d,dp=2){ return d==null||!isFinite(d)?'–':(d>=0?'+':'')+d.toFixed(dp); }
function tabOverview(body){
  const f=focusB(), p=partnerB(), s=f.summary, fm=fleetMedians();
  keyFindings(body);
  const ps=p?p.summary:null, pn=p?p.name.slice(0,9):null, pv=k=>ps?ps[k]:null;
  const grid=document.createElement('div'); grid.className='cards'; body.appendChild(grid);
  card(grid,'Avg speed',s.sog_mean,'kt',fm.sog_mean,pv('sog_mean'),1,pn);
  card(grid,'Upwind speed',s.sog_up,'kt',fm.sog_up,pv('sog_up'),1,pn);
  card(grid,'Downwind speed',s.sog_dn,'kt',fm.sog_dn,pv('sog_dn'),1,pn);
  card(grid,'Upwind VMG',s.vmg_up,'kt',fm.vmg_up,pv('vmg_up'),1,pn);
  card(grid,'Downwind VMG',s.vmg_dn,'kt',fm.vmg_dn,pv('vmg_dn'),1,pn);
  card(grid,'Upwind angle',s.twa_up,'°',fm.twa_up,pv('twa_up'),-1,pn); // lower TWA = points higher = good
  card(grid,'Tacks',s.n_tacks,'',fm.n_tacks,pv('n_tacks'),0,pn);
  card(grid,'Avg tack loss',s.tack_loss,'BL',fm.tack_loss,pv('tack_loss'),-1,pn);
  card(grid,'Gybes',s.n_gybes,'',fm.n_gybes,pv('n_gybes'),0,pn);
  card(grid,'Avg gybe loss',s.gybe_loss,'BL',fm.gybe_loss,pv('gybe_loss'),-1,pn);
  card(grid,'Distance',s.dist_nm,'NM',fm.dist_nm,pv('dist_nm'),-1,pn);

  body.insertAdjacentHTML('beforeend',`<div class="sectitle">How to read this</div>
   <div class="muted">Deltas compare <b style="color:var(--focus)">${f.name}</b> to the <b>fleet median</b> for this race.
   Green = better (faster, higher VMG, lower tack loss, higher pointing). Wind is <b>estimated from the GPS tracks</b>
   (bisector of the fleet's upwind tacks), so absolute angles are approximate but apply equally to every boat, making
   the comparisons valid. There is no heel/trim data — TracTrac trackers are GPS only.</div>`);

  if(p){
    const ps=p.summary;
    body.insertAdjacentHTML('beforeend',`<div class="sectitle">Head-to-head vs ${p.name}</div>`);
    const rows=[['Upwind speed','sog_up','kt',1],['Downwind speed','sog_dn','kt',1],
      ['Upwind VMG','vmg_up','kt',1],['Downwind VMG','vmg_dn','kt',1],
      ['Upwind angle','twa_up','°',-1],['Avg tack loss','tack_loss','BL',-1],
      ['Distance sailed','dist_nm','NM',-1]];   // less distance = better
    let t='<table><tr><th>Metric</th><th style="color:var(--focus)">'+f.name.slice(0,9)+'</th>'+
      '<th style="color:var(--partner)">'+p.name.slice(0,9)+'</th><th>Fleet avg</th><th>Δ</th></tr>';
    for(const [lbl,key,u,dir] of rows){ const a=s[key],b=ps[key],fl=fm[key]; const d=(a!=null&&b!=null)?a-b:null;
      const good=d!=null && (dir>0?d>=0:d<=0);
      t+=`<tr><td>${lbl}</td><td>${a??'–'} ${u}</td><td>${b??'–'} ${u}</td><td>${fl!=null?fl.toFixed(2):'–'} ${u}</td>`+
         `<td><b class="${good?'good':'bad'}">${d==null?'–':(d>=0?'+':'')+d.toFixed(2)}</b></td></tr>`; }
    t+='</table>'; body.insertAdjacentHTML('beforeend',t);
    body.insertAdjacentHTML('beforeend',`<div class="muted">Δ = ${f.name.slice(0,9)} − ${p.name.slice(0,9)} (green = ${f.name.slice(0,9)} better).
     Distance sailed: less = more efficient. Near-equal speed but less distance can win the race.</div>`);
  }
}

function seriesFromBoat(b, key){ return b.t.map((t,k)=>[t, b[key][k]]); }

function tabSpeed(body){
  const f=focusB(), p=partnerB(), fs=S.race.fleet_stats, [xa,xb]=legWindow();
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Speed over ground — you vs fleet vs partner</div>');
  const series=[{color:'rgba(139,148,158,.8)',w:1.3,data:fs.t.map((t,k)=>[t,fs.sog_p50[k]])},
                {color:'#f5a623',w:2,data:seriesFromBoat(f,'sog')}];
  if(p) series.push({color:'#3aa0ff',w:1.7,data:seriesFromBoat(p,'sog')});
  const ch=makeChart(body,{h:200,xmin:xa,xmax:xb,series,
    band:{lo:fs.t.map((t,k)=>[t,fs.sog_p25[k]]),hi:fs.t.map((t,k)=>[t,fs.sog_p75[k]])},
    legs:legSpans(f),fmtY:v=>v.toFixed(1),
    cursorLabels:()=>{ const a=liveRank(f), b2=p?liveRank(p):null;
      return [a&&{y:sampleAt(f,S.t).sog,text:'P'+a.rank,color:'#f5a623'},
              b2&&{y:sampleAt(p,S.t).sog,text:'P'+b2.rank,color:'#3aa0ff'}]; }});
  S.charts.push(ch);
  body.insertAdjacentHTML('beforeend',`<div class="muted" style="margin-top:8px">
    <span class="dot" style="background:#f5a623"></span> you &nbsp;
    ${p?'<span class="dot" style="background:#3aa0ff"></span> '+p.name+' &nbsp;':''}
    <span class="dot" style="background:#8b949e"></span> fleet median &nbsp;
    shaded band = fleet 25–75th percentile. Background = leg type: <b style="color:#6ea3ff">upwind</b> / <b style="color:#6edc8c">reach</b> / <b style="color:#ff7b54">downwind</b>.</div>`);
  // per-leg breakdown (click a row to drill into that leg)
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Per-leg average speed (click a leg to drill in)</div>');
  let t='<table><tr><th>Leg</th><th>Type</th><th>Avg SOG</th><th>Avg VMG</th></tr>';
  boatLegs(f).forEach(([a,c,k],i)=>{ let ss=0,vv=0,n=0; for(let j=a;j<=c;j++){ss+=f.sog[j];vv+=f.vmg[j];n++;}
    const sel=S.legFilter===i?' style="background:var(--panel2)"':'';
    t+=`<tr class="man" data-leg="${i}"${sel}><td>${legNo(f,i)}</td><td>${k}</td><td>${(ss/n).toFixed(2)} kt</td><td>${(Math.abs(vv/n)).toFixed(2)} kt</td></tr>`; });
  t+='</table>'; body.insertAdjacentHTML('beforeend',t);
  body.querySelectorAll('tr[data-leg]').forEach(tr=>tr.onclick=()=>{
    const i=+tr.dataset.leg; S.legFilter = (S.legFilter===i?'all':i); setTab('speed'); });
}

function tabVMG(body){
  const f=focusB(), p=partnerB(), fs=S.race.fleet_stats, [xa,xb]=legWindow();
  body.insertAdjacentHTML('beforeend','<div class="sectitle">VMG to windward (+ up / − down)</div>');
  const series=[{color:'rgba(139,148,158,.8)',w:1.3,data:fs.t.map((t,k)=>[t,fs.vmg_p50[k]])},
                {color:'#f5a623',w:2,data:seriesFromBoat(f,'vmg')}];
  if(p) series.push({color:'#3aa0ff',w:1.7,data:seriesFromBoat(p,'vmg')});
  S.charts.push(makeChart(body,{h:200,xmin:xa,xmax:xb,series,legs:legSpans(f),fmtY:v=>v.toFixed(1),
    cursorLabels:()=>{ const a=liveRank(f), b2=p?liveRank(p):null;
      return [a&&{y:sampleAt(f,S.t).vmg,text:'P'+a.rank,color:'#f5a623'},
              b2&&{y:sampleAt(p,S.t).vmg,text:'P'+b2.rank,color:'#3aa0ff'}]; }}));
  body.insertAdjacentHTML('beforeend','<div class="sectitle">TWA (angle to the wind) over time</div>');
  S.charts.push(makeChart(body,{h:160,xmin:xa,xmax:xb,ymin:0,ymax:180,
    series:[{color:'#f5a623',w:1.6,data:seriesFromBoat(f,'twa')}].concat(p?[{color:'#3aa0ff',w:1.4,data:seriesFromBoat(p,'twa')}]:[]),
    legs:legSpans(f),fmtY:v=>v.toFixed(0)+'°'}));
  body.insertAdjacentHTML('beforeend',`<div class="muted" style="margin-top:8px">VMG = the speed you actually make
   toward (or away from) the wind. Upwind you want high positive VMG; downwind, high magnitude negative VMG. TWA near
   ~40–45° is close-hauled; ~150° is a deep run. Flat TWA = consistent; spikes = maneuvers.</div>`);
}

function upwindLegsToScan(boat){
  const legs=boatLegs(boat);
  if(typeof S.legFilter==='number'){ const lg=legs[S.legFilter]; return (lg&&lg[2]==='upwind')?[lg]:[]; }
  return legs.filter(l=>l[2]==='upwind');
}
function liftedPct(boat){          // % of upwind time on the lifted tack (vs race-mean wind)
  const ws=S.race.wind_series, avg=med(ws.dir.filter(v=>v!=null));
  const winAt=t=>{ const a=ws.t; let i=0; while(i<a.length-1&&a[i]<t)i++; return ws.dir[i]; };
  let lift=0,head=0;
  for(const lg of upwindLegsToScan(boat)){ const a=lg[0],c=lg[1];
    for(let j=a;j<c;j++){ const w=winAt(boat.t[j]); if(w==null)continue;
      const shift=((w-avg+540)%360)-180;
      // tack>0 = heading right of wind; a right shift (veer, shift>0) points it
      // closer to the mark = lifted. (left/back shift lifts the other tack.)
      const lifted = boat.tack[j]>0 ? shift>0 : shift<0;
      const dt=boat.t[j+1]-boat.t[j]; lifted?lift+=dt:head+=dt; } }
  const tot=lift+head; return tot? lift/tot*100 : null;
}
function tabWind(body){
  const f=focusB(), p=partnerB(), ws=S.race.wind_series, [xa,xb]=legWindow();
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Estimated wind direction (shift track)</div>');
  const avg=med(ws.dir.filter(v=>v!=null));
  S.charts.push(makeChart(body,{h:180,xmin:xa,xmax:xb,
    series:[{color:'#58a6ff',w:2,data:ws.t.map((t,k)=>[t,ws.dir[k]])},
            {color:'rgba(139,148,158,.5)',w:1,data:[[xa,avg],[xb,avg]]}],
    legs:legSpans(f),fmtY:v=>v.toFixed(0)+'°'}));
  body.insertAdjacentHTML('beforeend',`<div class="muted" style="margin-top:8px">
   Wind direction is reconstructed from the fleet's upwind tacking angles in a moving window — no anemometer needed.
   Rising line = wind veering (clockwise); falling = backing. Dashed grey = race average (${avg?avg.toFixed(0):'–'}°).
   When the trace moves <b>toward</b> your current heading you've been <b>lifted</b>; away = <b>headed</b>.</div>`);

  // lifted/headed scoring on upwind legs, with a comparison
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Were you on the lifted tack? (upwind)</div>');
  if(!S.liftCmp) S.liftCmp='fleet';
  const cmpBar=document.createElement('div'); cmpBar.className='filterbar';
  cmpBar.innerHTML=`Compare with: <span class="pill ${S.liftCmp==='fleet'?'on':''}" data-c="fleet">fleet average</span>`+
    (p?`<span class="pill ${S.liftCmp==='partner'?'on':''}" data-c="partner">${p.name}</span>`:'');
  body.appendChild(cmpBar);
  cmpBar.querySelectorAll('[data-c]').forEach(el=>el.onclick=()=>{ S.liftCmp=el.dataset.c; setTab('wind'); });

  const flp=liftedPct(f);
  let cmpLp, cmpName;
  if(S.liftCmp==='partner' && p){ cmpLp=liftedPct(p); cmpName=p.name; }
  else { const all=finishers().map(liftedPct).filter(v=>v!=null); cmpLp=all.length?all.reduce((a,b)=>a+b,0)/all.length:null; cmpName='Fleet average'; }
  const bar=(name,v,col)=>v==null?`<div class="muted">${name}: no upwind data for this selection.</div>`:
    `<div class="barwrap"><span class="lbl" style="width:110px">${name}</span>
      <div class="bar" style="width:${Math.round(v)}%;background:${col}"></div><span>${Math.round(v)}%</span></div>`;
  body.insertAdjacentHTML('beforeend', bar('You (lifted)',flp,'var(--good)') + bar(cmpName,cmpLp,'#3aa0ff'));
  const dlt=(flp!=null&&cmpLp!=null)?(flp-cmpLp):null;
  body.insertAdjacentHTML('beforeend',`<div class="muted">% of upwind time on the favoured (lifted) tack relative to the
    race-mean wind. ${dlt!=null?`You were <b class="${dlt>=0?'good':'bad'}">${dlt>=0?'+':''}${dlt.toFixed(0)} pts</b> vs ${cmpName.toLowerCase()}. `:''}
    Higher = you sailed the headers and tacked onto the lifts.${typeof S.legFilter==='number'?' (this upwind leg only)':''}</div>`);
}

// ---------- tack-decision analysis (focus, upwind) ----------
function _wrapd(d){ return ((d+540)%360)-180; }
function windAtT(t){ const ws=S.race.wind_series; let i=0; while(i<ws.t.length-1&&ws.t[i]<t)i++;
  return ws.dir[i]!=null?ws.dir[i]:S.race.wind_from; }
function meanWind(t0,t1){ const ws=S.race.wind_series; let sx=0,sy=0,n=0;
  for(let i=0;i<ws.t.length;i++){ if(ws.t[i]>=t0&&ws.t[i]<=t1&&ws.dir[i]!=null){ const r=ws.dir[i]*Math.PI/180; sx+=Math.cos(r);sy+=Math.sin(r);n++;} }
  return n?(Math.atan2(sy,sx)*180/Math.PI+360)%360:S.race.wind_from; }
// headed amount (deg, + = being headed on this tack) vs the leg-mean wind
function headedDeg(f,i,bw){ return (f.tack[i]>0?-1:1)*_wrapd(windAtT(f.t[i])-bw); }
function tackDecisions(f){
  const real=[], missed=[], THR=5, DUR=20;
  for(const [a,c,k] of boatLegs(f)){ if(k!=='upwind') continue;
    const bw=meanWind(f.t[a],f.t[c]), cutoff=f.t[a]+(f.t[c]-f.t[a])*0.8;   // skip last 20% = layline
    const tacks=(f.maneuvers||[]).filter(m=>m.kind==='tack'&&m.t>=f.t[a]&&m.t<=f.t[c]);
    for(const m of tacks){ // avg headed over a window before the tack (robust to noise)
      const lo=idxAt(f,m.t-18), hi=idxAt(f,m.t-5); let q=0,nq=0;
      for(let i=lo;i<=hi;i++){ q+=headedDeg(f,i,bw); nq++; } const h=nq?q/nq:0;
      let good=null, reason='small shift — likely a layline / clear-air / fleet call';
      if(h>3){good=true; reason=`tacked on a ${h.toFixed(0)}° header → onto the lift`;}
      else if(h<-3){good=false; reason=`tacked while lifted ${(-h).toFixed(0)}° → gave up a lift`;}
      real.push({t:m.t,lat:m.lat,lon:m.lon,good,reason}); }
    // missed: sustained header (>THR for >=DUR s) with no tack -> should have tacked
    const runs=[]; let rs=null;
    for(let i=a;i<=c;i++){ const bad=headedDeg(f,i,bw)>THR && f.t[i]<cutoff;
      if(bad){ if(rs==null)rs=i; } else { if(rs!=null){runs.push([rs,i-1]);rs=null;} } }
    if(rs!=null)runs.push([rs,c]);
    for(const [s,e] of runs){ if(f.t[e]-f.t[s]<DUR) continue;
      if(tacks.some(m=>m.t>=f.t[s]-5&&m.t<=f.t[e]+5)) continue; const mid=(s+e)>>1;
      missed.push({t:f.t[s],lat:f.lat[mid],lon:f.lon[mid],deg:headedDeg(f,mid,bw),dur:Math.round(f.t[e]-f.t[s])}); }
  }
  return {real,missed};
}
function drawTackCalls(boat,isPartner){
  if(!boat) return; const td=tackDecisions(boat);
  const R1=isPartner?4.5:6, R2=isPartner?6:8, LW=isPartner?2:3;   // partner: smaller rings
  if(isPartner) bgx.setLineDash([3*DPR,3*DPR]);                    // and dashed, to tell apart
  for(const m of td.missed){ const [x,y]=proj(m.lat,m.lon); bgx.strokeStyle='#ffb000'; bgx.lineWidth=2*DPR;
    bgx.beginPath(); bgx.arc(x,y,R2*DPR,0,7); bgx.stroke();
    bgx.fillStyle='#ffb000'; bgx.font=`bold ${12*DPR}px sans-serif`; bgx.fillText('!',x-2*DPR,y-(R2+2)*DPR); }
  for(const t of td.real){ const [x,y]=proj(t.lat,t.lon);
    bgx.strokeStyle=t.good===true?'#3fb950':t.good===false?'#f85149':'#8b949e';
    bgx.lineWidth=LW*DPR; bgx.beginPath(); bgx.arc(x,y,R1*DPR,0,7); bgx.stroke(); }
  bgx.setLineDash([]);
}
function tabManeuvers(body){
  const f=focusB();
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Tacks & gybes — maneuver loss</div>');
  const fb=document.createElement('div'); fb.className='filterbar';
  fb.innerHTML=`Show: <span class="pill on" data-f="all">All</span><span class="pill" data-f="tack">Tacks</span>
    <span class="pill" data-f="gybe">Gybes</span> &nbsp;|&nbsp;
    <span class="pill" data-q="all">Every</span><span class="pill" data-q="best">Best 50%</span><span class="pill" data-q="worst">Worst 50%</span>`;
  body.appendChild(fb);
  const tableWrap=document.createElement('div'); body.appendChild(tableWrap);
  let kindF='all', qF='all', sortKey='t', sortDir=1;
  function rows(){
    const [xa,xb]=legWindow();
    let m=f.maneuvers.filter(x=>x.t>=xa&&x.t<=xb);   // restrict to selected leg
    if(kindF!=='all') m=m.filter(x=>x.kind===kindF);
    if(qF!=='all'){ const sorted=m.slice().sort((a,b)=>a.bl_lost-b.bl_lost); const half=Math.ceil(sorted.length/2);
      const set=new Set((qF==='best'?sorted.slice(0,half):sorted.slice(-half))); m=m.filter(x=>set.has(x)); }
    m.sort((a,b)=>(a[sortKey]>b[sortKey]?1:-1)*sortDir);
    return m;
  }
  function draw(){
    const m=rows();
    const tk=m.filter(x=>x.kind==='tack'), gy=m.filter(x=>x.kind==='gybe');
    const avg=arr=>arr.length?(arr.reduce((s,x)=>s+x.bl_lost,0)/arr.length).toFixed(2):'–';
    let h=`<div class="muted" style="margin-bottom:8px">Avg loss — tacks <b>${avg(tk)} BL</b>, gybes <b>${avg(gy)} BL</b>
       &nbsp;(1 BL = ${ILCA} m). Click a row to jump the map & scrubber to that maneuver.</div>`;
    h+='<table><tr>'+[['t','Time'],['kind','Type'],['entry_sog','Entry'],['min_sog','Min'],['exit_sog','Exit'],['sog_loss','ΔSOG'],['duration','Dur'],['bl_lost','BL lost']]
        .map(([k,l])=>`<th data-k="${k}">${l}${sortKey===k?(sortDir>0?' ▲':' ▼'):''}</th>`).join('')+'</tr>';
    for(const x of m){ h+=`<tr class="man ${x.kind==='tack'?'tk':'gy'}" data-t="${x.t}">
      <td>${fmtT(x.t-S.tMin)}</td><td>${x.kind}</td><td>${x.entry_sog}</td><td>${x.min_sog}</td>
      <td>${x.exit_sog}</td><td>${x.sog_loss}</td><td>${x.duration}s</td>
      <td><b class="${x.bl_lost<=1.2?'good':x.bl_lost>=2.5?'bad':''}">${x.bl_lost}</b></td></tr>`; }
    h+='</table>'; tableWrap.innerHTML=h;
    tableWrap.querySelectorAll('th').forEach(th=>th.onclick=()=>{ const k=th.dataset.k; if(sortKey===k)sortDir*=-1; else{sortKey=k;sortDir=1;} draw(); });
    tableWrap.querySelectorAll('tr.man').forEach(tr=>tr.onclick=()=>{ S.t=+tr.dataset.t-8; zoomTo(+tr.dataset.t); syncTime(); });
  }
  fb.querySelectorAll('[data-f]').forEach(p=>p.onclick=()=>{ fb.querySelectorAll('[data-f]').forEach(z=>z.classList.toggle('on',z===p)); kindF=p.dataset.f; draw(); });
  fb.querySelectorAll('[data-q]').forEach(p=>p.onclick=()=>{ fb.querySelectorAll('[data-q]').forEach(z=>z.classList.toggle('on',z===p)); qF=p.dataset.q; draw(); });
  draw();

  // tack-decision review (upwind): graded real tacks + missed shifts
  const td=tackDecisions(f);
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Tack decisions — upwind (toggle “Tack calls” on the map)</div>');
  const ng=td.real.filter(x=>x.good===true).length, nb=td.real.filter(x=>x.good===false).length;
  const tdWrap=document.createElement('div'); body.appendChild(tdWrap);
  if(!td.real.length && !td.missed.length){ tdWrap.innerHTML='<div class="muted">No upwind tacks or clear missed shifts detected.</div>'; }
  else {
    const rows=[...td.real.map(x=>({t:x.t,type:'tack',good:x.good,txt:x.reason})),
                ...td.missed.map(x=>({t:x.t,type:'missed',good:false,txt:`headed ${x.deg.toFixed(0)}° for ${x.dur}s — should have tacked onto the lift`}))]
               .sort((a,b)=>a.t-b.t);
    let h=`<div class="muted" style="margin-bottom:8px"><b class="good">${ng} good</b> · <b class="bad">${nb} poor</b> tack${td.real.length!==1?'s':''}, <b style="color:#ffb000">${td.missed.length} missed</b> shift${td.missed.length!==1?'s':''}. Click a row to jump.</div>`;
    h+='<table><tr><th>Time</th><th>Call</th><th>Verdict</th></tr>';
    for(const r of rows){ const v=r.type==='missed'?'<b style="color:#ffb000">MISSED</b>':r.good===true?'<b class="good">GOOD</b>':r.good===false?'<b class="bad">POOR</b>':'<b style="color:#8b949e">—</b>';
      h+=`<tr class="man" data-tt="${r.t}"><td>${fmtT(r.t-S.tMin)}</td><td>${r.type==='missed'?'missed shift':'tack'}</td><td>${v} ${r.txt}</td></tr>`; }
    h+='</table>'; tdWrap.innerHTML=h;
    tdWrap.querySelectorAll('tr[data-tt]').forEach(tr=>tr.onclick=()=>{ S.t=+tr.dataset.tt;
      if(!S.showTacks){ S.showTacks=true; document.querySelector('#tacksBtn').classList.add('on'); renderBG(); } syncTime(); });
  }
  body.insertAdjacentHTML('beforeend','<div class="muted" style="font-size:11px;margin-top:6px">Verdict vs the leg-mean wind: GOOD = tacked onto a header (the lift); POOR = tacked while lifted (gave up gain); MISSED = headed >6° for >25 s with no tack. Heuristic from estimated wind — laylines/clear-air calls show as “—”.</div>');
}

function zoomTo(t){ /* keep current view; dots will show. Could implement zoom later. */ }

function tabStart(body){
  const f=focusB(), r=S.race, st=(r.gun_t||r.start_t);
  body.insertAdjacentHTML('beforeend',`<div class="sectitle">Start performance — gun ${fmtClock(st)} (${fmtT(st-S.tMin)} into the track)</div>`);
  const rc=markBy('RC')[0], pin=markBy('Pin')[0];
  const aEnd = pin || (r.start_line&&r.start_line[0]), bEnd = rc || (r.start_line&&r.start_line[1]);
  if(!aEnd||!bEnd){ body.insertAdjacentHTML('beforeend','<div class="muted">Start line could not be inferred for this race.</div>'); return; }
  // distance to line along wind axis at gun, speed at gun, build speed over first 90s
  const wf=r.wind_from*Math.PI/180, ux=Math.sin(wf), uy=Math.cos(wf);
  const cosL=Math.cos(r.lat0*Math.PI/180);
  const toXY=(la,lo)=>[(lo-r.lon0)*111320*cosL,(la-r.lat0)*111320];
  const [ax,ay]=toXY(aEnd[0],aEnd[1]); const lineperp=ax*ux+ay*uy;            // along-wind coord of line
  function startMetrics(boat){ const s=sampleAt(boat,st+1); const [x,y]=toXY(s.lat,s.lon);
    const d=(x*ux+y*uy)-lineperp;  // + = above line (over early), - = behind
    return {dist:d, sog:s.sog}; }
  const fm=startMetrics(f);
  const fl=finishers(), fleetS=fl.map(bb=>startMetrics(bb).sog);
  const myRank=1+fl.filter(bb=>startMetrics(bb).dist>fm.dist).length;   // nearer the line at gun = better
  const grid=document.createElement('div'); grid.className='cards'; body.appendChild(grid);
  card(grid,'Dist behind line @ gun',Math.abs(Math.min(0,fm.dist)).toFixed(0),'m');
  card(grid,'Speed @ gun',+fm.sog.toFixed(2),'kt',med(fleetS),null,1);
  card(grid,'Line position',myRank+' / '+fl.length,'');
  // speed: final approach (gun-30s) + first 2 min off the line; gun line is drawn by the chart
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Speed — approach &amp; build-up (dashed line = gun)</div>');
  const t0=Math.max(S.tMin,st-30),t1=st+120, fdata=[],pdata=[];
  const p=partnerB();
  for(let t=t0;t<=t1;t+=3){ fdata.push([t,sampleAt(f,t).sog]); if(p)pdata.push([t,sampleAt(p,t).sog]); }
  const med2=[]; for(let t=t0;t<=t1;t+=3){ med2.push([t,med(r.boats.map(bb=>sampleAt(bb,t).sog))]); }
  S.charts.push(makeChart(body,{h:170,xmin:t0,xmax:t1,
    series:[{color:'rgba(139,148,158,.8)',w:1.3,data:med2},{color:'#f5a623',w:2,data:fdata}].concat(p?[{color:'#3aa0ff',w:1.6,data:pdata}]:[]),
    fmtY:v=>v.toFixed(1)}));
  body.insertAdjacentHTML('beforeend',`<div class="muted" style="margin-top:8px">Gun detected at the surge of fleet
   upwind speed, snapped to the clock minute. The line runs between the <b style="color:#ffd166">RC</b> (committee, starboard)
   and <b style="color:#ffd166">Pin</b> (port) marks — drag them in <b>Edit marks</b> to correct. Distances are approximate;
   a good start = on the line at the gun (≈0 m behind) at full speed, accelerating ahead of the fleet-median curve.</div>`);
}

function tabPosition(body){
  const f=focusB(), p=partnerB(), r=S.race, fs=r.fleet_stats, G=fs.t;
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Fleet position over time (1 = leading)</div>');
  // rank each grid index by course progress, among FINISHERS (retirees excluded)
  const pool=finishers().slice(); for(const b of [f,p]) if(b&&!pool.includes(b)) pool.push(b);
  const C={}; for(const o of pool) C[o.sail]=boatCourse(o);
  const rankOf=(boat)=>{ const bc=C[boat.sail], out=[]; for(let k=0;k<G.length;k++){ const my=bc[k]; if(my==null){out.push([G[k],null]);continue;}
      let rank=1; for(const o of pool){ const v=C[o.sail][k]; if(v!=null && v>my) rank++; } out.push([G[k],rank]); } return out; };
  const series=[{color:'#f5a623',w:2,data:rankOf(f)}];
  if(p) series.push({color:'#3aa0ff',w:1.7,data:rankOf(p)});
  const [xa,xb]=legWindow();
  S.charts.push(makeChart(body,{h:220,xmin:xa,xmax:xb,ymin:pool.length+1,ymax:0,
    series,legs:legSpans(f),fmtY:v=>v.toFixed(0),
    cursorLabels:()=>{ const a=liveRank(f), b2=p?liveRank(p):null;
      return [a&&{y:a.rank,text:'P'+a.rank,color:'#f5a623'},
              b2&&{y:b2.rank,text:'P'+b2.rank,color:'#3aa0ff'}]; }}));
  body.insertAdjacentHTML('beforeend',`<div class="muted" style="margin-top:8px">Position is estimated from cumulative
   distance made good along the course (made-good distance to each mark). Line going <b>up</b> = gaining places, <b>down</b>
   = losing. This is a GPS proxy for race rank — penalties, OCS and protests aren't reflected.
   Background = <b style="color:#6ea3ff">upwind</b> / <b style="color:#6edc8c">reach</b> / <b style="color:#ff7b54">downwind</b> legs.</div>`);
  // position at each mark rounding: rank by arrival time at each leg end
  // (final leg ranked by race_min = finish-line crossing)
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Position at each mark (rounding order)</div>');
  const NL=Math.max(0,...pool.map(o=>1+Math.max(-1,...boatLegs(o).map((_,li)=>courseIdx(o,li)))));
  // rounding time: mark-proximity (server 'rounds', sharp) >> leg-boundary (smoothed)
  const rankAtLeg=(boat,ci)=>dbRankAtMark(boat,ci,pool);
  const lastCols={}; for(const o of pool) if(o.summary.finished){ const c=courseIdx(o,boatLegs(o).length-1); lastCols[c]=(lastCols[c]||0)+1; }
  const finCol=Object.keys(lastCols).length?+Object.entries(lastCols).sort((a,b)=>b[1]-a[1])[0][0]:-1;
  const colLabel=i=>legMarkLabel(i)+(i===finCol&&i<courseLegs().length-1?' (finish)':'');
  let mt=`<table><tr><th>Boat</th>${Array.from({length:NL},(_,i)=>`<th>${esc(colLabel(i))}</th>`).join('')}</tr>`;
  const rowFor=(boat,color)=>{ let prev=null, tds='';
    for(let i=0;i<NL;i++){ const sc=isLastFor(boat,i)?officialScore(boat):null;
      if(noFinish(sc)){ tds+=`<td><span class="bad" title="official result">${esc(sc.code)}</span></td>`; continue; }
      const fin=finishesAt(boat,i), rk2=fin?dbFinishRank(boat,pool):rankAtLeg(boat,i);   // finish: official order, else finish line
      const d=(rk2!=null&&prev!=null)?rk2-prev:null; if(rk2!=null)prev=rk2;
      const badge=d==null||d===0?'':` <span class="${d<0?'good':'bad'}" style="font-size:10px">${d<0?'+'+(-d):'−'+d}</span>`;
      const op=fin?officialPlace(boat):null, shown=op!=null?op:rk2;               // the official place itself
      // boats ahead not tracked (or not matched): the official place isn't on the mark ranks'
      // scale, so the rank among the tracked boats and its gain/loss go alongside
      const tip=op==null?'':op===rk2?' title="official result"':` title="official result; P${rk2} among the tracked boats"`;
      const extra=op==null||op===rk2?badge:` <span class="muted" style="font-size:10px">(P${rk2} tracked${badge})</span>`;
      tds+=`<td${tip}>${shown==null?'–':'P'+shown}${extra}</td>`; }
    return `<tr><td style="color:${color}">${boat.name.slice(0,12)}</td>${tds}</tr>`; };
  mt+=rowFor(f,'var(--focus)'); if(p) mt+=rowFor(p,'var(--partner)');
  mt+='</table>'; body.insertAdjacentHTML('beforeend',mt);
  body.insertAdjacentHTML('beforeend',`<div class="muted" style="margin-top:4px;font-size:11px">P = place at that mark
   (order the fleet rounded it); green/red = places gained/lost on the leg just sailed. Mark ranks come from rounding
   times (robust); the finish column is the official result when downloaded (Regattas…), else the GPS
   finish-line crossing. Retirees excluded.</div>`);

  // gains/losses summary
  const rk=rankOf(f).filter(x=>x[1]!=null);
  if(rk.length){ body.insertAdjacentHTML('beforeend',`<div class="sectitle">Summary</div>
    <div class="muted">First clear position: <b>${rk[0][1]}</b> · Best: <b class="good">${Math.min(...rk.map(x=>x[1]))}</b>
    · Worst: <b class="bad">${Math.max(...rk.map(x=>x[1]))}</b> · Final: <b>${rk[rk.length-1][1]}</b> of ${finishers().length} finishers.</div>`); }

  // closest to the ideal route = least time vs the fastest-demonstrated-per-leg total
  const it=idealTime(), nl=boatLegs(f).length;
  if(it>0){
    const eff=finishers().map(b=>({sail:b.sail,name:b.name,rt:raceTime(b),n:boatLegs(b).length}))
      .filter(x=>x.n>=nl && x.rt>0).map(x=>({...x,e:it/x.rt}));
    eff.sort((a,b)=>b.e-a.e);
    body.insertAdjacentHTML('beforeend','<div class="sectitle">Closest to the ideal route (toggle “Ideal route” on the map)</div>');
    const myI=eff.findIndex(x=>x.sail===S.focus);
    let h='<table><tr><th>#</th><th>Boat</th><th>Match</th><th>Lost vs ideal</th></tr>';
    eff.slice(0,5).forEach((x,i)=>{ h+=row_eff(i+1,x,it); });
    if(myI>=5) h+=`<tr><td colspan=4 style="text-align:center;color:var(--mut)">…</td></tr>`+row_eff(myI+1,eff[myI],it);
    h+='</table>'; body.insertAdjacentHTML('beforeend',h);
    body.insertAdjacentHTML('beforeend',`<div class="muted">Ideal = fastest <b>actually-sailed</b> track on each leg, stitched
     (real fleet paths/speeds), total ${fmtT(it)}. Match = ideal_time / your_time; higher = closer to sailing every leg
     like the fleet's best. No single boat hits 100% (nobody is fastest on every leg). Completed-course boats only.</div>`);
  }
}
function row_eff(rank,x,it){ const lost=x.rt-it; const me=x.sail===S.focus;
  return `<tr${me?' style="background:var(--panel2)"':''}><td>${rank}</td><td>${me?'<b>'+x.name+'</b>':x.name}</td>`+
    `<td><b class="${x.e>=0.95?'good':x.e<0.88?'bad':''}">${(x.e*100).toFixed(0)}%</b></td><td>+${fmtT(lost)}</td></tr>`; }

// ---------- Regatta tab: trends across all races ----------
function tabRegatta(body){
  const idx=S.index||[], p=partnerB(), pn=p?p.name.slice(0,10):null;
  const withOff=(m,sail,r)=>{ const s=officialFor(sail,r.id); if(!m||!s) return m;
    const scored=S.results.rows.filter(x=>{ const v=officialFor(x.sail,r.id); return v&&!noFinish(v); }).length;
    return {...m, rank:officialPlaceBySail(sail,r.id)??(s.code?null:m.rank), n_fin:scored, code:s.code, pts:s.pts}; };
  const rows=idx.map(r=>({x:r.i+1, race:r.race, fl:r.fleet||{}, me:withOff((r.boats||{})[S.focus]||null,S.focus,r),
                          pr:p?withOff((r.boats||{})[S.partner]||null,S.partner,r):null}));
  if(!rows.length || !rows.some(r=>r.me)){ body.insertAdjacentHTML('beforeend','<div class="muted">No regatta summary yet — reopen the regatta (top bar) to analyse its races.</div>'); return; }
  const N=rows.length, fmtX=t=>'R'+Math.round(t);
  const opt={xmin:0.5, xmax:N+0.5, fmtX};
  const dser=(get,color,w)=>({color,w:w||2,data:rows.map(r=>[r.x,get(r)])});
  const dots=(get,color)=>rows.map(r=>({t:r.x,y:get(r),color})).filter(m=>m.y!=null);
  const dlt=(r,k,src)=> (src&&src[k]!=null&&r.fl[k]!=null)? +(src[k]-r.fl[k]).toFixed(2) : null;

  // 1) finish place
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Finish place by race (1 = winner, finishers only)</div>');
  const maxFin=Math.max(...rows.map(r=>r.me&&r.me.n_fin||0),10);
  S.charts.push(makeChart(body,{...opt,h:170,ymin:maxFin+1,ymax:0,fmtY:v=>v.toFixed(0),
    series:[dser(r=>r.me?r.me.rank:null,'#f5a623')].concat(p?[dser(r=>r.pr?r.pr.rank:null,'#3aa0ff',1.6)]:[]),
    marks:dots(r=>r.me?r.me.rank:null,'#f5a623').concat(p?dots(r=>r.pr?r.pr.rank:null,'#3aa0ff'):[])}));

  // 2) speed deltas vs fleet
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Speed vs fleet median (kt, + = faster)</div>');
  S.charts.push(makeChart(body,{...opt,h:170,fmtY:v=>v.toFixed(2),
    series:[dser(r=>dlt(r,'sog_up',r.me),'#4aa3ff'),dser(r=>dlt(r,'sog_dn',r.me),'#ff7b54')],
    marks:dots(r=>dlt(r,'sog_up',r.me),'#4aa3ff').concat(dots(r=>dlt(r,'sog_dn',r.me),'#ff7b54'))}));
  body.insertAdjacentHTML('beforeend',`<div class="muted"><span class="dot" style="background:#4aa3ff"></span> upwind &nbsp; <span class="dot" style="background:#ff7b54"></span> downwind — above 0 = faster than the fleet.</div>`);

  // 3) tack loss + distance deltas
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Tack loss (BL) &amp; distance (NM) vs fleet (below 0 = better)</div>');
  S.charts.push(makeChart(body,{...opt,h:170,fmtY:v=>v.toFixed(2),
    series:[dser(r=>dlt(r,'tack_loss',r.me),'#e6c229'),dser(r=>dlt(r,'dist_nm',r.me),'#bf7bff')],
    marks:dots(r=>dlt(r,'tack_loss',r.me),'#e6c229').concat(dots(r=>dlt(r,'dist_nm',r.me),'#bf7bff'))}));
  body.insertAdjacentHTML('beforeend',`<div class="muted"><span class="dot" style="background:#e6c229"></span> tack loss &nbsp; <span class="dot" style="background:#bf7bff"></span> distance sailed.</div>`);

  // 4) per-race table
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Per race</div>');
  let h=`<table><tr><th>Race</th><th>Place</th><th>ΔUp kt</th><th>ΔDn kt</th><th>ΔAngle°</th><th>ΔTack BL</th><th>ΔDist NM</th>${p?`<th>vs ${pn}</th>`:''}</tr>`;
  for(const r of rows){ const m=r.me; if(!m){h+=`<tr><td>${esc(r.race)}</td><td colspan=7 style="color:var(--mut)">no data</td></tr>`;continue;}
    const cell=(v,inv)=>v==null?'<td>–</td>':`<td><b class="${(inv?v<=0:v>=0)?'good':'bad'}">${v>0?'+':''}${v}</b></td>`;
    let pv='';
    if(p){ const d=(m.race_min!=null&&r.pr&&r.pr.race_min!=null)?Math.round((m.race_min-r.pr.race_min)*60):null;
      pv=d==null?'<td>–</td>':`<td><b class="${d<=0?'good':'bad'}">${d<=0?'':'+'}${d}s</b></td>`; }
    const place=m.rank?`P${m.rank}/${m.n_fin}`:m.code&&!NO_FINISH.has(m.code)?`<span title="official score">${esc(m.code)} ${m.pts??''}</span>`
      :`<span class="bad">${esc(m.code||'DNF')}</span>`;
    h+=`<tr><td>${esc(r.race)}</td><td>${place}</td>`+
       cell(dlt(r,'sog_up',m))+cell(dlt(r,'sog_dn',m))+cell(dlt(r,'twa_up',m),true)+
       cell(dlt(r,'tack_loss',m),true)+cell(dlt(r,'dist_nm',m),true)+pv+'</tr>'; }
  h+='</table>'; body.insertAdjacentHTML('beforeend',h);

  // 5) training priorities: how often each weakness shows up
  const iss=[
    ['upwind boatspeed', rows.filter(r=>dlt(r,'sog_up',r.me)<-0.05).length],
    ['downwind boatspeed', rows.filter(r=>dlt(r,'sog_dn',r.me)<-0.05).length],
    ['pointing (upwind angle)', rows.filter(r=>{const d=dlt(r,'twa_up',r.me);return d!=null&&d>2;}).length],
    ['tack execution (loss)', rows.filter(r=>{const d=dlt(r,'tack_loss',r.me);return d!=null&&d>0.3;}).length],
    ['sailing extra distance', rows.filter(r=>{const d=dlt(r,'dist_nm',r.me);return d!=null&&d>0.05;}).length],
  ].filter(x=>x[1]>=2).sort((a,b)=>b[1]-a[1]);
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Training priorities</div>'+
    (iss.length?`<div class="muted" style="font-size:13px;line-height:1.6"><ul style="margin:0;padding-left:18px">`+
      iss.map(([w,c])=>`<li><b>${w}</b> — below fleet in ${c}/${N} races</li>`).join('')+'</ul></div>'
    :'<div class="muted">No recurring weakness vs the fleet — gains are race-specific (see per-race table).</div>'));
  officialTable(body);
}
// Official overall results table (downloaded from the regatta's results page)
function officialTable(body){
  const R=S.results; if(!R||!R.rows.length) return;
  if(R._ix!==S.index) linkResults();
  const focus=officialRow(S.focus), part=S.partner?officialRow(S.partner):null;
  let h=`<div class="sectitle">Official results (${R.rows.length} boats${R.fetched?' · downloaded '+esc(R.fetched.slice(0,10)):''})</div>
    <div style="overflow-x:auto"><table class="offres"><tr><th>#</th><th>Sail</th><th style="text-align:left">Name</th><th>Cat</th><th>Net</th><th>Total</th>`+
    R.columns.map((c,i)=>`<th title="${esc((S.index.find(x=>x.id===R.map[i])||{}).race||'not a tracked race')}">${esc(c)}</th>`).join('')+'</tr>';
  for(const r of R.rows){
    const me=r===focus, pr=part&&r===part, nm=R._byName.get(r);
    h+=`<tr${me?' style="background:rgba(245,166,35,.18)"':pr?' style="background:rgba(58,160,255,.15)"':''}><td>${r.rank??''}</td><td>${esc(r.sail_raw)}`+
      `${nm?` <span class="muted" title="tracked as sail ${esc(nm)}: matched by name">≈${esc(nm)}</span>`:''}</td>`+
      `<td style="text-align:left">${esc(r.name)}</td><td>${esc(r.cat||'')}</td><td>${r.net??''}</td><td>${r.total??''}</td>`+
      r.scores.map(sc=>{ if(!sc) return '<td></td>'; const v=sc.code?`${sc.code}${sc.pts!=null?' '+Math.round(sc.pts):''}`:(sc.pts!=null?Math.round(sc.pts):'');
        return `<td${sc.code?' class="bad"':''}>${sc.disc?'('+v+')':v}</td>`; }).join('')+'</tr>';
  }
  body.insertAdjacentHTML('beforeend',h+'</table></div><div class="muted" style="font-size:11px">( ) = discarded · codes: UFD/BFD = over the line, DNF/RET = did not finish / retired, DNC = did not compete, DSQ = disqualified.</div>');
}

// ======================= DEBRIEF: course & decision analysis =======================
// Scores a boat's decisions phase by phase against modern ILCA fleet tactics (clear air,
// limit tacks, footing, controlled leverage, consolidation, starboard top-mark approach,
// runs & gate choice, second beat, rules, series) and classic principles of starts,
// shifts, laylines and roundings. All evidence is GPS-only and fleet-relative.
function gk(t){ const G=S.race.fleet_stats.t; return Math.max(0,Math.min(G.length-1,Math.round((t-G[0])/5))); }
function dirtyPct(b,t0,t1){ const s=b.shadow; if(!s||!(t1>t0)) return null; let n=0,d=0;
  for(let k=gk(t0);k<=gk(t1);k++){ const c=s[k]; if(c==null||c==='-') continue; n++; if(c!=='0') d++; }
  return n>=3?100*d/n:null; }
const medN=a=>{ a=a.filter(v=>v!=null&&isFinite(v)).sort((x,y)=>x-y); return a.length?a[a.length>>1]:null; };
const pctl=(a,p)=>{ a=a.filter(v=>v!=null&&isFinite(v)).sort((x,y)=>x-y); return a.length?a[Math.min(a.length-1,Math.floor(p*(a.length-1)))]:null; };
const mmss=t=>fmtT(t-S.tMin);
const plur=(n,w)=>`${n} ${w}${n===1?'':'s'}`;
function dbPool(){ const p=finishers().slice(); for(const b of [focusB(),partnerB()]) if(b&&!p.includes(b)) p.push(b); return p; }
// course leg index -> boat leg index (cached per legs array; rebuilt after mark edits)
function ciMap(b){ const lg=boatLegs(b); if(b._cim&&b._cimRef===lg) return b._cim;
  const m={}; lg.forEach((_,li)=>{ const ci=courseIdx(b,li); if(ci>=0&&!(ci in m)) m[ci]=li; });
  b._cim=m; b._cimRef=lg; return m; }
// Rounding time of course leg ci's mark for boat b (null if b never sailed that leg).
function dbRoundT(b,ci){ if(ci==null||ci<0) return null; const li=ciMap(b)[ci]; if(li==null) return null;
  const rr=b._mlegs?null:b.rounds; return rr&&rr[li]!=null?rr[li]:b.t[boatLegs(b)[li][1]]; }
function isLastFor(b,ci){ const lg=boatLegs(b); return lg.length>0 && courseIdx(b,lg.length-1)===ci; }
// b crossed the finish line at the end of course leg ci (retirees never "finish")
function fleetFinCi(){ const r=S.race; if(r._finCi!=null) return r._finCi;
  const c={}; for(const b of r.boats){ const lg=boatLegs(b); if(!b.summary.finished||!lg.length) continue;
    const x=courseIdx(b,lg.length-1); if(x>=0) c[x]=(c[x]||0)+1; }
  const k=Object.keys(c).sort((x,y)=>c[y]-c[x])[0]; return r._finCi=(k==null?-1:+k); }
function finishesAt(b,ci){ return (officialPlace(b)!=null || !!b.summary.finished) && isLastFor(b,ci) && ci===fleetFinCi(); }
function dbRankAtMark(b,i,pl){ const my=dbRoundT(b,i); if(my==null) return null; let r=1;
  for(const o of pl){ if(o===b) continue; const ot=dbRoundT(o,i); if(ot!=null&&ot<my) r++; } return r; }
function dbRankAtT(b,t,pl){ const k=gk(t), my=boatCourse(b)[k]; if(my==null) return null; let r=1;
  for(const o of pl){ if(o===b) continue; const v=boatCourse(o)[k]; if(v!=null&&v>my) r++; } return r; }
function dbFinishRank(b,pl){                     // within the same pool as the mark ranks
  if(noFinish(officialScore(b))) return null;      // official DNF/UFD/DSQ...: no place
  const gps=o=>o.summary.finished&&o.summary.race_min!=null?o.summary.race_min:null;
  // official order, with the boats that have no official place (ZFP etc., unmatched sail)
  // merged in by GPS finish time — one order, so no two boats share a place
  const ok=pl.includes(b)?pl:pl.concat([b]), rest=ok.filter(o=>!noFinish(officialScore(o)));
  const off=rest.filter(o=>officialPlace(o)!=null).sort((x,y)=>officialPlace(x)-officialPlace(y));
  const oth=rest.filter(o=>officialPlace(o)==null&&gps(o)!=null).sort((x,y)=>gps(x)-gps(y));
  const order=[]; let i=0;
  for(const c of oth){                               // before the first placed boat that GPS has behind it
    let j=i; while(j<off.length&&!(gps(off[j])!=null&&gps(off[j])>gps(c))) j++;
    while(i<j) order.push(off[i++]);
    order.push(c); }
  while(i<off.length) order.push(off[i++]);
  const k=order.indexOf(b); return k<0?null:k+1; }
function windVecs(t){ const w=windAtT(t)*Math.PI/180;           // u = upwind, l = right looking upwind
  return {u:[Math.sin(w),Math.cos(w)], l:[Math.cos(w),-Math.sin(w)]}; }
function countIn(arr,t0,t1,kind){ return (arr||[]).filter(m=>m.t>=t0&&m.t<=t1&&(!kind||m.kind===kind)).length; }
function trackMean(b,t0,t1,f){ const i0=idxAt(b,t0), i1=idxAt(b,t1); let s=0,n=0;
  for(let i=i0;i<=i1;i++){ const v=f(i); if(v!=null&&isFinite(v)){ s+=v; n++; } } return n?s/n:null; }
// Course leg index of boat b's leg li (server course_ix: matched by leg-type order;
// legs re-derived from hand-placed marks follow the course one to one).
function courseIdx(b,li){ if(b._mlegs) return li; const cx=b.course_ix; if(!cx) return li;
  return cx[li]==null?-1:cx[li]; }
// leg number shown to the user: the course leg (as in Key findings / Position)
function legNo(b,li){ const ci=courseIdx(b,li); return (ci>=0?ci:li)+1; }
function markForRound(ci){ const leg=courseLegs()[ci]; return leg?markBy(leg.mark):[]; }
// seconds spent inside the R-metre zone around the mark of course leg ci
function roundingZone(o,ci,R=50){ const t=dbRoundT(o,ci); if(t==null) return null; const mk=markForRound(ci); if(!mk.length) return null;
  const d=j=>Math.min(...mk.map(m=>distM(o.lat[j],o.lon[j],m[0],m[1])));
  const j0=idxAt(o,t); if(d(j0)>R) return null; let a=j0,c=j0;
  while(a>0&&d(a-1)<R&&o.t[j0]-o.t[a-1]<120) a--;
  while(c<o.t.length-1&&d(c+1)<R&&o.t[c+1]-o.t[j0]<120) c++;
  return o.t[c]-o.t[a]>=3?o.t[c]-o.t[a]:null; }

function debriefChecks(b){
  const r=S.race, pl=dbPool(), gun=r.gun_t||r.start_t, legs=boatLegs(b), out=[], legChanges=[];
  const add=(phase,v,title,text,t,impact,outcome)=>out.push({phase,v,title,text,t:t??null,impact:impact||0,outcome:!!outcome});

  // ---------------- START ----------------
  const P='Start', wv=windVecs(gun);
  const gp=pl.map(o=>{ const s=sampleAt(o,gun); return s.inrace?{o,p:toXY(s.lat,s.lon)}:null; }).filter(Boolean);
  const A=x=>x.p[0]*wv.u[0]+x.p[1]*wv.u[1], L=x=>x.p[0]*wv.l[0]+x.p[1]*wv.l[1];
  const me=gp.find(x=>x.o===b);
  if(me && gp.length>=8){
    const ls=gp.map(L), l5=pctl(ls,0.05), l95=pctl(ls,0.95), span=Math.max(1,l95-l5);
    const third=x=>{ const f=(L(x)-l5)/span; return f<1/3?0:f<2/3?1:2; };      // 0 pin(left) .. 2 committee(right)
    const NAMES=['pin end','middle','committee-boat end'];
    // line = front of the fleet at the gun: 80th pct along-wind per lateral bin, then a straight fit
    const bins=[[],[],[],[],[],[]];
    for(const x of gp){ const f=(L(x)-l5)/span; bins[Math.max(0,Math.min(5,Math.floor(f*6)))].push(x); }
    const pts=bins.map(bb=>bb.length>=2?[medN(bb.map(L)),pctl(bb.map(A),0.8)]:null).filter(Boolean);
    let slope=0, icpt=pctl(gp.map(A),0.85), biasDeg=null;
    if(pts.length>=3){ const mx=mean(pts.map(q=>q[0])), my=mean(pts.map(q=>q[1])); let sxy=0,sxx=0;
      for(const [x,y] of pts){ sxy+=(x-mx)*(y-my); sxx+=(x-mx)**2; }
      if(sxx>0){ slope=sxy/sxx; icpt=my-slope*mx; biasDeg=Math.atan(slope)*180/Math.PI; } }
    const front=x=>icpt+slope*L(x);
    const m1=o=>dbRankAtMark(o,0,pl);
    const thirdRank=[0,1,2].map(th=>medN(gp.filter(x=>third(x)===th).map(x=>m1(x.o))));
    const valid=thirdRank.filter(v=>v!=null), best=thirdRank.indexOf(Math.min(...valid)), myTh=third(me);
    const fav = biasDeg==null||Math.abs(biasDeg)<4 ? null : (biasDeg>0?2:0);
    add(P, myTh===best?'good':(thirdRank[myTh]!=null&&thirdRank[myTh]-thirdRank[best]>=6?'warn':'info'), 'Start position on the line',
      `You started at the <b>${NAMES[myTh]}</b>. `+
      (fav!=null?`The front of the fleet suggests the <b>${NAMES[fav]}</b> was favoured (~${Math.abs(biasDeg).toFixed(0)}°). `:'The line looked roughly square. ')+
      `Mark-1 median place by start position: pin P${thirdRank[0]??'–'} · middle P${thirdRank[1]??'–'} · boat end P${thirdRank[2]??'–'}.`, gun);
    const ahead=A(me)-front(me);
    if(ahead>6){
      const a0=A(me); let ret=null;     // a return = falling >12 m back downwind within 90 s of the gun
      for(let t=gun+3;t<=gun+90;t+=3){ const s=sampleAt(b,t); if(!s.inrace) break;
        const [x,y]=toXY(s.lat,s.lon); if(x*wv.u[0]+y*wv.u[1]<a0-12){ ret=t; break; } }
      add(P,'warn','Over the line at the gun?',
        `At the gun you were ~${ahead.toFixed(0)} m ahead of the local front row — likely over early. `+
        (ret?`You turned back at ${mmss(ret)} (looks like a return). `:'No return detected — check the results for OCS. ')+
        'Know the flag first: under U or black flag there is no return (UFD/BFD) — sail on and reset for the next race.', gun, 5);
    } else {
      // ILCA: on the line at the gun at full speed (~1 length back at 5 s, then accelerate). Measured
      // behind the fleet's front row; upwind a Laser covers ~½ length (2 m) per second: <= 2 m is in
      // the front row, from ~1½ lengths (6 m, ~3 s late) the boat starts in the second row.
      const behind=Math.round(Math.max(0,-ahead)), bl=(behind/4.2).toFixed(1);   // judged as shown (whole m)
      const v=behind<=2?'good':behind<6?'warn':'bad';
      add(P, v, 'Line position at the gun',
        v==='good'?`On the line at the gun (${behind} m behind the fleet front).`
        :v==='warn'?`${behind} m (~${bl} boat lengths) behind the front row at the gun — a second or two late. `+
                    'Be about a length back with 5 s to go and hit the line at full speed with the gun.'
        :`${behind} m (~${bl} boat lengths, ~${Math.round(behind/2)} s late) behind the front row at the gun — a second-row `+
         'start. Hold your spot closer to the line (half a length from the boats either side) and pull the trigger earlier.',
        gun, v==='bad'?2:v==='warn'?1:0);
    }
  }
  // speed / footing in the first 90 s
  const sp=o=>trackMean(o,gun+10,gun+90,i=>o.sog[i]), tw=o=>trackMean(o,gun+10,gun+90,i=>o.twa[i]<80?o.twa[i]:null);
  const mySp=sp(b), myTw=tw(b), fSp=medN(pl.map(sp)), fTw=medN(pl.map(tw));
  if(mySp!=null&&fSp!=null){
    const dS=mySp-fSp, dT=(myTw!=null&&fTw!=null)?myTw-fTw:0;
    const v= dS>=-0.1?'good':(dS<-0.25&&dT<-1)?'bad':'warn';
    add(P,v,'Speed off the line (first 90 s)',
      `${mySp.toFixed(2)} kt (${fmtS(dS)} vs fleet), angle ${myTw!=null?myTw.toFixed(0):'–'}° (${fmtS(dT,0)}° vs fleet). `+
      (v==='bad'?'Slow and high = pinching. Shift into footing mode: ease the main slightly, add vang, drive for speed until clear.':
       v==='good'?(dT>2?'Footing for speed — right call off the line.':'Good acceleration.'):'A bit slow off the line.'), gun+30, v==='bad'?2:0);
  }
  // clear air first 3 min
  const d3=o=>dirtyPct(o,gun,gun+180), myD=d3(b), fD=medN(pl.map(d3));
  if(myD!=null&&fD!=null){
    const after=dirtyPct(b,gun+180,gun+300);
    const v= myD<=fD+5?'good':(myD>=50&&myD>fD+25)?'bad':'warn';
    add(P,v,'Clear air after the gun',
      `Dirty air ${myD.toFixed(0)}% of the first 3 min (fleet median ${fD.toFixed(0)}%)`+(after!=null?`, ${after.toFixed(0)}% in minutes 3–5`:'')+'. '+
      (v==='good'?'Clean lane.':'Clear air beats position: in a 50-boat fleet give up 2–3 lengths for a clean lane rather than fight in bad air.'),
      gun+60, v==='bad'?3:v==='warn'?1:0);
  }
  // limit tacks
  const tk3=o=>countIn(o.maneuvers,gun,gun+180,'tack'), myTk=tk3(b), fTk=medN(pl.map(tk3));
  const many=myTk>=3&&myTk>(fTk||0)+1, stuck=!many&&myTk===0&&myD!=null&&fD!=null&&myD>=40&&myD>fD+15;
  add(P, many||stuck?'warn':'good','Tacks in the first 3 min',
    `${plur(myTk,'tack')} (fleet median ${fTk??'–'}). `+
    (many?'Limit tacks: one decisive move to clear air beats several panicky ones — each ILCA tack costs real distance.':
     stuck?'You stayed in bad air without tacking out — one decisive tack to a clean lane may have paid.':'Measured.'), gun+90, many||stuck?1:0);

  // ---------------- LEGS ----------------
  let beatNo=0, runNo=0;
  legs.forEach(([a,c,kind],li)=>{
    const ci=courseIdx(b,li), cp=li>0?courseIdx(b,li-1):-1;          // course legs (this / previous)
    const tA=li===0?gun:dbRoundT(b,cp), tB=dbRoundT(b,ci); if(tA==null||tB==null||tB<=tA) return;
    const isLast=li===legs.length-1;
    const endRank=o=>finishesAt(o,ci)?dbFinishRank(o,pl):dbRankAtMark(o,ci,pl);
    const r0=li===0?dbRankAtT(b,gun+120,pl):dbRankAtMark(b,cp,pl), r1=endRank(b);
    const dPl=(r0!=null&&r1!=null)?r0-r1:null;                       // + = places gained
    const win=o=>[li===0?gun:dbRoundT(o,cp),
      (finishesAt(o,ci)&&o.summary.race_min!=null)?gun+o.summary.race_min*60:dbRoundT(o,ci)];
    const PH=kind==='upwind'?(++beatNo===1?'First beat':beatNo===2?'Second beat':`Beat ${beatNo}`):
             kind==='downwind'?(++runNo===1?'First run':`Run ${runNo}`):`Reach (leg ${(ci>=0?ci:li)+1})`;
    if(dPl!=null) add(PH, dPl>=3?'good':dPl<=-3?'bad':'info', 'Places on the leg',
      `P${r0} → P${r1} (${dPl>0?'+':''}${dPl}).`+
      (kind==='downwind'&&dPl>=3?' The run is the biggest chance to gain — well used.':
       kind==='downwind'&&dPl<=-3?' Runs reward clear air, waves and sailing by the lee; gybe away from the pack\'s shadow.':
       kind==='upwind'&&beatNo>=2&&dPl>=3?' Treated the second beat as a fresh start.':''), tA, dPl<0?-dPl:0, true);
    // clear air on the leg
    const dl=o=>{ const [x0,x1]=win(o); return (x0!=null&&x1!=null)?dirtyPct(o,x0+10,x1-10):null; };
    const myDl=dl(b), fDl=medN(pl.map(dl));
    if(myDl!=null&&fDl!=null&&kind!=='reach'){
      const v=myDl<=fDl+5?'good':myDl>fDl+20?'bad':'warn';
      add(PH,v,'Clear air',`Dirty air ${myDl.toFixed(0)}% of the leg (fleet median ${fDl.toFixed(0)}%).`+
        (v!=='good'?(kind==='downwind'?' Gybe away from the shadow of the boats behind you.':' Look for the gaps — the side with fewer boats.'):''),
        tA, v==='bad'?2:v==='warn'?1:0);
    }
    if(kind==='upwind'){
      const tkl=o=>{ const [x0,x1]=win(o); return x0!=null&&x1!=null?countIn(o.maneuvers,x0,x1,'tack'):null; };
      const myTkl=tkl(b), fTkl=medN(pl.map(tkl));
      const losses=(b.maneuvers||[]).filter(m=>m.kind==='tack'&&m.t>=tA&&m.t<=tB).map(m=>m.bl_lost);
      const extra=myTkl>(fTkl||0)+3;
      add(PH, extra?'warn':'info','Tacks', `${plur(myTkl,'tack')} (fleet median ${fTkl??'–'})`+
        (losses.length?`, avg loss ${mean(losses).toFixed(2)} BL`:'')+'.'+(extra?' Extra tacks cost distance — tack on the shifts that matter.':''), tA, extra?1:0);
      // side of the course + leverage, relative to the fleet centre on this leg
      const wvL=windVecs((tA+tB)/2);
      const qAt=(o,t)=>{ const s=sampleAt(o,t); if(!s.inrace) return null; const [x,y]=toXY(s.lat,s.lon); return x*wvL.l[0]+y*wvL.l[1]; };
      const onLeg=(o,t)=>{ const [x0,x1]=win(o); return x0!=null&&x1!=null&&t>=x0&&t<=x1; };
      const w0s=pl.map(o=>win(o)[0]).filter(v=>v!=null), w1s=pl.map(o=>win(o)[1]).filter(v=>v!=null);
      const ux0=Math.min(...w0s), ux1=Math.max(...w1s), STEP=20, cen=[];
      for(let t=ux0;t<=ux1;t+=STEP) cen.push(medN(pl.filter(o=>onLeg(o,t)).map(o=>qAt(o,t))));
      const cenAt=t=>cen[Math.max(0,Math.min(cen.length-1,Math.round((t-ux0)/STEP)))];
      const offStats=o=>{ const [x0,x1]=win(o); if(x0==null||x1==null) return null; let s=0,n2=0,mx=0;
        for(let t=x0+20;t<=x1-20;t+=STEP){ const q=qAt(o,t), c0=cenAt(t); if(q==null||c0==null) continue;
          const d=q-c0; s+=d; n2++; if(Math.abs(d)>Math.abs(mx)) mx=d; }
        return n2?{mean:s/n2,max:mx}:null; };
      const OS=new Map(pl.map(o=>[o,offStats(o)])), my=OS.get(b);
      if(my){
        const means=pl.map(o=>OS.get(o)?OS.get(o).mean:null).filter(v=>v!=null);
        const c1=pctl(means,1/3), c2=pctl(means,2/3), thirdOf=v=>v<c1?0:v<c2?1:2, SIDE=['left','middle','right'];
        const sideRank=[0,1,2].map(th=>medN(pl.filter(o=>OS.get(o)&&thirdOf(OS.get(o).mean)===th).map(endRank)));
        const mySide=thirdOf(my.mean), okR=sideRank.filter(v=>v!=null), bestSide=sideRank.indexOf(Math.min(...okR));
        const sameFrac=means.filter(v=>Math.sign(v)===Math.sign(my.mean)).length/means.length;
        add(PH, mySide===bestSide?'good':(sideRank[mySide]!=null&&sideRank[mySide]-sideRank[bestSide]>=6?'warn':'info'), 'Side of the course',
          `You worked the <b>${SIDE[mySide]}</b> (avg ${Math.abs(my.mean/ILCA).toFixed(0)} BL ${my.mean>=0?'right':'left'} of the fleet centre, looking upwind). `+
          `End-of-leg median place: left P${sideRank[0]??'–'} · middle P${sideRank[1]??'–'} · right P${sideRank[2]??'–'}. `+
          (mySide===bestSide?'You were on the side that paid.':`The ${SIDE[bestSide]} paid.`)+
          (sameFrac>=0.6&&myDl!=null&&fDl!=null&&myDl>fDl?` You went with the crowd (${Math.round(sameFrac*100)}% of the fleet) and got more dirty air — look for the gaps.`:''), tA);
        const maxs=pl.map(o=>OS.get(o)?Math.abs(OS.get(o).max):null).filter(v=>v!=null), p90=pctl(maxs,0.9), myMax=Math.abs(my.max);
        if(p90!=null&&myMax>=p90){
          const v=dPl!=null&&dPl>=4?'good':dPl!=null&&dPl<=-4?'bad':'warn';
          add(PH,v,'Leverage (corner / flyer)',
            `Max ${(myMax/ILCA).toFixed(0)} BL from the fleet centre — top 10% most extreme. `+
            (v==='good'?'Controlled leverage that paid.':v==='bad'?'The flyer didn\'t pay. Lean to the favoured side, but don\'t bang a corner without a persistent shift or current — a 30th is recoverable, a 55th becomes the discard.':'High risk for little gain.'),
            tA, v==='bad'?Math.abs(dPl||0):0);
        } else add(PH,'good','Leverage (corner / flyer)',`Controlled: max ${(myMax/ILCA).toFixed(0)} BL from the fleet centre (fleet 90th pct ${p90!=null?(p90/ILCA).toFixed(0):'–'} BL).`, tA);
        // consolidation of the biggest gain on this leg
        let ev=null;
        for(let t=tA+120;t<=tB-20;t+=15){ const ra=dbRankAtT(b,t-120,pl), rb=dbRankAtT(b,t,pl);
          if(ra!=null&&rb!=null&&(!ev||ra-rb>ev.g)) ev={t,g:ra-rb,r:rb}; }
        if(ev&&ev.g>=3){
          const t2=Math.min(ev.t+90,tB), q0=qAt(b,ev.t), q1=qAt(b,t2), k0=cenAt(ev.t), k1=cenAt(t2);
          if(q0!=null&&q1!=null&&k0!=null&&k1!=null){
            const o0=Math.abs(q0-k0), o1=Math.abs(q1-k1), tackAfter=countIn(b.maneuvers,ev.t,t2,'tack')>0;
            const cons=o1<o0-10||(tackAfter&&o1<=o0), rEnd=endRank(b), kept=rEnd!=null?ev.r-rEnd:null;
            const v=cons?(kept!=null&&kept>=-2?'good':'info'):(kept!=null&&kept<=-3?'warn':'info');
            add(PH,v,'Consolidate gains',
              `Biggest gain: +${ev.g} places by ${mmss(ev.t)} (to P${ev.r}). Then you ${cons?'came back toward the fleet (consolidated)':'kept extending away from the fleet'}; leg ended P${rEnd??'–'}. `+
              (v==='warn'?'Once a shift lets you cross boats, lock it in by tacking in front of them — don\'t keep extending.':''), ev.t, v==='warn'?Math.max(0,-kept):0);
          }
        }
      }
      // playing the shifts on this beat
      const td=tackDecisions(b), inL=x=>x.t>=tA&&x.t<=tB;
      const good=td.real.filter(x=>inL(x)&&x.good===true).length, poor=td.real.filter(x=>inL(x)&&x.good===false).length, miss=td.missed.filter(inL).length;
      if(good+poor+miss>0) add(PH, poor+miss>good?'warn':'good','Playing the shifts',
        `${plur(good,'tack')} onto lifts, ${poor} off lifts, ${plur(miss,'missed header')} (Maneuvers → Tack decisions).`, tA, poor+miss>good?1:0);
      // top-mark approach
      if(!isLast){
        const sg=[]; for(let t=tB-60;t<=tB-12;t+=3){ const s=sampleAt(b,t); if(s.inrace) sg.push(s.tack); }
        const onPort=sg.filter(v=>v>0).length>sg.length/2, late=countIn(b.maneuvers,tB-40,tB-3,'tack');
        const twA=trackMean(b,tB-50,tB-10,i=>b.twa[i]<110?b.twa[i]:null), tUp=b.summary.twa_up;
        const over=(twA!=null&&tUp!=null)?twA-tUp:null;
        const traffic=pl.filter(o=>o!==b&&dbRoundT(o,ci)!=null&&Math.abs(dbRoundT(o,ci)-tB)<=20).length;
        let v='good', txt=`Approached on <b>${onPort?'port':'starboard'}</b>${late?`, tacked ${late}× in the last 40 s`:''}; ${traffic} boat${traffic!==1?'s':''} rounding within ±20 s.`;
        if(onPort&&late>0&&traffic>=3){ v='warn'; txt+=' Avoid the port-layline pile: come in on starboard a little early rather than tacking into the queue\'s wind shadow.'; }
        if(over!=null&&over>=12){ v='warn'; txt+=` Overstood: sailed ~${over.toFixed(0)}° freer than close-hauled into the mark (extra distance).`; }
        const cl=courseLegs()[ci];
        add(PH,v,`Approach to ${!cl?'the mark':cl.gate?'the gate':'mark '+esc(cl.mark)}`,txt,tB-40, v==='warn'?1:0);
      }
    }
    if(kind==='downwind'){
      const gyl=o=>{ const [x0,x1]=win(o); return x0!=null&&x1!=null?countIn(o.maneuvers,x0,x1,'gybe'):null; };
      const myG=gyl(b), fG=medN(pl.map(gyl));
      add(PH,'info','Gybes',`${plur(myG,'gybe')} (fleet median ${fG??'–'}).`, tA);
      const cl=courseLegs()[ci], G=cl&&cl.gate?markBy(cl.mark):[];
      if(G.length===2&&!isLast){
        const gateOf=o=>{ const t=dbRoundT(o,ci); if(t==null) return null; const bd=[1e9,1e9];
          for(let i=idxAt(o,t-30);i<=idxAt(o,t+30);i++) for(const g of [0,1]) bd[g]=Math.min(bd[g],distM(o.lat[i],o.lon[i],G[g][0],G[g][1]));
          return Math.min(...bd)>60?null:(bd[0]<=bd[1]?0:1); };
        const GO=new Map(pl.map(o=>[o,gateOf(o)])), gi=GO.get(b), used=[...GO.values()].filter(v=>v!=null);
        const share=used.length?used.filter(v=>v===0).length/used.length:0;
        if(gi!=null&&share>=0.15&&share<=0.85){
          const near=pl.filter(o=>o!==b&&GO.get(o)!=null&&Math.abs(dbRoundT(o,ci)-tB)<=25);
          const same=near.filter(o=>GO.get(o)===gi).length, other=near.length-same;
          const wv2=windVecs(tB), q=g=>{ const [x,y]=toXY(g[0],g[1]); return x*wv2.l[0]+y*wv2.l[1]; };
          const side=q(G[gi])>q(G[1-gi])?'right':'left';
          const cn=courseIdx(b,li+1), nr0=dbRankAtMark(b,ci,pl), nr1=finishesAt(b,cn)?dbFinishRank(b,pl):dbRankAtMark(b,cn,pl);
          const nd=(nr0!=null&&nr1!=null)?nr0-nr1:null, v=same>other+2?'warn':'good';
          add(PH,v,'Gate choice',`Rounded the <b>${side}</b> gate mark (looking upwind) with ${plur(same,'boat')} within ±25 s; ${other} at the other one `+
            `(fleet split ${Math.round(share*100)}/${Math.round((1-share)*100)}). `+(nd!=null?`Next leg: ${nd>0?'+':''}${nd} places. `:'')+
            (v==='warn'?'Pick the less crowded gate mark, especially if it leads to the favoured side.':'Less crowded choice.'), tB, v==='warn'?1:0);
        } else if(gi!=null) add(PH,'info','Gate choice','The whole fleet rounded near the same detected gate mark — no choice to evaluate (if that looks wrong, check the G marks on the map).', tB);
      }
    }
    if(!isLast){
      const z=roundingZone(b,ci), fz=medN(pl.map(o=>roundingZone(o,ci)));
      if(z!=null&&fz!=null){ const loss=z-fz;
        add(PH, loss<=3?'good':loss>10?'warn':'info','Mark rounding',`${z.toFixed(0)} s through the 50 m zone (fleet median ${fz.toFixed(0)} s, ${fmtS(loss,0)} s).`+
          (loss>10?' Wide in, tight out: set up wide, exit close to the mark on the new course.':''), tB, loss>10?1:0); }
    }
    legChanges.push({li,label:PH,dPl});
  });

  // ---------------- MINDSET ----------------
  const ranks=[dbRankAtT(b,gun+120,pl)]; for(let i=0;i<legs.length-1;i++) ranks.push(dbRankAtMark(b,courseIdx(b,i),pl));
  const fin=dbFinishRank(b,pl); ranks.push(fin);
  const vr=ranks.filter(v=>v!=null);
  if(fin!=null&&vr.length>=2){
    const worst=Math.max(...vr), best=Math.min(...vr);
    if(worst-fin>=5) add('Mindset','good','Recovery',`From P${worst} at your worst point to P${fin} at the finish (+${worst-fin}). In a series, recovering places is worth more than a gamble.`,null,0,true);
    else if(fin-best>=8) add('Mindset','bad','Fading',`Best position P${best}, finished P${fin} (−${fin-best}). Protect a good position: cover the boats behind, avoid new leverage.`,null,fin-best);
    else add('Mindset','info','Position through the race',`Start+2 min / marks / finish: ${vr.map(v=>'P'+v).join(' → ')}.`,null,0,true);
  }
  const worstLeg=legChanges.filter(x=>x.dPl!=null).sort((x,y)=>x.dPl-y.dPl)[0];
  if(worstLeg&&worstLeg.dPl<=-3){ const nx=legChanges.find(x=>x.li===worstLeg.li+1);
    if(nx&&nx.dPl!=null) add('Mindset', nx.dPl>=2?'good':nx.dPl<=-2?'warn':'info','Reset after the worst leg',
      `Lost ${-worstLeg.dPl} on the ${worstLeg.label.toLowerCase()}, then ${nx.dPl>=0?'+':''}${nx.dPl} on the next leg. `+
      (nx.dPl<=-2?'Reset quickly: 30 s on boat speed and breathing, then one clear plan for the leg.':nx.dPl>=2?'Reset well.':''), null, nx.dPl<0?-nx.dPl:0); }

  // ---------------- RULES ----------------
  const pens=b.penalties||[];
  if(pens.length) for(const p2 of pens) add('Rules','bad',p2.type==='720'?'Possible two-turn penalty':'Possible 360° turn',
    `${Math.abs(p2.deg)}° net rotation in ${p2.dur} s at ${mmss(p2.t)}${p2.t<gun+120?' (could also be an OCS return loop)':''}. Avoid fouls in the scramble — give way early; a penalty in traffic costs far more than a duck.`,
    p2.t, p2.type==='720'?3:2);
  else add('Rules','good','Penalty turns','No penalty-turn signature detected.');
  add('Rules','info','Rule 42 (pumping / rocking)','Not measurable from 3-second GPS. Free pumping only once flag O is up — frustration-pumping risks a yellow flag.');

  // ---------------- SERIES ----------------
  const series=(S.index||[]).map(x=>({i:x.i, race:x.race, rk:(x.boats||{})[b.sail]?.rank??null, n:(x.boats||{})[b.sail]?.n_fin}));
  const done=series.filter(x=>x.rk!=null);
  // official series: every scored column of the boat's row, tracked races or not
  const R=S.results, orow=officialRow(b.sail), cur=curRaceId();
  const colName=k=>{ const x=(S.index||[]).find(y=>y.id===R.map[k]); return x?x.race:'R'+(R.races||[])[k]; };
  const ocols=orow?orow.scores.map((s,k)=>({k,s})).filter(x=>x.s):[], odisc=ocols.filter(x=>x.s.disc);
  const pts=s=>s.pts!=null?s.pts:1e9;                // a code without points: worst
  if(odisc.length){ const here=odisc.some(x=>R.map[x.k]===cur);
    add('Series',here?'warn':'info','Discard (official)',`Discarded in the official results: ${odisc.map(x=>`${esc(colName(x.k))} (${scoreText(x.s)})`).join(', ')}. `+
      (here?'This race is your discard — the next bad one will count, so keep the risk controlled.':'This race counts.')); }
  else if(ocols.length>=2){ const worst=ocols.reduce((p1,c1)=>pts(c1.s)>pts(p1.s)?c1:p1), here=ocols.find(x=>R.map[x.k]===cur), isD=here&&worst.k===here.k;
    add('Series',isD?'warn':'info','Discard (official)',`Worst official score so far (likely discard): ${esc(colName(worst.k))} ${scoreText(worst.s)}. `+
      (isD?'This race is your discard — the next bad one will count, so keep the risk controlled.':`This race (${here?scoreText(here.s):'–'}) counts.`)); }
  else if(done.length>=2){ const worst=done.reduce((p1,c1)=>c1.rk>p1.rk?c1:p1), here=series.find(x=>x.i===S.raceIdx), isD=here&&worst.i===here.i;
    add('Series',isD?'warn':'info','Discard',`Worst race so far (likely discard): ${worst.race} P${worst.rk}/${worst.n}. `+
      (isD?'This race is your discard — the next bad one will count, so keep the risk controlled.':`This race (P${here?.rk??'–'}) counts.`)); }
  return out;
}

function tabDebrief(body){
  const f=focusB(), p=partnerB(), subj=(S.dbSubj==='cmp'&&p)?p:f;
  if(p){ const bar=document.createElement('div'); bar.className='filterbar';
    bar.innerHTML=`Analyze: <span class="pill ${subj===f?'on':''}" data-s="focus">${f.name}</span><span class="pill ${subj===p?'on':''}" data-s="cmp">${p.name}</span>`;
    body.appendChild(bar); bar.querySelectorAll('[data-s]').forEach(el=>el.onclick=()=>{ S.dbSubj=el.dataset.s; setTab('debrief'); }); }
  const ch=debriefChecks(subj), cnt=v=>ch.filter(x=>x.v===v).length;
  const ICON={good:'<b style="color:#3fb950">✔</b>',warn:'<b style="color:#e3b341">⚠</b>',bad:'<b style="color:#f85149">✖</b>',info:'<b style="color:#8b949e">ℹ</b>'};
  body.insertAdjacentHTML('beforeend',`<div class="sectitle">Decision debrief — ${subj.name}</div>
    <div class="muted" style="font-size:13px">${ICON.good} ${cnt('good')} good · ${ICON.warn} ${cnt('warn')} to improve · ${ICON.bad} ${cnt('bad')} costly · ${ICON.info} ${cnt('info')} info</div>`);
  const top=ch.filter(x=>(x.v==='bad'||x.v==='warn')&&!x.outcome).sort((x,y)=>((y.v==='bad')-(x.v==='bad'))||(y.impact-x.impact)).slice(0,3);
  if(top.length) body.insertAdjacentHTML('beforeend','<div class="sectitle">Top things to work on</div><ol class="muted" style="font-size:13px;line-height:1.6;margin:0;padding-left:20px">'+
    top.map(x=>`<li><b>${x.phase} — ${x.title}:</b> ${x.text}</li>`).join('')+'</ol>');
  const phases=[]; for(const x of ch) if(!phases.includes(x.phase)) phases.push(x.phase);
  const wrap=document.createElement('div'); body.appendChild(wrap);
  let h='';
  for(const ph of phases){ h+=`<div class="sectitle">${ph}</div><table>`;
    for(const x of ch.filter(y=>y.phase===ph))
      h+=`<tr ${x.t!=null?`class="man" data-dt="${x.t}"`:''}><td style="width:22px">${ICON[x.v]}</td>`+
         `<td style="text-align:left"><b>${x.title}</b><br><span class="muted">${x.text}</span></td>`+
         `<td style="width:58px;white-space:nowrap;color:var(--mut)">${x.t!=null?'▶ '+mmss(x.t):''}</td></tr>`;
    h+='</table>'; }
  wrap.innerHTML=h;
  wrap.querySelectorAll('tr[data-dt]').forEach(tr=>tr.onclick=()=>{ S.t=+tr.dataset.dt; syncTime(); });
  body.insertAdjacentHTML('beforeend',`<div class="muted" style="font-size:11px;margin-top:10px">Checks follow modern ILCA fleet tactics
    (clear air over position, limit tacks, footing off the line, controlled leverage, consolidating gains, starboard top-mark approach,
    runs &amp; gate choice, second beat as a fresh start, rules, series) and classic principles of starts, shifts, laylines and roundings
    (R. Pattison, <i>Tactics: Sail to Win</i>). Evidence is GPS-only and fleet-relative: dirty air = another boat upwind within ~8 boat
    lengths; the line and its bias are estimated from the front of the fleet at the gun; penalty turns = large net rotation. Click a row
    to jump the map there — use the <b>Air</b> colour mode to see clean/dirty air on the track. Not measurable: Rule 42, flags, protests.</div>`);
}

// ---------- wind arrow ----------
function setWind(deg){ $('#windVal').textContent=deg.toFixed(0)+'°';
  $('#windG').setAttribute('transform',`rotate(${(deg+180)%360} 13 13)`); }  // arrow points the way wind blows TO

// ---------- load ----------
// Regattas (registry) -> races of the selected regatta (index) -> one race.
async function fetchRegattas(){
  let j=null;
  try{ j=await api('/api/regattas'); if(!Array.isArray(j.regattas)) throw new Error('unexpected reply'); }
  catch(e){ setBusy('Could not read the regatta list: '+e.message); setTimeout(()=>setBusy(null),6000); return false; }
  S.regattas=j.regattas; S.templates=j.templates;
  fillRegSel(S.regatta);
  return true;
}
function fillRegSel(rid){
  const gs=$('#regSel'); gs.innerHTML='';
  S.regattas.forEach(g=>{ const o=document.createElement('option'); o.value=g.id; o.textContent=g.name; gs.appendChild(o); });
  gs.value=rid||''; gs.onchange=()=>loadIndex(gs.value);
}
async function loadRegattas(rid, raceId){
  if(!await fetchRegattas()){ if(!S.regattas.length) showEmpty(); return; }   // keep the current view + message
  let keep=null; try{ keep=localStorage.getItem('sa_regatta'); }catch(e){}
  rid=rid||S.regatta||keep;
  if(!S.regattas.some(g=>g.id===rid)) rid=S.regattas.length?S.regattas[0].id:null;
  fillRegSel(rid);
  if(!rid){ S.regatta=null; showEmpty(); if(typeof openManager==='function') openManager(); return; }
  await loadIndex(rid, raceId);
}
async function loadIndex(rid, raceId){
  const tok=S._ixTok=(S._ixTok||0)+1; S._rcTok=(S._rcTok||0)+1;   // newer selections win
  setBusy('Loading races… (the first open after an update analyses the tracks, ~10–30 s)');
  let idx;
  try{ idx=await api('/api/index/'+rid); if(!Array.isArray(idx)) throw new Error('unexpected reply'); }
  catch(e){ if(tok!==S._ixTok) return;
    setBusy('Could not load that regatta: '+e.message); setTimeout(()=>setBusy(null),5000);
    $('#regSel').value=S.regatta||''; return; }
  if(tok!==S._ixTok) return;
  let res=null; try{ res=(await api('/api/results/'+rid)).results; }catch(e){}
  if(tok!==S._ixTok) return;                       // a newer regatta selection took over
  setBusy(null);
  S.regatta=rid; $('#regSel').value=rid; try{ localStorage.setItem('sa_regatta',rid); }catch(e){}
  const g=S.regattas.find(x=>x.id===rid);
  $('#regTitle').textContent=g?g.name:''; document.title='Sail Analyzer — '+(g?g.name:'');
  S.index=idx; S.results=res;
  const rs=$('#raceSel'); rs.innerHTML='';
  S.index.forEach(r=>{ const o=document.createElement('option'); o.value=r.i;
    o.textContent=r.race+'  ·  wind '+r.wind_from+'°'; rs.appendChild(o); });
  rs.onchange=()=>loadRace(+rs.value);
  if(!S.index.length){ showEmpty(); return; }
  const pick=S.index.find(r=>r.id===raceId)||S.index[0];
  rs.value=pick.i; await loadRace(pick.i);
}
function showEmpty(){
  setPlaying(false); S.race=null; S.index=S.index||[]; S.byS={}; S.view=null;
  for(const sel of ['#raceSel','#focusSel']) $(sel).innerHTML='';
  $('#partnerSel').innerHTML='<option value="">— fleet only —</option>';
  $('#tEnd').textContent='00:00'; $('#tLabel').textContent='00:00'; $('#legend').innerHTML=''; $('#hover').innerHTML='';
  bgx.clearRect(0,0,bg.width,bg.height); ctx.clearRect(0,0,map.width,map.height);
  setTab(S.tab);
}
function setBusy(msg){ const el=$('#busy'); if(!el) return; el.textContent=msg||''; el.style.display=msg?'':'none'; }
async function loadRace(i){
  const tok=S._rcTok=(S._rcTok||0)+1;
  setPlaying(false);
  let race=null, why='';
  try{ race=await api('/api/race/'+S.regatta+'/'+S.index[i].id); }catch(e){ why=e.message; }
  if(tok!==S._rcTok) return;
  if(!race){ setBusy(`Could not load that race${why?': '+why:''} — reopen the regatta to refresh the list.`);
    setTimeout(()=>setBusy(null),5000);
    if(S.raceRid!==S.regatta){                 // shown race belongs to the previous regatta: drop it
      S.race=null; S.raceIdx=-1; S.byS={}; S.view=null; $('#raceSel').value='';
      renderBG(); render(); setTab(S.tab);
    } else { S.raceIdx=S.index.findIndex(x=>x.id===S.raceId); $('#raceSel').value=S.raceIdx>=0?S.raceIdx:''; }
    return; }
  S.raceIdx=i; S.race=race; S.raceRid=S.regatta; S.raceId=S.index[i].id;
  $('#raceSel').value=i;
  S._marks=null;                       // reload marks (auto or saved override) per race
  S.legFilter='all';                   // reset leg drill-down for the new race
  S.byS={}; S.race.boats.forEach(b=>S.byS[b.sail]=b);
  if(!S.byS[S.focus]) S.focus=S.race.boats[0].sail;
  if(S.partner && !S.byS[S.partner]) S.partner='';
  // selects
  const fillSel=(sel,blank)=>{ sel.innerHTML = blank?'<option value="">— fleet only —</option>':'';
    S.race.boats.forEach(b=>{ const o=document.createElement('option'); o.value=b.sail;
      o.textContent=b.sail+'  '+b.name; sel.appendChild(o); }); };
  fillSel($('#focusSel'),false); $('#focusSel').value=S.focus;
  fillSel($('#partnerSel'),true); $('#partnerSel').value=S.partner;
  // time bounds
  S.tMin=S.race.fleet_stats.t[0]; S.tMax=S.race.fleet_stats.t[S.race.fleet_stats.t.length-1]; S.t=S.tMin;
  $('#tEnd').textContent=fmtT(S.tMax-S.tMin);
  setWind(S.race.wind_from);
  updateMarkButtons();
  applyManualMarks();                  // derive legs from saved marks if any
  computeView(); renderBG(); render();
  drawLegend(); setTab(S.tab); syncTime();
}
function drawLegend(){
  const modes={tack:'<span><span class="dot" style="background:#3f9bff"></span>Stbd</span><span><span class="dot" style="background:#ff5e7a"></span>Port</span>',
    speed:'<span>slow</span><span class="dot" style="background:hsl(220,85%,55%)"></span>→<span class="dot" style="background:hsl(10,85%,55%)"></span><span>fast (vs own avg for that leg type)</span>',
    vmg:'<span><span class="dot" style="background:#50c8ff"></span>above</span><span><span class="dot" style="background:#ff7b54"></span>below own leg-type VMG avg</span>',
    fleet:'<span><span class="dot" style="background:#3fb950"></span>ahead of fleet VMG</span><span><span class="dot" style="background:#f85149"></span>behind</span>',
    air:'<span><span class="dot" style="background:#e1e6ee"></span>clean air</span><span><span class="dot" style="background:#be5aff"></span>dirty air (boat upwind ≤8 BL)</span>',
    cmp: S.partner?`<span><span class="dot" style="background:#3fb950"></span>ahead of ${S.byS[S.partner].name}</span><span><span class="dot" style="background:#f85149"></span>behind</span>`:'<span>pick a Compare-vs boat</span>'};
  $('#legend').innerHTML=`<span><span class="dot" style="background:#f5a623"></span>You</span>`+
    (S.partner?`<span><span class="dot" style="background:#3aa0ff"></span>${S.byS[S.partner].name}</span>`:'')+
    `<span><span class="dot" style="background:#ffd166"></span>Marks</span> ${modes[S.colorMode]}`;
}

// ---------- events ----------
$('#focusSel').onchange=e=>{ S.focus=e.target.value; S.legFilter='all'; renderBG(); render(); drawLegend(); setTab(S.tab); syncTime(); };
$('#partnerSel').onchange=e=>{ S.partner=e.target.value; renderBG(); render(); drawLegend(); setTab(S.tab); syncTime(); };
$('#tabs').onclick=e=>{ if(e.target.dataset.tab) setTab(e.target.dataset.tab); };
$('#playBtn').onclick=()=>{ if(S.race) setPlaying(!S.playing); };
$('#timeRange').oninput=e=>{ if(!S.race) return; S.t=S.tMin+(e.target.value/1000)*(S.tMax-S.tMin); if(S.playing)setPlaying(false); syncTime(); };
$('#fitBtn').onclick=()=>{ if(!S.race) return; computeView(); renderBG(); render(); };
$('#fleetBtn').onclick=e=>{ S.showFleet=!S.showFleet; e.target.classList.toggle('on',S.showFleet); render(); };
$('#idealBtn').onclick=e=>{ S.showIdeal=!S.showIdeal; e.target.classList.toggle('on',S.showIdeal); renderBG(); render(); };
$('#tacksBtn').onclick=e=>{ S.showTacks=!S.showTacks; e.target.classList.toggle('on',S.showTacks); renderBG(); render(); };
document.querySelectorAll('#maptools [data-mode]').forEach(btn=>btn.onclick=()=>{
  S.colorMode=btn.dataset.mode; document.querySelectorAll('#maptools [data-mode]').forEach(b=>b.classList.toggle('on',b===btn));
  renderBG(); render(); drawLegend(); });
$('#editMarksBtn').onclick=e=>{ S.editMarks=!S.editMarks; e.target.classList.toggle('on',S.editMarks);
  e.target.textContent=S.editMarks?'Done editing':'Edit marks';
  map.style.cursor=S.editMarks?'pointer':'grab'; updateMarkButtons(); renderBG(); render(); };
$('#saveMarksBtn').onclick=saveSnapshot;
$('#resetOrigBtn').onclick=resetOriginal;
$('#resetSavedBtn').onclick=resetSaved;
// ---------- map zoom/pan + mark editing ----------
// Not editing: drag = pan, wheel/double-click = zoom (centred on cursor).
// Editing marks: drag = move mark, click empty = add, double-click = delete.
let _dragMark=null, _pan=null;
function zoomAt(px,py,f){ const v=S.view; if(!v) return; const z0=v.zoom, z1=Math.max(1,Math.min(40,z0*f));
  v.panX=px-(px-v.panX)*z1/z0; v.panY=py-(py-v.panY)*z1/z0; v.zoom=z1; renderBG(); render(); }
map.onmousedown=e=>{ if(!S.view) return; const px=e.offsetX*DPR, py=e.offsetY*DPR;
  if(S.editMarks){
    const n=nearestMark(px,py);
    if(n.i>=0 && n.d<18*DPR){ _dragMark=n.i; renderBG(); render(); return; }   // drag an existing mark
    // empty space in edit mode: pan (do NOT create a new mark)
  }
  _pan={mx:px,my:py,panX:S.view.panX,panY:S.view.panY}; map.style.cursor='grabbing'; };
map.onmousemove=e=>{ const px=e.offsetX*DPR, py=e.offsetY*DPR;
  if(_dragMark!=null){ effMarks()[_dragMark].ll=unproj(px,py); renderBG(); render(); }
  else if(_pan){ S.view.panX=_pan.panX+(px-_pan.mx); S.view.panY=_pan.panY+(py-_pan.my); renderBG(); render(); } };
addEventListener('mouseup',()=>{
  if(_dragMark!=null){ _dragMark=null; saveMarks(); afterMarksChanged(); }
  if(_pan){ _pan=null; map.style.cursor=S.editMarks?'pointer':'grab'; } });
map.onwheel=e=>{ e.preventDefault(); if(!S.view) return; zoomAt(e.offsetX*DPR, e.offsetY*DPR, e.deltaY<0?1.15:1/1.15); };
map.ondblclick=e=>{ if(!S.view) return; const px=e.offsetX*DPR, py=e.offsetY*DPR;
  if(S.editMarks){ const n=nearestMark(px,py);
    if(n.i>=0 && n.d<18*DPR){ effMarks().splice(n.i,1); saveMarks(); afterMarksChanged(); } return; }
  zoomAt(px,py,1.6); };
addEventListener('resize',()=>{ if(S.race){ layout(); renderBG(); render(); S.charts.forEach(c=>c.draw()); } });
addEventListener('keydown',e=>{ const tg=e.target;
  if(!S.race || $('#mgModal').style.display!=='none' || tg.tagName==='TEXTAREA' || tg.tagName==='SELECT' ||
     (tg.tagName==='INPUT' && !/^(range|checkbox|button)$/.test(tg.type))) return;
  if(e.code==='Space'){e.preventDefault();setPlaying(!S.playing);}
  if(e.code==='ArrowRight'){S.t=Math.min(S.tMax,S.t+5);syncTime();} if(e.code==='ArrowLeft'){S.t=Math.max(S.tMin,S.t-5);syncTime();} });

loadRegattas();
