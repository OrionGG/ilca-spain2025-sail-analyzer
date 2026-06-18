'use strict';
// ILCA Sail Analyzer — offline client. Vanilla JS, canvas rendering.

const S = {
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
function marksKey(){ return 'sa_marks_'+(S.race?S.race.race:''); }       // working
function savedKey(){ return 'sa_marksaved_'+(S.race?S.race.race:''); }   // saved snapshot
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

// Course rounding order: start -U-> M1 -R-> M2 -D-> gate -U-> M2 -D-> gate -R-> finish.
// For a boat, each leg ends at the closest approach to its target mark; if a
// later mark is never reached (shortened race / retirement) we stop there.
function legsFromMarks(b){
  const M1=markBy('1')[0], M2=markBy('2')[0], G=markBy('G'), F=markBy('F')[0];
  if(!M1||!M2||!G.length) return null;
  const seq=[ {ll:[M1],k:'upwind'}, {ll:[M2],k:'reach'}, {ll:G,k:'downwind'},
              {ll:[M2],k:'upwind'}, {ll:G,k:'downwind'}, {ll:(F?[F]:G),k:'reach'} ];
  const n=b.lat.length, R=70;
  const md=(i,lls)=>Math.min(...lls.map(ll=>distM(b.lat[i],b.lon[i],ll[0],ll[1])));
  const g0 = S.race.gun_t ? idxAt(b,S.race.gun_t) : 0;   // leg 1 starts at the gun
  const legs=[]; let start=g0, prev=g0;
  for(const s of seq){
    let i=prev+1; while(i<n && md(i,s.ll)>R) i++;        // enter the mark's zone
    if(i>=n) break;
    let bi=i, bd=md(i,s.ll);
    while(i<n && md(i,s.ll)<=R*1.6){ const d=md(i,s.ll); if(d<bd){bd=d;bi=i;} i++; }  // closest pt in pass
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
  const on=marksOverridden();
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
  const nl=boatLegs(focusB()).length, best=[];
  for(let i=0;i<nl;i++){ let bt=Infinity,bb=null;
    for(const o of S.race.boats){ const lg=boatLegs(o); if(i>=lg.length) continue;
      const [a,c]=lg[i], tm=o.t[c]-o.t[a]; if(tm>0 && tm<bt){ bt=tm; bb={sail:o.sail,a,c,t:tm}; } }
    best.push(bb); }
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
  const r=S.race; bgx.clearRect(0,0,bg.width,bg.height);
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
  // ideal route = fastest demonstrated track per leg (real paths, stitched)
  if(S.showIdeal){ const wps=idealRoute();
    if(wps.length>1){ bgx.strokeStyle='rgba(90,210,255,.9)'; bgx.lineWidth=2.4*DPR; bgx.setLineDash([3*DPR,4*DPR]);
      bgx.beginPath(); wps.forEach((p,i)=>{ const [x,y]=proj(p[0],p[1]); i?bgx.lineTo(x,y):bgx.moveTo(x,y); }); bgx.stroke(); bgx.setLineDash([]); } }
  // course marks (1 = windward, 2 = wing/reach, G = gate, F = finish, RC/Pin = start)
  for(const m of effMarks()) drawMark(m.ll, m.label);
  // partner full track
  if(S.partner && S.byS[S.partner]) drawTrackColored(S.byS[S.partner], 'var(--partner)', 'rgba(58,160,255,.9)');
  // focus full track (colored by mode)
  if(S.byS[S.focus]) drawTrackColored(S.byS[S.focus], null, null);
}
function drawTrackPlain(b){
  bgx.beginPath();
  for(let k=0;k<b.lat.length;k++){ const [x,y]=proj(b.lat[k],b.lon[k]); k?bgx.lineTo(x,y):bgx.moveTo(x,y); }
  bgx.stroke();
}
function drawTrackColored(b, solid, solidStroke){
  // partner: solid color; focus: per-segment by colorMode
  const isFocus = (solid===null);
  bgx.lineWidth=(isFocus?2.6:2.0)*DPR; bgx.lineJoin='round';
  if(!isFocus){ bgx.strokeStyle=solidStroke; drawTrackPlain(b); return; }
  if(S.colorMode==='fleet'){ const rf=fleetRef(); b._gain=computeGain(b,rf); }
  else if(S.colorMode==='cmp'){ const rf=cmpRef(); b._gain=rf?computeGain(b,rf):null; }   // null = no partner
  for(let k=1;k<b.lat.length;k++){
    const [x0,y0]=proj(b.lat[k-1],b.lon[k-1]), [x1,y1]=proj(b.lat[k],b.lon[k]);
    bgx.strokeStyle=segColor(b,k);
    bgx.beginPath(); bgx.moveTo(x0,y0); bgx.lineTo(x1,y1); bgx.stroke();
  }
}
// Per-point gain vs a reference (fleet or partner) for the current leg type:
// upwind/downwind use VMG-to-mark (downwind better = more negative VMG), reaches use SOG. >0 = ahead.
function computeGain(b, ref){    // ref(t) -> {vmg,sog} | null
  const kind=new Array(b.lat.length).fill('upwind');
  for(const [a,c,k] of boatLegs(b)) for(let j=a;j<=c;j++) kind[j]=k;
  const gain=new Array(b.lat.length).fill(null);
  for(let k=0;k<b.lat.length;k++){ const rf=ref(b.t[k]); if(!rf) continue;
    if(kind[k]==='downwind') gain[k]=rf.vmg-b.vmg[k];
    else if(kind[k]==='reach') gain[k]=b.sog[k]-rf.sog;
    else gain[k]=b.vmg[k]-rf.vmg; }
  return gain;
}
function fleetRef(){ const fs=S.race.fleet_stats, G=fs.t;
  const it=(arr,t)=>{ if(t<=G[0])return arr[0]; if(t>=G[G.length-1])return arr[G.length-1];
    let i=0; while(i<G.length-1&&G[i+1]<t)i++; const a=arr[i],c=arr[i+1];
    if(a==null||c==null)return a==null?c:a; return a+(c-a)*(t-G[i])/(G[i+1]-G[i]); };
  return t=>({vmg:it(fs.vmg_p50,t), sog:it(fs.sog_p50,t)}); }
function cmpRef(){ const p=partnerB(); if(!p) return null;
  return t=>{ const s=sampleAt(p,t); return s.inrace?{vmg:s.vmg,sog:s.sog}:null; }; }
function segColor(b,k){
  if(S.colorMode==='tack') return b.tack[k]>0 ? '#3f9bff' : '#ff5e7a';   // stbd blue / port red
  if(S.colorMode==='speed'){ const f=Math.max(0,Math.min(1,(b.sog[k]-2)/5)); return `hsl(${(1-f)*220+10},85%,55%)`; }
  if(S.colorMode==='fleet'||S.colorMode==='cmp'){             // green=ahead, red=behind reference
    if(!b._gain) return 'rgba(150,160,175,.55)';              // no comparison (e.g. no partner)
    const g=b._gain[k]; if(g==null) return 'rgba(150,160,175,.5)';
    const m=0.4+0.55*Math.min(1,Math.abs(g)/1.2);
    return g>=0?`rgba(63,185,80,${m})`:`rgba(248,81,73,${m})`; }
  // vmg
  const f=Math.max(-1,Math.min(1,b.vmg[k]/4));
  return f>=0?`rgba(80,200,255,${.3+.6*f})`:`rgba(255,120,84,${.3+.6*-f})`;
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

// ---------- hover readout ----------
function updateHover(){
  const f=S.byS[S.focus]; if(!f) return;
  const s=sampleAt(f,S.t);
  let html=`<div class="h-name">${f.name}</div>`;
  if(s.inrace){
    html+=`SOG <b>${s.sog.toFixed(2)}</b> kt<br>TWA ${s.twa.toFixed(0)}° (${s.tack>0?'Stbd':'Port'})<br>`+
          `VMG ${s.vmg.toFixed(2)} kt<br>COG ${s.cog.toFixed(0)}°`;
    if(S.partner&&S.byS[S.partner]){
      const ps=sampleAt(S.byS[S.partner],S.t);
      if(ps.inrace){
        // row: real partner value + delta vs focus
        const row=(lbl,val,d,u,dp=2,good=null)=>{ const sign=d>=0?'+':''; const cls=good==null?'':(good?'good':'bad');
          return `${lbl} ${val.toFixed(dp)}${u} <b class="${cls}">(${sign}${d.toFixed(dp)})</b><br>`; };
        const dCog=((s.cog-ps.cog+540)%360)-180;          // signed heading diff
        html+=`<hr style="border-color:#2b333f;margin:5px 0">`+
          `<span style="color:#3aa0ff">${S.byS[S.partner].name}</span><br>`+
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
    const gun=S.race&&S.race.gun_t;
    // gridlines + axis labels (drawn first, unclipped so labels sit in the margins)
    c.strokeStyle='#222a35'; c.fillStyle='#8b949e'; c.font='10px sans-serif'; c.lineWidth=1;
    for(let g=0;g<=4;g++){ const v=ymin+(ymax-ymin)*g/4, y=Y(v); c.beginPath();c.moveTo(L,y);c.lineTo(w-R,y);c.stroke(); c.fillText((opts.fmtY?opts.fmtY(v):v.toFixed(1)),2,y+3); }
    for(let g=0;g<=5;g++){ const t=xmin+(xmax-xmin)*g/5; c.fillText(fmtT(t-S.tMin),X(t)-12,h-6); }
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
  ({overview:tabOverview,speed:tabSpeed,vmg:tabVMG,wind:tabWind,maneuvers:tabManeuvers,start:tabStart,position:tabPosition}[name])(body);
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
    style="border-left:3px solid ${COL[lg[2]]}" title="${lg[2]}">${i+1}</span>`; });
  bar.innerHTML=h;
  bar.querySelectorAll('.lp').forEach(p=>p.onclick=()=>{
    S.legFilter = p.dataset.leg==='all' ? 'all' : +p.dataset.leg; setTab(S.tab); });
}

function fleetMedians(){
  const keys=['sog_mean','sog_up','sog_dn','twa_up','twa_dn','vmg_up','vmg_dn','n_tacks','n_gybes','tack_loss','gybe_loss','dist_nm'];
  const o={}; for(const k of keys) o[k]=med(S.race.boats.map(b=>b.summary[k]));
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

function tabOverview(body){
  const f=focusB(), p=partnerB(), s=f.summary, fm=fleetMedians();
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
    legs:legSpans(f),fmtY:v=>v.toFixed(1)});
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
    t+=`<tr class="man" data-leg="${i}"${sel}><td>${i+1}</td><td>${k}</td><td>${(ss/n).toFixed(2)} kt</td><td>${(Math.abs(vv/n)).toFixed(2)} kt</td></tr>`; });
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
  S.charts.push(makeChart(body,{h:200,xmin:xa,xmax:xb,series,legs:legSpans(f),fmtY:v=>v.toFixed(1)}));
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
  else { const all=S.race.boats.map(liftedPct).filter(v=>v!=null); cmpLp=all.length?all.reduce((a,b)=>a+b,0)/all.length:null; cmpName='Fleet average'; }
  const bar=(name,v,col)=>v==null?`<div class="muted">${name}: no upwind data for this selection.</div>`:
    `<div class="barwrap"><span class="lbl" style="width:110px">${name}</span>
      <div class="bar" style="width:${Math.round(v)}%;background:${col}"></div><span>${Math.round(v)}%</span></div>`;
  body.insertAdjacentHTML('beforeend', bar('You (lifted)',flp,'var(--good)') + bar(cmpName,cmpLp,'#3aa0ff'));
  const dlt=(flp!=null&&cmpLp!=null)?(flp-cmpLp):null;
  body.insertAdjacentHTML('beforeend',`<div class="muted">% of upwind time on the favoured (lifted) tack relative to the
    race-mean wind. ${dlt!=null?`You were <b class="${dlt>=0?'good':'bad'}">${dlt>=0?'+':''}${dlt.toFixed(0)} pts</b> vs ${cmpName.toLowerCase()}. `:''}
    Higher = you sailed the headers and tacked onto the lifts.${typeof S.legFilter==='number'?' (this upwind leg only)':''}</div>`);
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
  const fleetD=r.boats.map(bb=>startMetrics(bb).dist), fleetS=r.boats.map(bb=>startMetrics(bb).sog);
  const grid=document.createElement('div'); grid.className='cards'; body.appendChild(grid);
  card(grid,'Dist behind line @ gun',Math.abs(Math.min(0,fm.dist)).toFixed(0),'m',null);
  card(grid,'Speed @ gun',fm.sog.toFixed(2),'kt',fm.sog-med(fleetS));
  // rank of distance (closest to line / least behind = best, among those behind)
  const behind=fleetD.map((d,i)=>({d,i})).sort((p,q)=>q.d-p.d);
  const myRank=behind.findIndex(o=>o.i===r.boats.indexOf(f))+1;
  card(grid,'Line position',myRank+' / '+r.boats.length,'',null);
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
  // rank each grid index by course progress (uses mark-derived legs if edited)
  const C={}; for(const o of r.boats) C[o.sail]=boatCourse(o);
  const rankOf=(boat)=>{ const bc=C[boat.sail], out=[]; for(let k=0;k<G.length;k++){ const my=bc[k]; if(my==null){out.push([G[k],null]);continue;}
      let rank=1; for(const o of r.boats){ const v=C[o.sail][k]; if(v!=null && v>my) rank++; } out.push([G[k],rank]); } return out; };
  const series=[{color:'#f5a623',w:2,data:rankOf(f)}];
  if(p) series.push({color:'#3aa0ff',w:1.7,data:rankOf(p)});
  const [xa,xb]=legWindow();
  S.charts.push(makeChart(body,{h:220,xmin:xa,xmax:xb,ymin:r.boats.length+1,ymax:0,
    series,legs:legSpans(f),fmtY:v=>v.toFixed(0)}));
  body.insertAdjacentHTML('beforeend',`<div class="muted" style="margin-top:8px">Position is estimated from cumulative
   distance made good along the course (made-good distance to each mark). Line going <b>up</b> = gaining places, <b>down</b>
   = losing. This is a GPS proxy for race rank — penalties, OCS and protests aren't reflected.
   Background = <b style="color:#6ea3ff">upwind</b> / <b style="color:#6edc8c">reach</b> / <b style="color:#ff7b54">downwind</b> legs.</div>`);
  // gains/losses summary
  const rk=rankOf(f).filter(x=>x[1]!=null);
  if(rk.length){ body.insertAdjacentHTML('beforeend',`<div class="sectitle">Summary</div>
    <div class="muted">First clear position: <b>${rk[0][1]}</b> · Best: <b class="good">${Math.min(...rk.map(x=>x[1]))}</b>
    · Worst: <b class="bad">${Math.max(...rk.map(x=>x[1]))}</b> · Final: <b>${rk[rk.length-1][1]}</b> of ${r.boats.length}.</div>`); }

  // closest to the ideal route = least time vs the fastest-demonstrated-per-leg total
  const it=idealTime(), nl=boatLegs(f).length;
  if(it>0){
    const eff=r.boats.map(b=>({sail:b.sail,name:b.name,rt:raceTime(b),n:boatLegs(b).length}))
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

// ---------- wind arrow ----------
function setWind(deg){ $('#windVal').textContent=deg.toFixed(0)+'°';
  $('#windG').setAttribute('transform',`rotate(${(deg+180)%360} 13 13)`); }  // arrow points the way wind blows TO

// ---------- load ----------
async function loadIndex(){
  S.index=await (await fetch('/api/index')).json();
  const rs=$('#raceSel'); rs.innerHTML='';
  S.index.forEach(r=>{ const o=document.createElement('option'); o.value=r.i;
    o.textContent=r.race+'  ·  wind '+r.wind_from+'°'; rs.appendChild(o); });
  rs.onchange=()=>loadRace(+rs.value);
  await loadRace(S.index[0].i);
}
async function loadRace(i){
  S.raceIdx=i; setPlaying(false);
  S.race=await (await fetch('/api/race/'+i)).json();
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
    speed:'<span>slow</span><span class="dot" style="background:hsl(220,85%,55%)"></span>→<span class="dot" style="background:hsl(10,85%,55%)"></span><span>fast</span>',
    vmg:'<span><span class="dot" style="background:#50c8ff"></span>gaining</span><span><span class="dot" style="background:#ff7b54"></span>losing</span>',
    fleet:'<span><span class="dot" style="background:#3fb950"></span>ahead of fleet VMG</span><span><span class="dot" style="background:#f85149"></span>behind</span>',
    cmp: S.partner?`<span><span class="dot" style="background:#3fb950"></span>ahead of ${S.byS[S.partner].name}</span><span><span class="dot" style="background:#f85149"></span>behind</span>`:'<span>pick a Compare-vs boat</span>'};
  $('#legend').innerHTML=`<span><span class="dot" style="background:#f5a623"></span>You</span>`+
    (S.partner?`<span><span class="dot" style="background:#3aa0ff"></span>${S.byS[S.partner].name}</span>`:'')+
    `<span><span class="dot" style="background:#ffd166"></span>Marks</span> ${modes[S.colorMode]}`;
}

// ---------- events ----------
$('#focusSel').onchange=e=>{ S.focus=e.target.value; S.legFilter='all'; renderBG(); render(); drawLegend(); setTab(S.tab); syncTime(); };
$('#partnerSel').onchange=e=>{ S.partner=e.target.value; renderBG(); render(); drawLegend(); setTab(S.tab); syncTime(); };
$('#tabs').onclick=e=>{ if(e.target.dataset.tab) setTab(e.target.dataset.tab); };
$('#playBtn').onclick=()=>setPlaying(!S.playing);
$('#timeRange').oninput=e=>{ S.t=S.tMin+(e.target.value/1000)*(S.tMax-S.tMin); if(S.playing)setPlaying(false); syncTime(); };
$('#fitBtn').onclick=()=>{ computeView(); renderBG(); render(); };
$('#fleetBtn').onclick=e=>{ S.showFleet=!S.showFleet; e.target.classList.toggle('on',S.showFleet); render(); };
$('#idealBtn').onclick=e=>{ S.showIdeal=!S.showIdeal; e.target.classList.toggle('on',S.showIdeal); renderBG(); render(); };
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
map.onmousedown=e=>{ const px=e.offsetX*DPR, py=e.offsetY*DPR;
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
map.onwheel=e=>{ e.preventDefault(); zoomAt(e.offsetX*DPR, e.offsetY*DPR, e.deltaY<0?1.15:1/1.15); };
map.ondblclick=e=>{ const px=e.offsetX*DPR, py=e.offsetY*DPR;
  if(S.editMarks){ const n=nearestMark(px,py);
    if(n.i>=0 && n.d<18*DPR){ effMarks().splice(n.i,1); saveMarks(); afterMarksChanged(); } return; }
  zoomAt(px,py,1.6); };
addEventListener('resize',()=>{ if(S.race){ layout(); renderBG(); render(); S.charts.forEach(c=>c.draw()); } });
addEventListener('keydown',e=>{ if(e.code==='Space'){e.preventDefault();setPlaying(!S.playing);}
  if(e.code==='ArrowRight'){S.t=Math.min(S.tMax,S.t+5);syncTime();} if(e.code==='ArrowLeft'){S.t=Math.max(S.tMin,S.t-5);syncTime();} });

loadIndex();
