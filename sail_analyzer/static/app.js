'use strict';
// ILCA Sail Analyzer — offline client. Vanilla JS, canvas rendering.

const S = {
  index: [], race: null, raceIdx: 0,
  focus: '206341', partner: '',
  tMin: 0, tMax: 1, t: 0,
  playing: false, colorMode: 'tack', showFleet: true,
  view: null, tab: 'overview', charts: [],
  byS: {},                       // sail -> boat
};
const KN = 1.94384, ILCA = 4.2;
const $ = s => document.querySelector(s);
const map = $('#map'), ctx = map.getContext('2d');
let bg = document.createElement('canvas'), bgx = bg.getContext('2d');
let DPR = Math.min(devicePixelRatio || 1, 2);

// ---------- utils ----------
const fmtT = s => { s = Math.max(0, s|0); return String(s/60|0).padStart(2,'0')+':'+String(s%60).padStart(2,'0'); };
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
function computeView(){
  const r=S.race; let mnLa=1e9,mxLa=-1e9,mnLo=1e9,mxLo=-1e9;
  for(const b of r.boats){
    for(let k=0;k<b.lat.length;k+=4){
      if(b.lat[k]<mnLa)mnLa=b.lat[k]; if(b.lat[k]>mxLa)mxLa=b.lat[k];
      if(b.lon[k]<mnLo)mnLo=b.lon[k]; if(b.lon[k]>mxLo)mxLo=b.lon[k];
    }
  }
  S.view={mnLa,mxLa,mnLo,mxLo,lat0:r.lat0,cosL:Math.cos(r.lat0*Math.PI/180)};
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
  return [px,py];
}

// ---------- background layer (static tracks/marks) ----------
function renderBG(){
  const r=S.race; bgx.clearRect(0,0,bg.width,bg.height);
  // faint fleet tracks
  bgx.lineWidth=1*DPR; bgx.strokeStyle='rgba(120,135,155,.18)';
  for(const b of r.boats){
    if(b.sail===S.focus||b.sail===S.partner) continue;
    drawTrackPlain(b);
  }
  // course marks
  drawMark(r.marks.windward,'W'); drawMark(r.marks.leeward,'L');
  if(r.start_line){
    const [a,b]=r.start_line, p1=proj(a[0],a[1]), p2=proj(b[0],b[1]);
    bgx.strokeStyle='rgba(245,166,35,.7)'; bgx.lineWidth=2*DPR;
    bgx.setLineDash([6*DPR,5*DPR]); bgx.beginPath();
    bgx.moveTo(p1[0],p1[1]); bgx.lineTo(p2[0],p2[1]); bgx.stroke(); bgx.setLineDash([]);
  }
  // partner full track
  if(S.partner && S.byS[S.partner]) drawTrackColored(S.byS[S.partner], 'var(--partner)', 'rgba(38,208,124,.85)');
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
  for(let k=1;k<b.lat.length;k++){
    const [x0,y0]=proj(b.lat[k-1],b.lon[k-1]), [x1,y1]=proj(b.lat[k],b.lon[k]);
    bgx.strokeStyle=segColor(b,k);
    bgx.beginPath(); bgx.moveTo(x0,y0); bgx.lineTo(x1,y1); bgx.stroke();
  }
}
function segColor(b,k){
  if(S.colorMode==='tack') return b.tack[k]>0 ? '#3f9bff' : '#ff5e7a';   // stbd blue / port red
  if(S.colorMode==='speed'){ const f=Math.max(0,Math.min(1,(b.sog[k]-2)/5)); return `hsl(${(1-f)*220+10},85%,55%)`; }
  // vmg
  const f=Math.max(-1,Math.min(1,b.vmg[k]/4));
  return f>=0?`rgba(80,200,255,${.3+.6*f})`:`rgba(255,120,84,${.3+.6*-f})`;
}
function drawMark(ll,label){
  if(!ll) return; const [x,y]=proj(ll[0],ll[1]);
  bgx.fillStyle='#ffd166'; bgx.beginPath(); bgx.arc(x,y,5*DPR,0,7); bgx.fill();
  bgx.fillStyle='#ffd166'; bgx.font=`${11*DPR}px sans-serif`; bgx.fillText(label,x+8*DPR,y-6*DPR);
}

// ---------- dynamic layer (boat dots at current time) ----------
function render(){
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
  if(S.partner&&S.byS[S.partner]) dot(S.byS[S.partner],'#26d07c',5.5);
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
      if(ps.inrace) html+=`<hr style="border-color:#2b333f;margin:5px 0">`+
        `<span style="color:#26d07c">${S.byS[S.partner].name}</span><br>ΔSOG <b>${(s.sog-ps.sog>=0?'+':'')}${(s.sog-ps.sog).toFixed(2)}</b> kt`;
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
    // leg shading
    if(opts.legs) for(const lg of opts.legs){ c.fillStyle = lg.up?'rgba(74,163,255,.07)':'rgba(255,123,84,.07)'; c.fillRect(X(lg.a),T,X(lg.b)-X(lg.a),ph); }
    // gridlines + y labels
    c.strokeStyle='#222a35'; c.fillStyle='#8b949e'; c.font='10px sans-serif'; c.lineWidth=1;
    for(let g=0;g<=4;g++){ const v=ymin+(ymax-ymin)*g/4, y=Y(v); c.beginPath();c.moveTo(L,y);c.lineTo(w-R,y);c.stroke(); c.fillText((opts.fmtY?opts.fmtY(v):v.toFixed(1)),2,y+3); }
    for(let g=0;g<=5;g++){ const t=xmin+(xmax-xmin)*g/5; c.fillText(fmtT(t-S.tMin),X(t)-12,h-6); }
    // zero line
    if(ymin<0&&ymax>0){ c.strokeStyle='#3a4452'; c.beginPath();c.moveTo(L,Y(0));c.lineTo(w-R,Y(0));c.stroke(); }
    // band
    if(opts.band){ c.fillStyle=opts.band.color||'rgba(120,135,155,.18)'; c.beginPath();
      const lo=opts.band.lo,hi=opts.band.hi; let started=false;
      for(let k=0;k<lo.length;k++){ if(hi[k][1]==null)continue; const x=X(hi[k][0]),y=Y(hi[k][1]); started?c.lineTo(x,y):c.moveTo(x,y); started=true; }
      for(let k=lo.length-1;k>=0;k--){ if(lo[k][1]==null)continue; c.lineTo(X(lo[k][0]),Y(lo[k][1])); }
      c.closePath(); c.fill();
    }
    // lines
    for(const s of opts.series){ c.strokeStyle=s.color; c.lineWidth=s.w||1.6; c.beginPath(); let st=false;
      for(const p of s.data){ if(p[1]==null){st=false;continue;} const x=X(p[0]),y=Y(p[1]); st?c.lineTo(x,y):c.moveTo(x,y); st=true; } c.stroke(); }
    // markers (e.g. maneuvers)
    if(opts.marks) for(const m of opts.marks){ c.fillStyle=m.color; c.beginPath(); c.arc(X(m.t),Y(m.y),3,0,7); c.fill(); }
    // cursor
    c.strokeStyle='rgba(245,166,35,.9)'; c.lineWidth=1; c.beginPath(); c.moveTo(X(S.t),T); c.lineTo(X(S.t),T+ph); c.stroke();
    chart._X=X; chart._Y=Y;
  }
  const chart={draw, cursor:draw, el:cv};
  return chart;
}

// ---------- tabs ----------
function setTab(name){
  S.tab=name; S.charts=[];
  document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.dataset.tab===name));
  const body=$('#tabbody'); body.innerHTML='';
  ({overview:tabOverview,speed:tabSpeed,vmg:tabVMG,wind:tabWind,maneuvers:tabManeuvers,start:tabStart,position:tabPosition}[name])(body);
  S.charts.forEach(c=>c.draw());
}
function focusB(){ return S.byS[S.focus]; }
function partnerB(){ return S.partner?S.byS[S.partner]:null; }
function legSpans(b){ return b.legs.map(([a,c,k])=>({a:b.t[a],b:b.t[c],up:k==='upwind'})); }

function fleetMedians(){
  const keys=['sog_mean','sog_up','sog_dn','twa_up','twa_dn','vmg_up','vmg_dn','n_tacks','n_gybes','tack_loss','gybe_loss','dist_nm'];
  const o={}; for(const k of keys) o[k]=med(S.race.boats.map(b=>b.summary[k]));
  return o;
}

function card(parent,k,v,unit,delta,deltaGood){
  const d=document.createElement('div'); d.className='card';
  let dh='';
  if(delta!=null && isFinite(delta)){ const good=deltaGood?deltaGood(delta):delta>=0;
    dh=`<div class="d ${good?'pos':'neg'}">${delta>=0?'▲':'▼'} ${Math.abs(delta).toFixed(2)} vs fleet</div>`; }
  d.innerHTML=`<div class="k">${k}</div><div class="v">${v==null?'–':v}${unit?`<span style="font-size:13px;color:var(--mut)"> ${unit}</span>`:''}</div>${dh}`;
  parent.appendChild(d);
}

function tabOverview(body){
  const f=focusB(), p=partnerB(), s=f.summary, fm=fleetMedians();
  const grid=document.createElement('div'); grid.className='cards'; body.appendChild(grid);
  card(grid,'Avg speed',s.sog_mean,'kt',s.sog_mean-fm.sog_mean);
  card(grid,'Upwind speed',s.sog_up,'kt',s.sog_up-fm.sog_up);
  card(grid,'Downwind speed',s.sog_dn,'kt',s.sog_dn-fm.sog_dn);
  card(grid,'Upwind VMG',s.vmg_up,'kt',s.vmg_up-fm.vmg_up);
  card(grid,'Downwind VMG',s.vmg_dn,'kt',s.vmg_dn-fm.vmg_dn);
  card(grid,'Upwind angle',s.twa_up,'°',s.twa_up-fm.twa_up,d=>d<0); // lower TWA = points higher = good
  card(grid,'Tacks',s.n_tacks,'',null);
  card(grid,'Avg tack loss',s.tack_loss,'BL',s.tack_loss-fm.tack_loss,d=>d<0);
  card(grid,'Gybes',s.n_gybes,'',null);
  card(grid,'Avg gybe loss',s.gybe_loss,'BL',s.gybe_loss-fm.gybe_loss,d=>d<0);
  card(grid,'Distance',s.dist_nm,'NM',null);

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
      ['Upwind angle','twa_up','°',-1],['Avg tack loss','tack_loss','BL',-1]];
    let t='<table><tr><th>Metric</th><th style="color:var(--focus)">'+f.name.slice(0,10)+'</th><th style="color:var(--partner)">'+p.name.slice(0,10)+'</th><th>Δ</th></tr>';
    for(const [lbl,key,u,dir] of rows){ const a=s[key],b=ps[key]; const d=(a!=null&&b!=null)?a-b:null;
      const good=d!=null && (dir>0?d>=0:d<=0);
      t+=`<tr><td>${lbl}</td><td>${a??'–'} ${u}</td><td>${b??'–'} ${u}</td><td><b class="${good?'good':'bad'}">${d==null?'–':(d>=0?'+':'')+d.toFixed(2)}</b></td></tr>`; }
    t+='</table>'; body.insertAdjacentHTML('beforeend',t);
  }
}

function seriesFromBoat(b, key){ return b.t.map((t,k)=>[t, b[key][k]]); }

function tabSpeed(body){
  const f=focusB(), p=partnerB(), fs=S.race.fleet_stats;
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Speed over ground — you vs fleet vs partner</div>');
  const series=[{color:'rgba(139,148,158,.8)',w:1.3,data:fs.t.map((t,k)=>[t,fs.sog_p50[k]])},
                {color:'#f5a623',w:2,data:seriesFromBoat(f,'sog')}];
  if(p) series.push({color:'#26d07c',w:1.7,data:seriesFromBoat(p,'sog')});
  const ch=makeChart(body,{h:200,xmin:S.tMin,xmax:S.tMax,series,
    band:{lo:fs.t.map((t,k)=>[t,fs.sog_p25[k]]),hi:fs.t.map((t,k)=>[t,fs.sog_p75[k]])},
    legs:legSpans(f),fmtY:v=>v.toFixed(1)});
  S.charts.push(ch);
  body.insertAdjacentHTML('beforeend',`<div class="muted" style="margin-top:8px">
    <span class="dot" style="background:#f5a623"></span> you &nbsp;
    ${p?'<span class="dot" style="background:#26d07c"></span> '+p.name+' &nbsp;':''}
    <span class="dot" style="background:#8b949e"></span> fleet median &nbsp;
    shaded band = fleet 25–75th percentile. Blue/orange backgrounds = upwind/downwind legs.</div>`);
  // per-leg breakdown
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Per-leg average speed</div>');
  let t='<table><tr><th>Leg</th><th>Type</th><th>Avg SOG</th><th>Avg VMG</th></tr>';
  f.legs.forEach(([a,c,k],i)=>{ let ss=0,vv=0,n=0; for(let j=a;j<=c;j++){ss+=f.sog[j];vv+=f.vmg[j];n++;}
    t+=`<tr><td>${i+1}</td><td>${k}</td><td>${(ss/n).toFixed(2)} kt</td><td>${(Math.abs(vv/n)).toFixed(2)} kt</td></tr>`; });
  t+='</table>'; body.insertAdjacentHTML('beforeend',t);
}

function tabVMG(body){
  const f=focusB(), p=partnerB(), fs=S.race.fleet_stats;
  body.insertAdjacentHTML('beforeend','<div class="sectitle">VMG to windward (+ up / − down)</div>');
  const series=[{color:'rgba(139,148,158,.8)',w:1.3,data:fs.t.map((t,k)=>[t,fs.vmg_p50[k]])},
                {color:'#f5a623',w:2,data:seriesFromBoat(f,'vmg')}];
  if(p) series.push({color:'#26d07c',w:1.7,data:seriesFromBoat(p,'vmg')});
  S.charts.push(makeChart(body,{h:200,xmin:S.tMin,xmax:S.tMax,series,legs:legSpans(f),fmtY:v=>v.toFixed(1)}));
  body.insertAdjacentHTML('beforeend','<div class="sectitle">TWA (angle to the wind) over time</div>');
  S.charts.push(makeChart(body,{h:160,xmin:S.tMin,xmax:S.tMax,ymin:0,ymax:180,
    series:[{color:'#f5a623',w:1.6,data:seriesFromBoat(f,'twa')}].concat(p?[{color:'#26d07c',w:1.4,data:seriesFromBoat(p,'twa')}]:[]),
    legs:legSpans(f),fmtY:v=>v.toFixed(0)+'°'}));
  body.insertAdjacentHTML('beforeend',`<div class="muted" style="margin-top:8px">VMG = the speed you actually make
   toward (or away from) the wind. Upwind you want high positive VMG; downwind, high magnitude negative VMG. TWA near
   ~40–45° is close-hauled; ~150° is a deep run. Flat TWA = consistent; spikes = maneuvers.</div>`);
}

function tabWind(body){
  const f=focusB(), ws=S.race.wind_series;
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Estimated wind direction (shift track)</div>');
  const avg=med(ws.dir.filter(v=>v!=null));
  S.charts.push(makeChart(body,{h:180,xmin:S.tMin,xmax:S.tMax,
    series:[{color:'#58a6ff',w:2,data:ws.t.map((t,k)=>[t,ws.dir[k]])},
            {color:'rgba(139,148,158,.5)',w:1,data:[[S.tMin,avg],[S.tMax,avg]]}],
    legs:legSpans(f),fmtY:v=>v.toFixed(0)+'°'}));
  body.insertAdjacentHTML('beforeend',`<div class="muted" style="margin-top:8px">
   Wind direction is reconstructed from the fleet's upwind tacking angles in a moving window — no anemometer needed.
   Rising line = wind veering (clockwise); falling = backing. Dashed grey = race average (${avg?avg.toFixed(0):'–'}°).
   When the trace moves <b>toward</b> your current heading you've been <b>lifted</b>; away = <b>headed</b>.</div>`);

  // lifted/headed scoring on upwind legs
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Were you on the lifted tack? (upwind)</div>');
  const winAt=t=>{ const a=ws.t; let i=0; while(i<a.length-1&&a[i]<t)i++; return ws.dir[i]; };
  let liftedTime=0,headedTime=0;
  for(const [a,c,k] of f.legs){ if(k!=='upwind')continue;
    for(let j=a;j<c;j++){ const w=winAt(f.t[j]); if(w==null)continue;
      const rel=((f.cog[j]-w+540)%360)-180;       // signed angle off wind
      const shift=((w-avg+540)%360)-180;           // wind shift vs mean
      // on starboard (tack>0) a left shift (negative) lifts; on port a right shift lifts
      const lifted = f.tack[j]>0 ? shift<0 : shift>0;
      const dt=(f.t[j+1]-f.t[j]); (lifted?liftedTime+=dt:headedTime+=dt);
    }
  }
  const tot=liftedTime+headedTime||1, lp=Math.round(liftedTime/tot*100);
  body.insertAdjacentHTML('beforeend',`
    <div class="barwrap"><span class="lbl">Lifted</span>
      <div class="bar" style="width:${lp}%;background:var(--good)"></div><span>${lp}%</span></div>
    <div class="barwrap"><span class="lbl">Headed</span>
      <div class="bar" style="width:${100-lp}%;background:var(--bad)"></div><span>${100-lp}%</span></div>
    <div class="muted">Share of upwind time spent on the favoured (lifted) tack relative to the race-mean wind.
    Higher is better — it means you were generally sailing the headers and tacking onto lifts.</div>`);
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
    let m=f.maneuvers.slice();
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
  const f=focusB(), r=S.race, st=r.start_t;
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Start performance (gun ≈ first GPS fix)</div>');
  if(!r.start_line){ body.insertAdjacentHTML('beforeend','<div class="muted">Start line could not be inferred for this race.</div>'); return; }
  // distance to line along wind axis at gun, speed at gun, build speed over first 90s
  const wf=r.wind_from*Math.PI/180, ux=Math.sin(wf), uy=Math.cos(wf);
  const cosL=Math.cos(r.lat0*Math.PI/180);
  const toXY=(la,lo)=>[(lo-r.lon0)*111320*cosL,(la-r.lat0)*111320];
  const a=r.start_line[0], b=r.start_line[1];
  const [ax,ay]=toXY(a[0],a[1]); const lineperp=ax*ux+ay*uy;            // along-wind coord of line
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
  // speed build chart first 120s
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Speed build-up off the line (first 2 min)</div>');
  const t0=st,t1=st+120, fdata=[],pdata=[];
  const p=partnerB();
  for(let t=t0;t<=t1;t+=3){ fdata.push([t,sampleAt(f,t).sog]); if(p)pdata.push([t,sampleAt(p,t).sog]); }
  // fleet median build
  const med2=[]; for(let t=t0;t<=t1;t+=3){ med2.push([t,med(r.boats.map(bb=>sampleAt(bb,t).sog))]); }
  S.charts.push(makeChart(body,{h:170,xmin:t0,xmax:t1,
    series:[{color:'rgba(139,148,158,.8)',w:1.3,data:med2},{color:'#f5a623',w:2,data:fdata}].concat(p?[{color:'#26d07c',w:1.6,data:pdata}]:[]),
    fmtY:v=>v.toFixed(1)}));
  body.insertAdjacentHTML('beforeend',`<div class="muted" style="margin-top:8px">The start line is <b>inferred</b> from
   where the fleet sits at the gun, perpendicular to the estimated wind, so treat distances as approximate. A good start =
   on the line at the gun (≈0 m behind) at full speed, accelerating ahead of the fleet-median curve.</div>`);
}

function tabPosition(body){
  const f=focusB(), p=partnerB(), r=S.race, fs=r.fleet_stats, G=fs.t;
  body.insertAdjacentHTML('beforeend','<div class="sectitle">Fleet position over time (1 = leading)</div>');
  // rank each grid index by course progress
  const rankOf=(boat)=>{ const out=[]; for(let k=0;k<G.length;k++){ const my=boat.course[k]; if(my==null){out.push([G[k],null]);continue;}
      let rank=1; for(const o of r.boats){ const v=o.course[k]; if(v!=null && v>my) rank++; } out.push([G[k],rank]); } return out; };
  const series=[{color:'#f5a623',w:2,data:rankOf(f)}];
  if(p) series.push({color:'#26d07c',w:1.7,data:rankOf(p)});
  S.charts.push(makeChart(body,{h:220,xmin:S.tMin,xmax:S.tMax,ymin:r.boats.length+1,ymax:0,
    series,legs:legSpans(f),fmtY:v=>v.toFixed(0)}));
  body.insertAdjacentHTML('beforeend',`<div class="muted" style="margin-top:8px">Position is estimated from cumulative
   distance made good along the course (made-good distance to each mark). Line going <b>up</b> = gaining places, <b>down</b>
   = losing. This is a GPS proxy for race rank — penalties, OCS and protests aren't reflected. Blue/orange = up/down legs.</div>`);
  // gains/losses summary
  const rk=rankOf(f).filter(x=>x[1]!=null);
  if(rk.length){ body.insertAdjacentHTML('beforeend',`<div class="sectitle">Summary</div>
    <div class="muted">First clear position: <b>${rk[0][1]}</b> · Best: <b class="good">${Math.min(...rk.map(x=>x[1]))}</b>
    · Worst: <b class="bad">${Math.max(...rk.map(x=>x[1]))}</b> · Final: <b>${rk[rk.length-1][1]}</b> of ${r.boats.length}.</div>`); }
}

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
  computeView(); renderBG(); render();
  drawLegend(); setTab(S.tab); syncTime();
}
function drawLegend(){
  const modes={tack:'<span><span class="dot" style="background:#3f9bff"></span>Stbd</span><span><span class="dot" style="background:#ff5e7a"></span>Port</span>',
    speed:'<span>slow</span><span class="dot" style="background:hsl(220,85%,55%)"></span>→<span class="dot" style="background:hsl(10,85%,55%)"></span><span>fast</span>',
    vmg:'<span><span class="dot" style="background:#50c8ff"></span>gaining</span><span><span class="dot" style="background:#ff7b54"></span>losing</span>'};
  $('#legend').innerHTML=`<span><span class="dot" style="background:#f5a623"></span>You</span>`+
    (S.partner?`<span><span class="dot" style="background:#26d07c"></span>${S.byS[S.partner].name}</span>`:'')+
    `<span><span class="dot" style="background:#ffd166"></span>Marks</span> ${modes[S.colorMode]}`;
}

// ---------- events ----------
$('#focusSel').onchange=e=>{ S.focus=e.target.value; renderBG(); render(); drawLegend(); setTab(S.tab); syncTime(); };
$('#partnerSel').onchange=e=>{ S.partner=e.target.value; renderBG(); render(); drawLegend(); setTab(S.tab); syncTime(); };
$('#tabs').onclick=e=>{ if(e.target.dataset.tab) setTab(e.target.dataset.tab); };
$('#playBtn').onclick=()=>setPlaying(!S.playing);
$('#timeRange').oninput=e=>{ S.t=S.tMin+(e.target.value/1000)*(S.tMax-S.tMin); if(S.playing)setPlaying(false); syncTime(); };
$('#fitBtn').onclick=()=>{ computeView(); renderBG(); render(); };
$('#fleetBtn').onclick=e=>{ S.showFleet=!S.showFleet; e.target.classList.toggle('on',S.showFleet); render(); };
document.querySelectorAll('#maptools [data-mode]').forEach(btn=>btn.onclick=()=>{
  S.colorMode=btn.dataset.mode; document.querySelectorAll('#maptools [data-mode]').forEach(b=>b.classList.toggle('on',b===btn));
  renderBG(); render(); drawLegend(); });
map.onmousemove=e=>{ // scrub by hovering near a time? keep simple: show focus values (already live)
};
addEventListener('resize',()=>{ if(S.race){ layout(); renderBG(); render(); S.charts.forEach(c=>c.draw()); } });
addEventListener('keydown',e=>{ if(e.code==='Space'){e.preventDefault();setPlaying(!S.playing);}
  if(e.code==='ArrowRight'){S.t=Math.min(S.tMax,S.t+5);syncTime();} if(e.code==='ArrowLeft'){S.t=Math.max(S.tMin,S.t-5);syncTime();} });

loadIndex();
