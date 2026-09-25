
/* ============================================================
   UrbanView — interactive mockup
   Vanilla JS. Builds a stylised Podgorica cadastre, an info
   panel with the free/paid boundary, an AI assistant, order +
   payment + subscription flows, and an admin console.
   All data is illustrative.
   ============================================================ */
'use strict';

const fmt = n => n.toLocaleString('en-US');
const eur = n => '€' + fmt(Math.round(n));
const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c; if (h != null) e.innerHTML = h; return e; };
const $ = s => document.querySelector(s);

/* ---------- zone palette ---------- */
const ZTYPES = {
  res: { name: 'Residential', col: 'var(--z-res)', hex: '#B5744A' },
  com: { name: 'Commercial',  col: 'var(--z-com)', hex: '#BE9A44' },
  mix: { name: 'Mixed use',   col: 'var(--z-mix)', hex: '#8A7A8E' },
  pub: { name: 'Public / institutional', col: 'var(--z-pub)', hex: '#5E8A82' },
  grn: { name: 'Green / recreation', col: 'var(--z-grn)', hex: '#7C8A4F' }
};

/* ---------- layers (selected by visual card, per client) ---------- */
const LAYERS = [
  { id: 'docareas',name: 'Planning documents', grp: 'base', on: true,  core: true, sw: 'docdash' },
  { id: 'base',    name: 'Base map',       grp: 'base', on: true,  core: true, sw: '#c4bdac' },
  { id: 'zones',   name: 'Urban zones', grp: 'base', on: true,  core: true, sw: 'zones' },
  { id: 'cadastre',name: 'Cadastral parcels', grp: 'parcels', on: true, sw: '#B3A894' },
  { id: 'planned', name: 'Urban parcels',  grp: 'parcels', on: true, sw: 'dash' },
  { id: 'owner',   name: 'Public ownership', grp: 'parcels', on: false, sw: '#4F6D82', req: 'cadastre' },
  { id: 'restit',  name: 'Restitution / legal', grp: 'parcels', on: false, sw: '#9E5568', req: 'cadastre' },
  { id: 'landuse', name: 'Land use',       grp: 'context', on: false, sw: '#b98a5a' },
  { id: 'heatFAR', name: 'FAR heatmap',    grp: 'context', on: false, sw: 'heat1' },
  { id: 'traffic', name: 'Planned traffic',grp: 'context', on: false, sw: '#5b5b5b' },
  { id: 'heatMkt', name: 'Price heatmap',  grp: 'feas', on: false, sw: 'heat2', paid: true }
];

/* price heatmap shows banded €/m² ranges (subscription layer) */
const PRICE_BANDS = [
  { lo:0,    hi:0,    col:'rgba(150,145,132,.34)', label:'not saleable' },
  { lo:1,    hi:1300, col:'rgba(201,154,46,.26)',  label:'under €1,300' },
  { lo:1300, hi:1700, col:'rgba(201,154,46,.45)',  label:'€1,300 – 1,700' },
  { lo:1700, hi:2100, col:'rgba(201,154,46,.62)',  label:'€1,700 – 2,100' },
  { lo:2100, hi:99999,col:'rgba(201,154,46,.82)',  label:'€2,100 and above' }
];
const priceBand = v => PRICE_BANDS.find(b=>v>=b.lo&&v<=b.hi) || PRICE_BANDS[0];

/* ---------- planning documents (a zone groups several) ---------- */
const DOCS = {
  A: [ { n: 'DUP Podgorica – Blok VII', s: 'adopted' }, { n: 'DUP Stari Aerodrom', s: 'adopted' }, { n: 'Izmjene i dopune DUP-a 2023', s: 'progress' } ],
  B: [ { n: 'DUP Centar – Zona C2', s: 'adopted' }, { n: 'PUP Glavni grad (izvod)', s: 'adopted' } ],
  C: [ { n: 'DUP Konik – Sjever', s: 'adopted' }, { n: 'DUP Konik – Jug', s: 'progress' } ],
  D: [ { n: 'DUP Gorica park', s: 'adopted' } ],
  E: [ { n: 'DUP Zabjelo 3', s: 'adopted' }, { n: 'DUP Zabjelo 5', s: 'adopted' } ]
};

/* ---------- map geometry (hand-authored, SVG units ~ metres/2) ---------- */
/* the city is divided into blocks; each block belongs to a zone and holds parcels */
const VIEW = { w: 1000, h: 720 };

/* zone regions (big polygons) keyed by id, with a doc-set + params */
const ZONES = [
  { id:'A', type:'res', doc:'A', name:'Blok VII — Stari Aerodrom', pts:'120,90 360,80 380,250 300,300 130,290',
    far:2.4, cov:40, floors:'P+5+Pk', use:'Residential / mixed', price:1650 },
  { id:'B', type:'mix', doc:'B', name:'Centar — Zona C2', pts:'380,80 640,95 660,250 400,255 380,250',
    far:3.2, cov:55, floors:'P+8', use:'Mixed use — commercial ground floor', price:2450 },
  { id:'C', type:'res', doc:'C', name:'Konik — Sjever', pts:'130,300 300,305 320,470 150,500 110,400',
    far:1.8, cov:35, floors:'P+3', use:'Residential', price:1180 },
  { id:'D', type:'grn', doc:'D', name:'Gorica Park', pts:'660,95 880,110 900,300 700,290 660,250',
    far:0.2, cov:5, floors:'P', use:'Green / recreation — protected', price:0 },
  { id:'E', type:'res', doc:'E', name:'Zabjelo', pts:'320,300 640,260 700,290 720,500 340,520 320,470',
    far:2.0, cov:38, floors:'P+4', use:'Residential', price:1420 },
  { id:'F', type:'com', doc:'B', name:'Poslovna zona — Istok', pts:'720,300 900,300 910,520 740,510 720,500',
    far:2.8, cov:60, floors:'P+6', use:'Commercial / business', price:1980 },
  { id:'G', type:'pub', doc:'A', name:'Institucionalni blok', pts:'150,500 340,520 330,640 160,630',
    far:1.5, cov:45, floors:'P+3', use:'Public / institutional', price:0 }
];

/* uncovered strip (no adopted plan) — south-west corner */
const UNCOVERED = '110,400 150,500 160,630 40,620 40,410';

/* SELECTABLE AREAS = coverage areas of planning documents (not planning zones).
   Several planning zones can sit inside one document's coverage area. */
const DOCLABEL = { A:'DUP Blok VII — Stari Aerodrom', B:'DUP Centar — Zona C2', C:'DUP Konik',
                   D:'DUP Gorica park', E:'DUP Zabjelo' };
const DOCAREAS = {};
ZONES.forEach(z=>{ (DOCAREAS[z.doc] = DOCAREAS[z.doc] || {id:z.doc,label:DOCLABEL[z.doc]||z.doc,zones:[]}).zones.push(z); });

/* build parcels inside each zone by subdividing its bounding area into a grid,
   clipping to the polygon. Each parcel gets a cadastral outline and a smaller
   planned outline (land taken for roads / public space). */
function polyBounds(pts){
  const c = pts.trim().split(/\s+/).map(p=>p.split(',').map(Number));
  const xs=c.map(p=>p[0]), ys=c.map(p=>p[1]);
  return {minx:Math.min(...xs),maxx:Math.max(...xs),miny:Math.min(...ys),maxy:Math.max(...ys),poly:c};
}
function pointInPoly(x,y,poly){
  let inside=false;
  for(let i=0,j=poly.length-1;i<poly.length;j=i++){
    const xi=poly[i][0],yi=poly[i][1],xj=poly[j][0],yj=poly[j][1];
    if(((yi>y)!=(yj>y)) && (x < (xj-xi)*(y-yi)/(yj-yi)+xi)) inside=!inside;
  }
  return inside;
}

let PARCELS = [];
let PID = 1000;
function buildParcels(){
  PARCELS = [];
  ZONES.forEach(z=>{
    if(z.type==='grn'||z.type==='pub') { return; } // parks/institutions: not subdivided into saleable parcels
    const b = polyBounds(z.pts);
    const step = 34;
    let idx=0;
    for(let gy=b.miny; gy<b.maxy; gy+=step){
      for(let gx=b.minx; gx<b.maxx; gx+=step){
        const cx=gx+step/2, cy=gy+step/2;
        if(!pointInPoly(cx,cy,b.poly)) continue;
        // cadastral rect with slight jitter
        const pad=2.2+Math.random()*1.6;
        // real blocks mix large and small plots — vary the plot size within the cell
        const sf = Math.random()<0.42 ? 0.58+Math.random()*0.22 : 0.92+Math.random()*0.08;
        const x=gx+pad, y=gy+pad,
              w=(step-pad*2-Math.random()*3)*sf, h=(step-pad*2-Math.random()*3)*sf;
        if(w<9||h<9) continue;
        // planned parcel: land taken on the street side (top/left) — smaller
        const take = 0.14 + Math.random()*0.16; // 14–30% reduction
        const px=x + w*take*0.6, py=y + h*take*0.6, pw=w*(1-take), ph=h*(1-take);
        const cadArea = Math.round(w*h*1.6);   // ≈ 230–1,450 m², so both price tiers occur
        const planArea = Math.round(pw*ph*1.6);
        PID++;
        idx++;
        PARCELS.push({
          id: PID, zone: z.id, ztype: z.type,
          num: (z.id.charCodeAt(0)-64)*1000 + idx, // parcel number (cadastral ref)
          sub: Math.random()<0.25 ? Math.ceil(Math.random()*3) : null,
          katOp: ['Podgorica I','Podgorica II','Podgorica III'][Math.floor(Math.random()*3)],
          block: z.id + '-' + String(Math.ceil(idx/4)).padStart(2,'0'),
          up: Math.random()<0.87 ? 'UP ' + z.id + String(Math.ceil(idx/4)).padStart(2,'0') + '.' + (((idx-1)%4)+1) : null,
          x,y,w,h, px,py,pw,ph, cadArea, planArea,
          publicOwn: Math.random()<0.12,
          restit: Math.random()<0.08
        });
      }
    }
  });
}
buildParcels();

/* urban block extents, derived from their parcels */
let BLOCKBOX={};
function buildBlocks(){
  BLOCKBOX={};
  PARCELS.forEach(p=>{
    const b=BLOCKBOX[p.block]||(BLOCKBOX[p.block]={minx:1e9,miny:1e9,maxx:-1e9,maxy:-1e9});
    b.minx=Math.min(b.minx,p.x); b.miny=Math.min(b.miny,p.y);
    b.maxx=Math.max(b.maxx,p.x+p.w); b.maxy=Math.max(b.maxy,p.y+p.h);
  });
}
buildBlocks();

/* ---------- market/cost model (deterministic, ranges) ---------- */
function feasibility(parcel, zone, assume){
  const A = parcel.planArea;              // planned parcel area drives everything
  const far = zone.far, cov = zone.cov;
  const gfa = Math.round(A * far);
  const coverage = Math.round(A * cov/100);
  const saleablePct = assume.saleable/100;
  const saleable = Math.round(gfa * saleablePct);
  const landVal = Math.round(A * zone.price * 0.55);          // land acquisition
  const design = Math.round(gfa * 90);                        // design & documentation
  const constr = Math.round(gfa * assume.constr);             // construction
  const mktUnit = assume.sale;                                // sale €/m²
  const marketVal = Math.round(saleable * mktUnit);
  const costTotal = landVal + design + constr;
  const profit = marketVal - costTotal;
  const roi = costTotal>0 ? (profit/costTotal*100) : 0;
  const band = v => ({ lo: Math.round(v*0.86), ex: Math.round(v), hi: Math.round(v*1.15) });
  return {
    gfa, coverage, saleable, landVal, design, constr, marketVal, costTotal, profit,
    roi, roiLo: roi-6.5, roiHi: roi+7.5,
    marketBand: band(marketVal), profitBand: band(profit), constrBand: band(constr), landBand: band(landVal)
  };
}

let DRAGGED=false; // true while the map is being panned, so a drag never selects
/* ---------- app state ---------- */
const STATE = {
  sel: null,          // selected parcel
  selMode: 'cad',     // 'cad' = cadastral parcel, 'urban' = urban parcel
  selDoc: null,       // selected planning-document coverage area
  selZone: null,
  paidMarket: false,  // has market-data subscription
  paidAI: false,      // unlimited AI
  aiQuota: 3,
  railOpen: true,
  layers: Object.fromEntries(LAYERS.map(l=>[l.id,l.on])),
  orderType: 'individual', // 'individual' | 'legal'
  assume: { constr: 780, sale: 2200, saleable: 70 } // editable assumptions
};

/* ============================================================
   RENDER: app chrome (topbar, rail, map, panel, ai, admin)
   ============================================================ */
function renderApp(){
  const app = $('#app');
  app.innerHTML = `
  <div class="topbar">
    <div class="brand">
      <img class="logo" src="${(window.__resources&&window.__resources.logo)||'UrbanView_CombinationMark.svg'}" alt="UrbanView">
    </div>
    <div class="searchwrap">
      <span class="mag"><svg width="15" height="15" viewBox="0 0 15 15" fill="none"><circle cx="6.5" cy="6.5" r="4.5" stroke="rgba(244,236,216,.55)" stroke-width="1.5"/><path d="M10 10l3.5 3.5" stroke="rgba(244,236,216,.55)" stroke-width="1.5"/></svg></span>
      <input id="search" placeholder="Search an address, click the map, or enter a parcel number…" autocomplete="off">
      <span class="kbd">⌘K</span>
      <div class="searchsug" id="searchsug"></div>
    </div>
    <div class="topnav">
      <button id="navMap" class="active"><svg width="15" height="15" viewBox="0 0 15 15" fill="none"><path d="M2 4l4-2 3 2 4-2v9l-4 2-3-2-4 2z" stroke="currentColor" stroke-width="1.3"/></svg>Map</button>
      <button id="navAdmin"><svg width="15" height="15" viewBox="0 0 15 15" fill="none"><path d="M7.5 2v11M2 7.5h11" stroke="currentColor" stroke-width="1.3"/><circle cx="7.5" cy="7.5" r="5.5" stroke="currentColor" stroke-width="1.3"/></svg>Admin</button>
      <button class="pill" id="homePill" title="Return to Podgorica">PODGORICA · PILOT</button>
    </div>
  </div>
  <div class="main">
    <div class="rail" id="rail"></div>
    <div class="mapwrap">
      <svg id="map" viewBox="0 0 ${VIEW.w} ${VIEW.h}" preserveAspectRatio="xMidYMid slice"></svg>
      <div class="mapchrome">
        <div class="legend" id="legend"></div>
        <div class="coverwarn" id="coverwarn"><svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 1l6 11H1z" stroke="#FFFFFF" stroke-width="1.3"/><path d="M7 5v3M7 10h.01" stroke="#FFFFFF" stroke-width="1.3"/></svg><span>Outside current coverage</span> <span class="mono">no adopted plan</span></div>
        <div class="scalebar"><span class="t">250 m</span><div class="bar"></div></div>
        <div class="coords mono" id="coords">42.4411° N · 19.2636° E</div>
        <div class="maptools">
          <div class="zlabel mono" id="zlabel" style="text-align:center">1.0×</div>
          <button class="mbtn" id="zin">+</button>
          <button class="mbtn" id="zout">−</button>
          <button class="mbtn" id="zreset"><svg width="15" height="15" viewBox="0 0 15 15" fill="none"><path d="M7.5 2.5v10M2.5 7.5h10" stroke="currentColor" stroke-width="1.3"/><circle cx="7.5" cy="7.5" r="2" stroke="currentColor" stroke-width="1.3"/></svg></button>
        </div>
      </div>
    </div>
    <aside class="panel" id="panel"></aside>
    <button class="aifab" id="aifab" title="Ask UrbanView AI">
      <svg width="22" height="22" viewBox="0 0 22 22" fill="none"><path d="M4 5h14v9H9l-4 3v-3H4z" stroke="#FFFFFF" stroke-width="1.5" stroke-linejoin="round"/><circle cx="8" cy="9.5" r="1" fill="rgba(255,255,255,.14)"/><circle cx="11" cy="9.5" r="1" fill="#f4efe6"/><circle cx="14" cy="9.5" r="1" fill="#f4efe6"/></svg>
      <span class="badge2" id="aibadge">3 free</span>
    </button>
    <div class="aipanel" id="aipanel"></div>
    <div class="admin" id="admin"></div>
  </div>`;
}

/* ============================================================
   MAP: build the SVG cadastre
   ============================================================ */
function renderMap(){
  const svg = $('#map');
  const NS='http://www.w3.org/2000/svg';
  const mk=(t,a)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);return e;};
  svg.innerHTML='';

  // paper texture bg
  svg.appendChild(mk('rect',{x:0,y:0,width:VIEW.w,height:VIEW.h,fill:'#EEF1F5'}));

  // uncovered area (rendered flat, base only)
  const unc = mk('polygon',{points:UNCOVERED,fill:'#E6E9EE',stroke:'#DCE1E8','stroke-width':1});
  unc.setAttribute('id','uncovered'); svg.appendChild(unc);

  // river (Morača) diagonal
  svg.appendChild(mk('path',{d:'M-20,120 C120,180 180,260 260,360 C320,440 300,560 360,740 L410,740 C360,560 380,440 320,360 C250,250 190,180 40,120 Z',class:'river'}));
  svg.appendChild(mk('path',{d:'M-20,120 C120,180 180,260 260,360 C320,440 300,560 360,740',class:'riverline'}));

  // ZONE fills
  const gZones = mk('g',{id:'gzones'}); svg.appendChild(gZones);
  ZONES.forEach(z=>{
    const t=ZTYPES[z.type];
    gZones.appendChild(mk('polygon',{points:z.pts,fill:t.hex,'fill-opacity':STATE.layers.zones?0.24:0,
      stroke:'none',class:'zone','data-zone':z.id}));
  });
  // planning-zone boundaries
  if(STATE.layers.zones){
    ZONES.forEach(z=>{ gZones.appendChild(mk('polygon',{points:z.pts,class:'zoneline',stroke:ZTYPES[z.type].hex})); });
  }

  // roads (major grid + ring)
  const gRoads=mk('g',{id:'groads',opacity:STATE.layers.base?1:0.15}); svg.appendChild(gRoads);
  [['M120,90 L900,110'],['M110,300 L910,300'],['M130,300 L340,640'],['M380,80 L400,520'],
   ['M660,95 L740,510'],['M120,90 L160,630']].forEach(d=>gRoads.appendChild(mk('path',{d:d[0],class:'road major'})));
  for(let x=180;x<900;x+=70) gRoads.appendChild(mk('path',{d:`M${x},85 L${x+30},640`,class:'road minor'}));
  for(let y=140;y<620;y+=55) gRoads.appendChild(mk('path',{d:`M110,${y} L910,${y-10}`,class:'road minor'}));

  // planned traffic (extra, only when layer on)
  if(STATE.layers.traffic){
    const gt=mk('g',{}); svg.appendChild(gt);
    ['M120,200 L900,215','M500,80 L520,640'].forEach(d=>gt.appendChild(mk('path',{d,stroke:'#5b5b5b','stroke-width':6,'stroke-dasharray':'10 6',fill:'none','stroke-opacity':.55})));
  }

  // land use tint (when on)
  if(STATE.layers.landuse){
    ZONES.forEach(z=>{const t=ZTYPES[z.type];
      svg.appendChild(mk('polygon',{points:z.pts,fill:t.hex,'fill-opacity':.32,stroke:'none','pointer-events':'none'}));});
  }

  // heatmaps
  if(STATE.layers.heatFAR){
    const gh=mk('g',{class:'heatcell'}); svg.appendChild(gh);
    ZONES.forEach(z=>gh.appendChild(mk('polygon',{points:z.pts,
      fill:`rgba(198,107,74,${0.15+(z.far/3.4)*0.55})`,stroke:'none','pointer-events':'none'})));
  }
  if(STATE.layers.heatMkt && STATE.paidMarket){
    const gm=mk('g',{class:'heatcell'}); svg.appendChild(gm);
    ZONES.forEach(z=>gm.appendChild(mk('polygon',{points:z.pts,
      fill:priceBand(z.price).col,stroke:'none','pointer-events':'none'})));
  }

  // PLANNING-DOCUMENT COVERAGE AREAS — these are the selectable areas
  if(STATE.layers.docareas){
    const gd=mk('g',{id:'gdocs'}); svg.appendChild(gd);
    Object.values(DOCAREAS).forEach(da=>{
      da.zones.forEach(z=>{
        const poly=mk('polygon',{points:z.pts,class:'docarea'+(STATE.selDoc===da.id?' sel':''),'data-doc':da.id});
        const ttl=mk('title',{}); ttl.textContent=da.label+' — click to open this planning document';
        poly.appendChild(ttl);
        poly.addEventListener('click',e=>{ if(DRAGGED)return; e.stopPropagation(); selectDoc(da.id); });
        gd.appendChild(poly);
      });
    });
  }

  // PARCELS
  const gParc=mk('g',{id:'gparcels'}); svg.appendChild(gParc);
  if(STATE.layers.cadastre){
    PARCELS.forEach(p=>{
      const t=ZTYPES[p.ztype];
      let fill = t.hex, op=0.5;
      if(STATE.layers.owner && p.publicOwn){ fill='#4F6D82'; op=0.7; }
      if(STATE.layers.restit && p.restit){ fill='#9E5568'; op=0.75; }
      const r=mk('rect',{x:p.x,y:p.y,width:p.w,height:p.h,rx:1.5,fill:fill,'fill-opacity':op,class:'parcel','data-pid':p.id});
      const ct=mk('title',{}); ct.textContent='Cadastral parcel #'+p.num+(p.sub?'/'+p.sub:'')+(p.up?' → '+p.up:''); r.appendChild(ct);
      r.addEventListener('click',e=>{ if(DRAGGED)return; e.stopPropagation(); selectParcel(p.id,'cad'); });
      gParc.appendChild(r);
    });
  }

  // URBAN PARCELS — clickable in their own right
  if(STATE.layers.planned){
    const gp=mk('g',{id:'gplanned'}); svg.appendChild(gp);
    PARCELS.forEach(p=>{
      if(!p.up) return;
      const r=mk('rect',{x:p.px,y:p.py,width:p.pw,height:p.ph,rx:1,class:'uparcel','data-upid':p.id});
      const ut=mk('title',{}); ut.textContent=p.up+' — urban parcel (building rights)'; r.appendChild(ut);
      r.addEventListener('click',e=>{ if(DRAGGED)return; e.stopPropagation(); selectParcel(p.id,'urban'); });
      gp.appendChild(r);
    });
  }

  // urban block boundaries + labels (part of the planning-zones layer)
  if(STATE.layers.zones){
    const gb=mk('g',{id:'gblocks'}); svg.appendChild(gb);
    Object.keys(BLOCKBOX).forEach(name=>{
      const b=BLOCKBOX[name];
      gb.appendChild(mk('rect',{x:b.minx-3.5,y:b.miny-3.5,width:(b.maxx-b.minx)+7,height:(b.maxy-b.miny)+7,rx:2,class:'blockline'}));
      gb.appendChild(mk('text',{x:b.minx-2.5,y:b.miny-5.5,class:'blocklabel'})).textContent=name;
    });
  }

  // zone labels
  ZONES.forEach(z=>{
    const b=polyBounds(z.pts);
    const tx=mk('text',{x:(b.minx+b.maxx)/2,y:(b.miny+b.maxy)/2,'text-anchor':'middle',
      'font-family':'Schibsted Grotesk','font-size':13,'font-weight':600,fill:'rgba(42,33,24,.5)','pointer-events':'none'});
    tx.textContent=z.name.split('—')[0].trim(); svg.appendChild(tx);
  });

  // selection pin group
  svg.appendChild(mk('g',{id:'gpin'}));

  // click empty map → drop a pin at that point (coords, no parcel)
  svg.onclick=(e)=>{ if(!DRAGGED) pinEmpty(e); };

  restoreSelectionHighlight();
}

/* view-coordinate helpers (respect zoom / pan / viewBox slice) */
function clientToView(clientX,clientY){
  const svg=$('#map'); const m=svg.getScreenCTM(); if(!m)return null;
  const pt=svg.createSVGPoint(); pt.x=clientX; pt.y=clientY;
  const v=pt.matrixTransform(m.inverse());
  return { x:Math.max(0,Math.min(VIEW.w,v.x)), y:Math.max(0,Math.min(VIEW.h,v.y)) };
}
function coordStr(x,y){ return `42.4${(410+y/9|0)}° N · 19.2${(600+x/9|0)}° E`; }

/* free pin: user clicked the map but not a parcel */
function pinEmpty(e){
  const v=clientToView(e.clientX,e.clientY); if(!v)return;
  STATE.sel=null; STATE.selZone=null; STATE.selDoc=null; STATE.selMode='cad';
  STATE.freePin=v;
  document.querySelectorAll('.parcel.sel,.uparcel.sel,.docarea.sel').forEach(n=>n.classList.remove('sel'));
  $('#coords').textContent=coordStr(v.x,v.y);
  drawPinAt(v.x,v.y);
  renderPanelEmpty(v);
  $('#panel').classList.remove('hidden');
}

function restoreSelectionHighlight(){
  document.querySelectorAll('.parcel.sel,.uparcel.sel').forEach(n=>n.classList.remove('sel'));
  if(STATE.sel){
    const sel=STATE.selMode==='urban'
      ? document.querySelector(`.uparcel[data-upid="${STATE.sel.id}"]`)
      : document.querySelector(`.parcel[data-pid="${STATE.sel.id}"]`);
    if(sel){ sel.classList.add('sel'); sel.parentNode.appendChild(sel); }
    drawPin(STATE.sel);
  } else if(STATE.freePin){
    drawPinAt(STATE.freePin.x, STATE.freePin.y);
  }
}
function drawPin(p){
  drawPinAt(p.px+p.pw/2, p.py+p.ph/2);
}
function drawPinAt(cx,cy){
  const g=$('#gpin'); if(!g)return; g.innerHTML='';
  const NS='http://www.w3.org/2000/svg';
  const pin=document.createElementNS(NS,'path');
  pin.setAttribute('d',`M${cx},${cy-26} c-7,0 -12,5 -12,12 c0,9 12,20 12,20 c0,0 12,-11 12,-20 c0,-7 -5,-12 -12,-12 z`);
  pin.setAttribute('fill','#B5613B'); pin.setAttribute('stroke','#ffffff'); pin.setAttribute('stroke-width','1.5');
  pin.setAttribute('class','pin');
  const dot=document.createElementNS(NS,'circle');
  dot.setAttribute('cx',cx);dot.setAttribute('cy',cy-14);dot.setAttribute('r','3.6');dot.setAttribute('fill','#ffffff');
  g.appendChild(pin);g.appendChild(dot);
}

/* ============================================================
   LAYER RAIL
   ============================================================ */
function swatchHTML(sw){
  if(sw==='zones') return `<div class="swatch" style="background:conic-gradient(#B5744A,#BE9A44,#8A7A8E,#5E8A82,#7C8A4F)"></div>`;
  if(sw==='dash') return `<div class="swatch" style="background:#fff;border:1.5px dashed #B5613B"></div>`;
  if(sw==='docdash') return `<div class="swatch" style="background:#fff;border:2px dashed #B3A894"></div>`;
  if(sw==='blockdash') return `<div class="swatch" style="background:#fff;border:1.5px dotted #B3A894"></div>`;
  if(sw==='heat1') return `<div class="swatch" style="background:linear-gradient(135deg,#EFE3CE,#B4744A)"></div>`;
  if(sw==='heat2') return `<div class="swatch" style="background:linear-gradient(135deg,#F1E7CF,#B5853F)"></div>`;
  return `<div class="swatch" style="background:${sw}"></div>`;
}
const GRPLABEL={base:'Base',parcels:'Parcels',context:'Context',feas:'Feasibility'};
function renderRail(){
  const rail=$('#rail'); rail.innerHTML='';
  rail.classList.toggle('collapsed', !STATE.railOpen);
  if(!STATE.railOpen){
    const opener=el('button','railopen','<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M2 3h12M2 8h12M2 13h12" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>');
    opener.title='Show map layers';
    opener.addEventListener('click',()=>{ STATE.railOpen=true; renderRail(); });
    rail.appendChild(opener);
    return;
  }
  const head=el('div','railhead',
    `<b>Map layers</b><button class="railtog" title="Collapse"><svg width="15" height="15" viewBox="0 0 15 15" fill="none"><path d="M9.5 3L5 7.5l4.5 4.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg></button>`);
  head.querySelector('.railtog').addEventListener('click',()=>{ STATE.railOpen=false; renderRail(); });
  rail.appendChild(head);
  let lastGrp='';
  LAYERS.forEach(l=>{
    if(l.grp!==lastGrp){ rail.appendChild(el('div','rlabel',GRPLABEL[l.grp])); lastGrp=l.grp; }
    const on=STATE.layers[l.id];
    const reqL = l.req ? LAYERS.find(x=>x.id===l.req) : null;
    const dep = !!(reqL && !STATE.layers[l.req]);
    const locked = !!(l.paid && !STATE.paidMarket);
    const b=el('button',`lyr${on&&!locked?' on':''}${l.core?' core':''}${dep?' dep':''}${locked?' paidlayer':''}`,
      `${swatchHTML(l.sw)}<span class="lyrtext"><span class="nm">${l.name}</span>${locked?`<span class="subnm lockedsub">Subscription</span>`:l.sub?`<span class="subnm">${l.sub}</span>`:''}</span>
       <span class="chk"><svg width="8" height="8" viewBox="0 0 8 8"><path d="M1 4l2 2 4-5" stroke="#fff" stroke-width="1.5" fill="none"/></svg></span>
       ${l.core?'<span class="lock">◆</span>':''}${dep?'<span class="depwarn">▲</span>':''}
       ${locked?'<span class="paidlock"><svg width="9" height="9" viewBox="0 0 12 12" fill="none"><rect x="2" y="5.4" width="8" height="5.4" rx="1.2" fill="currentColor"/><path d="M4 5.4V4a2 2 0 014 0v1.4" stroke="currentColor" stroke-width="1.3"/></svg></span>':''}`);
    b.title = l.core ? l.name+' (core layer, always on)'
      : locked ? l.name+' — included with the market-data subscription'
      : reqL ? l.name+' — shades cadastral parcels, so it only shows when “'+reqL.name+'” is on'
      : 'Toggle '+l.name;
    b.addEventListener('click',()=>{
      if(l.core) { toast('Core layer — always visible'); return; }
      if(l.paid && !STATE.paidMarket){ openUpgrade('market'); return; }
      STATE.layers[l.id]=!STATE.layers[l.id];
      if(STATE.layers[l.id] && reqL && !STATE.layers[l.req])
        toast(l.name+' shades cadastral parcels — turn on “'+reqL.name+'” to see it');
      renderRail(); renderMap(); renderLegend();
    });
    rail.appendChild(b);
    if(on && dep){
      const n=el('div','depnote',`Needs<b>${reqL.name}</b>tap to turn on`);
      n.title='Turn on '+reqL.name;
      n.addEventListener('click',()=>{ STATE.layers[l.req]=true; renderRail(); renderMap(); renderLegend();
        toast(reqL.name+' turned on'); });
      rail.appendChild(n);
    }
  });
}

/* ---------- legend ---------- */
function renderLegend(){
  const lg=$('#legend');
  const L=STATE.layers;
  const needCad = L.cadastre ? '' : ' — needs cadastral parcels';
  const secs=[];
  if(L.docareas) secs.push({t:'Planning documents', rows:[['docdash','Coverage area — click to open']]});
  if(L.zones) secs.push({t:'Urban zones', rows:[
    ...Object.values(ZTYPES).map(z=>[z.hex,z.name]),
    ['blockdash','Urban block boundary']
  ]});
  if(L.cadastre) secs.push({t:'Cadastral parcels', rows:[['cadsw','Parcel outline']]});
  if(L.planned) secs.push({t:'Urban parcels', rows:[['dash','Parcel — click to open']]});
  if(L.owner) secs.push({t:'Public ownership', rows:[['#4F6D82','Publicly owned'+needCad]]});
  if(L.restit) secs.push({t:'Restitution / legal', rows:[['#9E5568','Legal claim'+needCad]]});
  if(L.landuse) secs.push({t:'Land use', rows:Object.values(ZTYPES).map(z=>[z.hex,z.name])});
  if(L.heatFAR) secs.push({t:'FAR intensity', unit:'floor area ratio', rows:[['grad:#F1E7D6,#B5613B','Low → high']]});
  if(L.traffic) secs.push({t:'Planned traffic', rows:[['line:#5b5b5b','Planned route']]});
  if(L.heatMkt && STATE.paidMarket) secs.push({t:'Price heatmap', unit:'€/m² land', rows:PRICE_BANDS.map(b=>[b.col,b.label])});

  const mark = c =>
      c.startsWith('line:') ? `<span class="sw line" style="--lc:${c.slice(5)}"></span>`
    : c.startsWith('grad:') ? `<span class="sw" style="width:26px;flex:0 0 26px;border-radius:3px;background:linear-gradient(90deg,${c.slice(5)})"></span>`
    : c==='dash' ? `<span class="sw" style="background:#fff;border:1.5px dashed #B5613B"></span>`
    : c==='docdash' ? `<span class="sw" style="background:#fff;border:2px dashed #B3A894"></span>`
    : c==='blockdash' ? `<span class="sw" style="background:#fff;border:1.5px dotted #B3A894"></span>`
    : c==='cadsw' ? `<span class="sw" style="background:#fff;border:1.5px solid #B3A894"></span>`
    : `<span class="sw" style="background:${c}"></span>`;

  const body = secs.map(s=>
    `<div class="leggrp"><div class="legh">${s.t}${s.unit?` <span class="legu">${s.unit}</span>`:''}</div>`+
    s.rows.map(([c,n])=>`<div class="legrow">${mark(c)}${n}</div>`).join('')+
    `</div>`).join('');

  lg.innerHTML=`<h4>Legend <button id="legmin">–</button></h4><div class="legbody">${
    body||'<div class="legrow" style="opacity:.6">No overlays active</div>'
  }</div>`;
  $('#legmin').addEventListener('click',e=>{e.stopPropagation();lg.classList.toggle('min');
    e.target.textContent=lg.classList.contains('min')?'+':'–';});
}

/* ============================================================
   SELECTION
   ============================================================ */
function selectParcel(id,mode){
  const p=PARCELS.find(x=>x.id===id); if(!p)return;
  mode = (mode==='urban' && p.up) ? 'urban' : 'cad';
  STATE.sel=p; STATE.selMode=mode; STATE.selZone=null; STATE.selDoc=null; STATE.freePin=null;
  $('#coords').textContent=`42.4${(410+p.y/9|0)}° N · 19.2${(600+p.x/9|0)}° E`;
  renderMap(); renderPanel(); $('#panel').classList.remove('hidden');
}
window.selectParcel=selectParcel;
function selectDoc(id){
  const da=DOCAREAS[id]; if(!da)return;
  STATE.selDoc=id; STATE.sel=null; STATE.selZone=null; STATE.freePin=null;
  const g=$('#gpin'); if(g)g.innerHTML='';
  renderMap(); renderPanelDoc(da); $('#panel').classList.remove('hidden');
}
window.selectDoc=selectDoc;
function selectZone(id){
  const z=ZONES.find(x=>x.id===id); if(!z)return;
  STATE.selZone=z; STATE.sel=null; STATE.freePin=null;
  renderMap(); renderPanelZone(z); $('#panel').classList.remove('hidden');
}
function clearSelection(){
  const had = STATE.sel||STATE.selDoc;
  STATE.sel=null; STATE.selZone=null; STATE.selDoc=null; STATE.selMode='cad'; STATE.freePin=null;
  const g=$('#gpin'); if(g)g.innerHTML='';
  document.querySelectorAll('.parcel.sel,.uparcel.sel,.docarea.sel').forEach(n=>n.classList.remove('sel'));
  if(had) renderMap();
  renderPanelEmpty();
}

/* ---------- empty panel ---------- */
function renderPanelEmpty(pin){
  const pinned = pin || STATE.freePin;
  $('#panel').innerHTML=`
  <div class="pempty">
    <div class="ill"><img src="${(window.__resources&&window.__resources.mark)||'UrbanView_BrandMark.svg'}" alt="UrbanView"></div>
    <h3>Pick a parcel to begin</h3>
    ${pinned?`<div class="pinnote"><svg width="13" height="13" viewBox="0 0 13 13" fill="none"><path d="M6.5 1C4.6 1 3 2.6 3 4.5C3 7 6.5 12 6.5 12S10 7 10 4.5C10 2.6 8.4 1 6.5 1z" fill="#B5613B" stroke="#fff" stroke-width="1"/><circle cx="6.5" cy="4.5" r="1.3" fill="#fff"/></svg><span>Pin dropped at <b>${coordStr(pinned.x,pinned.y)}</b> — no parcel at this point.</span></div>`:''}
    <p>Click a cadastral parcel, an urban parcel, or a plan coverage area. UrbanView reads the adopted plan and tells you what can be built — and whether it's worth building.</p>
    <div class="hintrow">
      <span class="chiphint">◆ click a parcel</span>
      <span class="chiphint">▨ click a plan area</span>
      <span class="chiphint">⇕ scroll to zoom</span>
      <span class="chiphint">✥ drag to pan</span>
      <span class="chiphint">⌕ search address</span>
    </div>
  </div>`;
}

/* ---------- zone panel ---------- */
function renderPanelZone(z){
  const t=ZTYPES[z.type];
  const docs=DOCS[z.doc]||[];
  $('#panel').innerHTML=`
  <div class="pscroll">
    <div class="phead">
      <button class="pclose" onclick="clearSelection()">✕</button>
      <div class="peyebrow"><span class="tag">ZONE</span> ${t.name}</div>
      <div class="ptitle">${z.name}</div>
      <div class="psub">Internal city division · ≈ city quarter</div>
    </div>
    <div class="sect">
      <div class="secthead"><span class="lbl">Planning documents <span class="badge free">Free</span></span></div>
      <p style="font-size:11.5px;color:var(--ink-2);margin:-4px 0 11px;line-height:1.5">In Montenegro a zone isn't an official bounded area — it's UrbanView's own grouping of related planning documents, roughly a city quarter. This zone groups ${docs.length}.</p>
      <div class="doclist">
        ${docs.map(d=>`<div class="docitem">
          <span class="di"><svg width="15" height="15" viewBox="0 0 15 15" fill="none"><path d="M3 1h6l3 3v10H3z" stroke="currentColor" stroke-width="1.2"/><path d="M9 1v3h3" stroke="currentColor" stroke-width="1.2"/></svg></span>
          <span class="dn">${d.n}<span class="dm">source PDF · eRegistri</span></span>
          <span class="dstat ${d.s}">${d.s==='progress'?'In progress':'Adopted'}</span>
        </div>`).join('')}
      </div>
    </div>
    <div class="sect">
      <div class="secthead"><span class="lbl">Zone-level planning</span></div>
      <div class="prow"><span class="pk">Predominant land use</span><span class="pv" style="font-size:11.5px">${z.use}</span></div>
      <div class="prow"><span class="pk">Typical FAR (II)</span><span class="pv">${z.far.toFixed(1)}</span></div>
      <div class="prow"><span class="pk">Typical coverage (IZ)</span><span class="pv">${z.cov}<span class="u">%</span></span></div>
      <div class="prow"><span class="pk">Typical height</span><span class="pv">${z.floors}</span></div>
    </div>
    <div class="sect">
      <p style="font-size:12px;color:var(--ink-2);line-height:1.55">Click a specific parcel inside this zone to see its full planning parameters, the existing-vs-planned comparison, and the market feasibility.</p>
    </div>
  </div>`;
}

/* ---------- planning-document panel (the selected area IS one document) ---------- */
function renderPanelDoc(da){
  const docs=DOCS[da.id]||[];
  const zs=da.zones;
  const nParc=PARCELS.filter(p=>zs.some(z=>z.id===p.zone)).length;
  const nUrban=PARCELS.filter(p=>p.up&&zs.some(z=>z.id===p.zone)).length;
  const amend=docs.find(d=>d.s==='progress');
  const status = docs.some(d=>d.s==='adopted') ? 'adopted' : (amend?'progress':'adopted');
  const docType = /^PUP/.test(da.label)?'PUP — General urban plan'
    : /^PGR/.test(da.label)?'PGR — General regulation plan'
    : 'DUP — Detailed urban plan';
  const useSummary = ZTYPES[zs[0]&&zs[0].type] ? ZTYPES[zs[0].type].name : 'Mixed use';
  $('#panel').innerHTML=`
  <div class="pscroll">
    <div class="phead">
      <button class="pclose" onclick="clearSelection()">✕</button>
      <div class="peyebrow"><span class="tag">PLANNING DOCUMENT</span> adopted plan</div>
      <div class="ptitle">${da.label}</div>
      <div class="psub">${docType}</div>
    </div>

    <div class="sect">
      <div class="secthead"><span class="lbl">Document details <span class="badge free">Free</span></span>
        <span class="srcref" title="Traceable to source document"><svg width="10" height="11" viewBox="0 0 10 11" fill="none"><path d="M1 1h5l3 3v6H1z" stroke="currentColor" stroke-width="1" stroke-linejoin="round"/><path d="M6 1v3h3" stroke="currentColor" stroke-width="1"/></svg>source</span></div>
      <div class="prow"><span class="pk">Document name</span><span class="pv" style="font-size:11.5px">${da.label}</span></div>
      <div class="prow"><span class="pk">Type</span><span class="pv" style="font-size:11.5px">${docType.split('—')[0].trim()}</span></div>
      <div class="prow"><span class="pk">Status</span><span class="pv"><span class="dstat ${status}">${status==='progress'?'In progress':'Adopted'}</span></span></div>
      <div class="prow"><span class="pk">Source</span><span class="pv" style="font-size:11.5px">PDF · eRegistri</span></div>
      ${amend?`<div class="prow" style="flex-direction:column;align-items:stretch;gap:5px"><span class="pk">Amendments</span><span style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><span style="font-family:var(--mono);font-size:11.5px;color:var(--ink)">${amend.n}</span><span class="dstat progress">In progress</span></span></div>`:''}
    </div>

    <div class="sect">
      <div class="secthead"><span class="lbl">General planning information</span></div>
      <p style="font-size:12.5px;color:var(--ink-2);margin:-2px 0 0;line-height:1.55">This adopted plan governs building rights across its coverage area. Predominant land use is <b>${useSummary.toLowerCase()}</b>. Per-parcel parameters — land use, height, coverage, FAR — are defined on the urban parcels inside it; click any parcel to read them.</p>
    </div>

    <div class="sect">
      <div class="secthead"><span class="lbl">Coverage</span></div>
      <div class="prow"><span class="pk">Urban zones spanned</span><span class="pv">${zs.length}</span></div>
      <div class="prow"><span class="pk">Cadastral parcels</span><span class="pv">${nParc}</span></div>
      <div class="prow"><span class="pk">Urban parcels</span><span class="pv">${nUrban}</span></div>
      <div style="margin-top:9px;display:flex;flex-direction:column;gap:5px">
        ${zs.map(z=>`<div class="prow" style="padding:5px 0"><span class="pk" style="font-size:12.5px">${z.name}</span><span class="pv" style="font-size:11.5px">${z.floors} · FAR ${z.far.toFixed(1)}</span></div>`).join('')}
      </div>
    </div>
  </div>
  <div class="ctastack">
    <button class="cta ghost" onclick="openAI('What does ${da.label} allow?')">
      <svg width="15" height="15" viewBox="0 0 15 15" fill="none"><path d="M2 3h11v7H6l-3 2.5V10H2z" stroke="#12211f" stroke-width="1.2" stroke-linejoin="round"/></svg>
      Ask about this document</button>
    <button class="cta line" onclick="openMethodology(1)">
      <svg width="15" height="15" viewBox="0 0 15 15" fill="none"><circle cx="3" cy="3" r="1.6" stroke="currentColor" stroke-width="1.2"/><circle cx="3" cy="12" r="1.6" stroke="currentColor" stroke-width="1.2"/><path d="M3 4.6v5.8M6.5 3H13M6.5 7.5H13M6.5 12H13" stroke="currentColor" stroke-width="1.2"/></svg>
      How we read a planning document</button>
  </div>`;
}

/* ---------- cadastral parcel panel ---------- */
function renderPanelCadastral(){
  const p=STATE.sel; if(!p)return;
  const z=ZONES.find(z=>z.id===p.zone);
  const t=ZTYPES[p.ztype];
  const delta=Math.round((1-p.planArea/p.cadArea)*100);
  const parcelNo = p.num + (p.sub?('/'+p.sub):'');
  $('#panel').innerHTML=`
  <div class="pscroll">
    <div class="phead">
      <button class="pclose" onclick="clearSelection()">✕</button>
      <div class="peyebrow"><span class="tag">CADASTRAL PARCEL</span> ${t.name}</div>
      <div class="ptitle">Parcel #${parcelNo}</div>
      <div class="psub">${z.name} · ${p.katOp}</div>
    </div>

    <div class="idgrid">
      <div class="idcell"><div class="k">Parcel number</div><div class="v">${parcelNo}</div></div>
      <div class="idcell"><div class="k">Cadastral municipality</div><div class="v">${p.katOp}</div></div>
      <div class="idcell"><div class="k">Urban block</div><div class="v">${p.block}</div></div>
      <div class="idcell"><div class="k">Cadastral area</div><div class="v">${fmt(p.cadArea)} m²</div></div>
      <div class="idcell full"><div class="k">Governing document</div><div class="v" style="font-size:11px">${(DOCS[z.doc]||[{n:'—'}])[0].n}</div></div>
    </div>

    <div class="sect">
      <div class="secthead"><span class="lbl">Corresponding urban parcel</span></div>
      ${p.up ? `
      <div class="upcard" onclick="selectParcel(${p.id},'urban')">
        <div class="upn">${p.up}</div>
        <div class="upd">This cadastral parcel corresponds to an urban parcel in the adopted plan. Building rights — land use, height, coverage, FAR — are defined on the <b>urban parcel</b>, not on the cadastral one.</div>
        <div class="upgo">Open urban parcel <span>→</span></div>
      </div>
      <div class="vscard" style="margin-top:11px">
        <svg class="vsmini" viewBox="0 0 66 52">
          <rect x="4" y="4" width="58" height="44" rx="2" fill="none" stroke="#B3A894" stroke-width="1.5"/>
          <rect x="14" y="12" width="40" height="30" rx="2" fill="#B5613B" fill-opacity=".18" stroke="#B5613B" stroke-width="1.4" stroke-dasharray="3 2"/>
          <text x="58" y="50" text-anchor="end" font-family="IBM Plex Mono" font-size="5" fill="#B5613B">urban</text>
        </svg>
        <div class="txt">Cadastral <b>${fmt(p.cadArea)} m²</b> → urban <b>${fmt(p.planArea)} m²</b>. <span class="vsdelta">−${delta}%</span> taken for roads / public space.</div>
      </div>` : `
      <div class="upcard none">
        <div class="upn">Not defined</div>
        <div class="upd">The adopted plan defines no urban parcel over this cadastral parcel, so building rights cannot be read directly. An expert analysis is needed to establish what is possible here.</div>
      </div>`}
    </div>
  </div>

  <div class="ctastack">
    <button class="cta gold" onclick="openOrder()">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M2 3h12v10H2z" stroke="#fff" stroke-width="1.3"/><path d="M2 6h12M5 9h3" stroke="#fff" stroke-width="1.3"/></svg>
      Order expert analysis <small>€${orderPrice(p).price}</small></button>
    <button class="cta ghost" onclick="openAI('Tell me about cadastral parcel #${parcelNo}')">
      <svg width="15" height="15" viewBox="0 0 15 15" fill="none"><path d="M2 3h11v7H6l-3 2.5V10H2z" stroke="#12211f" stroke-width="1.2" stroke-linejoin="round"/></svg>
      Ask the AI assistant</button>
    <button class="cta line" onclick="openMethodology(0)">
      <svg width="15" height="15" viewBox="0 0 15 15" fill="none"><circle cx="3" cy="3" r="1.6" stroke="currentColor" stroke-width="1.2"/><circle cx="3" cy="12" r="1.6" stroke="currentColor" stroke-width="1.2"/><path d="M3 4.6v5.8M6.5 3H13M6.5 7.5H13M6.5 12H13" stroke="currentColor" stroke-width="1.2"/></svg>
      How we analyze this parcel</button>
  </div>`;
}

/* ---------- parcel panel router ---------- */
function renderPanel(){
  const p=STATE.sel; if(!p){renderPanelEmpty();return;}
  if(!(STATE.selMode==='urban' && p.up)){ renderPanelCadastral(); return; }
  renderPanelUrban();
}
function renderPanelUrban(){
  const p=STATE.sel; if(!p){renderPanelEmpty();return;}
  const z=ZONES.find(z=>z.id===p.zone);
  const t=ZTYPES[p.ztype];
  const f=feasibility(p,z,STATE.assume);
  const delta=Math.round((1-p.planArea/p.cadArea)*100);
  const parcelNo = p.num + (p.sub?('/'+p.sub):'');

  $('#panel').innerHTML=`
  <div class="pscroll">
    <div class="phead">
      <button class="pclose" onclick="clearSelection()">✕</button>
      <div class="peyebrow"><span class="tag">URBAN PARCEL</span> ${t.name}</div>
      <div class="ptitle">${p.up}</div>
      <div class="psub">${z.name} · ${p.katOp}</div>
    </div>

    <div class="backlink" onclick="selectParcel(${p.id},'cad')">← cadastral parcel #${parcelNo}</div>

    <!-- identity -->
    <div class="idgrid">
      <div class="idcell"><div class="k">Urban parcel</div><div class="v">${p.up}</div></div>
      <div class="idcell"><div class="k">Cadastral parcel</div><div class="v">${parcelNo}</div></div>
      <div class="idcell"><div class="k">Cadastral municipality</div><div class="v">${p.katOp}</div></div>
      <div class="idcell"><div class="k">Urban block</div><div class="v">${p.block}</div></div>
      <div class="idcell full"><div class="k">Governing document</div><div class="v" style="font-size:11px">${(DOCS[z.doc]||[{n:'—'}])[0].n}</div></div>
    </div>

    <!-- existing vs planned -->
    <div class="sect">
      <div class="secthead"><span class="lbl">Cadastral vs urban parcel</span></div>
      <div class="vscard">
        <svg class="vsmini" viewBox="0 0 66 52">
          <rect x="4" y="4" width="58" height="44" rx="2" fill="none" stroke="#B3A894" stroke-width="1.5"/>
          <rect x="14" y="12" width="40" height="30" rx="2" fill="#B5613B" fill-opacity=".18" stroke="#B5613B" stroke-width="1.4" stroke-dasharray="3 2"/>
          <text x="58" y="50" text-anchor="end" font-family="IBM Plex Mono" font-size="5" fill="#B5613B">planned</text>
        </svg>
        <div class="txt">Cadastral <b>${fmt(p.cadArea)} m²</b> → urban <b>${fmt(p.planArea)} m²</b>. <span class="vsdelta">−${delta}%</span> taken for roads / public space. <br><span style="color:var(--brand-dark);font-weight:600">All calculations use the urban parcel area.</span></div>
      </div>
    </div>

    <!-- planning params : FREE -->
    <div class="sect">
      <div class="secthead"><span class="lbl">Planning parameters <span class="badge free">Free</span></span>
        <span class="srcref" title="Traceable to source document"><svg width="10" height="11" viewBox="0 0 10 11" fill="none"><path d="M1 1h5l3 3v6H1z" stroke="currentColor" stroke-width="1" stroke-linejoin="round"/><path d="M6 1v3h3" stroke="currentColor" stroke-width="1"/></svg>source</span></div>
      <div class="prow"><span class="pk">Land use designation</span><span class="pv" style="font-size:11.5px">${z.use.split('—')[0].trim()}</span></div>
      <div class="prow"><span class="pk">Max building height</span><span class="pv">${z.floors}</span></div>
      <div class="prow"><span class="pk">Max site coverage (IZ)</span><span class="pv">${z.cov}<span class="u">%</span></span></div>
      <div class="prow"><span class="pk">Floor Area Ratio (II)</span><span class="pv">${z.far.toFixed(1)}</span></div>
      <div class="prow"><span class="pk">Planned parcel area</span><span class="pv">${fmt(p.planArea)}<span class="u">m²</span></span></div>
      <div class="prow"><span class="pk">Max Gross Floor Area</span><span class="pv" style="color:var(--brand-dark)">${fmt(f.gfa)}<span class="u">m²</span></span></div>
      <div class="prow"><span class="pk">Max coverage area</span><span class="pv">${fmt(f.coverage)}<span class="u">m²</span></span></div>
    </div>

    <!-- market data : PAID -->
    <div class="sect">
      <div class="secthead"><span class="lbl">Market data & feasibility <span class="badge paid"><svg width="9" height="9" viewBox="0 0 12 12" fill="none" style="margin-top:-1px"><rect x="2" y="5.4" width="8" height="5.4" rx="1.2" fill="currentColor"/><path d="M4 5.4V4a2 2 0 014 0v1.4" stroke="currentColor" stroke-width="1.3"/></svg>Subscription</span></span></div>
      ${STATE.paidMarket ? marketHTML(f,p,z) : lockedMarketHTML(f)}
    </div>
  </div>

  <div class="ctastack">
    <button class="cta gold" onclick="openOrder()">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M2 3h12v10H2z" stroke="#fff" stroke-width="1.3"/><path d="M2 6h12M5 9h3" stroke="#fff" stroke-width="1.3"/></svg>
      Order expert analysis <small>€${orderPrice(p).price}</small></button>
    <button class="cta ghost" onclick="openAI('Tell me about urban parcel ${p.up}')">
      <svg width="15" height="15" viewBox="0 0 15 15" fill="none"><path d="M2 3h11v7H6l-3 2.5V10H2z" stroke="#12211f" stroke-width="1.2" stroke-linejoin="round"/></svg>
      Ask the AI assistant</button>
    <button class="cta line" onclick="openMethodology(0)">
      <svg width="15" height="15" viewBox="0 0 15 15" fill="none"><circle cx="3" cy="3" r="1.6" stroke="currentColor" stroke-width="1.2"/><circle cx="3" cy="12" r="1.6" stroke="currentColor" stroke-width="1.2"/><path d="M3 4.6v5.8M6.5 3H13M6.5 7.5H13M6.5 12H13" stroke="currentColor" stroke-width="1.2"/></svg>
      How we analyze this parcel</button>
  </div>`;
}

/* parameters the engine computes — names and units stay readable, values are locked */
const MARKET_PARAMS = [
  { k:'Estimated land value',      u:'€',   d:'urban parcel area × zone land rate' },
  { k:'Construction cost',         u:'€',   d:'gross floor area × build rate' },
  { k:'Design & documentation',    u:'€',   d:'gross floor area × design rate' },
  { k:'Estimated market value',    u:'€',   d:'saleable area × sale rate' },
  { k:'Estimated saleable area',   u:'m²',  d:'gross floor area × saleable share' },
  { k:'Potential profit',          u:'€',   d:'market value − total cost' },
  { k:'Return on investment',      u:'%',   d:'profit ÷ total cost, as a range' }
];
function lockedMarketHTML(f){
  return `
  <p style="font-size:11.5px;color:var(--ink-2);margin:-4px 0 10px;line-height:1.5">
    These are the parameters UrbanView calculates for this urban parcel. The figures behind them are part of the market-data subscription.</p>
  <div class="lockedlist">
    ${MARKET_PARAMS.map(m=>`
      <div class="lrow">
        <span class="lk2">${m.k}<span class="ld">${m.d}</span></span>
        <span class="lv"><span class="lockval"><svg width="9" height="9" viewBox="0 0 12 12" fill="none"><rect x="2" y="5.4" width="8" height="5.4" rx="1.2" fill="currentColor"/><path d="M4 5.4V4a2 2 0 014 0v1.4" stroke="currentColor" stroke-width="1.3"/></svg>LOCKED</span><span class="lu">${m.u}</span></span>
      </div>`).join('')}
  </div>
  ${engineStripHTML()}
  <div class="lockcta">
    <div class="lki"><svg width="16" height="16" viewBox="0 0 18 18" fill="none"><rect x="3" y="8" width="12" height="8" rx="1.5" stroke="currentColor" stroke-width="1.5"/><path d="M6 8V5a3 3 0 016 0v3" stroke="currentColor" stroke-width="1.5"/></svg></div>
    <div class="lct"><b>Unlock the figures</b><span>Values, ranges and the assumptions sandbox.</span></div>
    <button class="b" onclick="openUpgrade('market')">Unlock →</button>
  </div>`;
}

/* ---------- calculation engine provenance ---------- */
const ENGINE = {
  version: '1.4',
  updated: 'August 2026',
  formulas: [
    ['Gross floor area (GFA)',  'urban parcel area × FAR', 'adopted plan'],
    ['Max coverage area',       'urban parcel area × site coverage %', 'adopted plan'],
    ['Saleable area',           'GFA × saleable share', 'UrbanView completed projects'],
    ['Land value',              'urban parcel area × zone land rate × 0.55', 'transaction data'],
    ['Design & documentation',  'GFA × design rate', 'UrbanView fee schedule'],
    ['Construction cost',       'GFA × build rate', 'contractor quotations'],
    ['Market value',            'saleable area × sale rate', 'listing & sale data'],
    ['Potential profit',        'market value − (land + design + construction)', 'derived'],
    ['Return on investment',    'profit ÷ total cost, banded to a range', 'derived']
  ],
  inputs: [
    ['Adopted planning documents', 'DUP / PUP source PDFs — parameters read per urban parcel'],
    ['Cadastre',                   'parcel geometry, area and ownership status'],
    ['UrbanView project archive',  'realised areas, fees and build rates from our own completed analyses'],
    ['Market sources',             'Monstat, Realitica, Estitor — asking and transaction prices'],
    ['Contractor quotations',      'current build rates by structure type']
  ]
};
function engineStripHTML(){
  return '';
}
window.openEngine=function(){
  openModal(`
    <div class="mhead">
      <div class="mi" style="background:var(--brand-tint);color:var(--brand)"><svg width="20" height="20" viewBox="0 0 20 20" fill="none"><path d="M3 17V11l6-6 6 6M10 3h7v7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
      <div class="mt"><div class="meyebrow" style="color:var(--brand)">Engine v${ENGINE.version} · updated ${ENGINE.updated}</div>
        <h2>How the figures are calculated</h2>
        <p>Every value comes from UrbanView's own formulas applied to the parameters read from the adopted plan. Nothing is generated by a language model.</p></div>
      <button class="x" data-close>✕</button>
    </div>
    <div class="mbody">
      <div class="secthead" style="padding:0 0 8px"><span class="lbl">Formulas</span></div>
      <div class="ftable">
        ${ENGINE.formulas.map(([n,fm,src])=>`<div class="frow2">
          <span class="fn">${n}</span>
          <span class="ff mono">${fm}</span>
          <span class="fs">${src}</span>
        </div>`).join('')}
      </div>
      <div class="secthead" style="padding:16px 0 8px"><span class="lbl">Input data</span></div>
      <div class="itable">
        ${ENGINE.inputs.map(([n,d])=>`<div class="irow"><span class="in2">${n}</span><span class="id2">${d}</span></div>`).join('')}
      </div>
      <div class="growbox">
        <b>The engine gets more precise over time.</b>
        Each completed analysis adds realised areas, fees and build rates back into the input data, and new formulas are added as we cover more structure types and municipalities. Figures are published as ranges, and those ranges narrow as the evidence behind them grows.
      </div>
      <p style="font-size:10.5px;color:var(--ink-2);opacity:.8;margin-top:12px;line-height:1.5">Indicative ranges, not investment advice. Deterministic calculation — the AI assistant reads these figures but never generates them.</p>
    </div>
    <div class="mfoot">
      <span class="fnote">Engine v${ENGINE.version} · ${ENGINE.formulas.length} formulas · ${ENGINE.inputs.length} input sources</span>
      <button class="cta ghost" style="width:auto;padding:0 16px;height:38px" data-close>Close</button>
    </div>`,true);
};

function marketHTML(f,p,z){
  return `
  <div class="roihero">
    <div class="rlab">Return on investment</div>
    <div class="rval">${f.roi.toFixed(0)}%</div>
    <div class="rrange">range ${f.roiLo.toFixed(0)}% — ${f.roiHi.toFixed(0)}% · expected ${f.roi.toFixed(0)}%</div>
    <svg class="spark" width="120" height="60" viewBox="0 0 120 60"><path d="M0,50 C30,48 40,20 60,22 C85,24 95,6 120,4 L120,60 L0,60Z" fill="rgba(255,255,255,.14)"/></svg>
  </div>
  <div style="height:12px"></div>
  ${rangeRow('Estimated land value',f.landBand,'€')}
  ${rangeRow('Construction cost',f.constrBand,'€')}
  <div class="prow"><span class="pk">Design & documentation</span><span class="pv">${eur(f.design)}</span></div>
  ${rangeRow('Estimated market value',f.marketBand,'€')}
  <div class="prow"><span class="pk">Estimated saleable area</span><span class="pv">${fmt(f.saleable)}<span class="u">m²</span></span></div>
  ${rangeRow('Potential profit',f.profitBand,'€')}
  <div class="assum">
    <div class="ah">◐ Test your own assumptions</div>
    <div class="arow"><label>Construction €/m²</label><input type="range" min="500" max="1200" value="${STATE.assume.constr}" oninput="setAssume('constr',this.value)"><span class="av">€${STATE.assume.constr}</span></div>
    <div class="arow"><label>Sale price €/m²</label><input type="range" min="1200" max="3600" value="${STATE.assume.sale}" oninput="setAssume('sale',this.value)"><span class="av">€${STATE.assume.sale}</span></div>
    <div class="arow"><label>Saleable %</label><input type="range" min="55" max="85" value="${STATE.assume.saleable}" oninput="setAssume('saleable',this.value)"><span class="av">${STATE.assume.saleable}%</span></div>
  </div>
  ${engineStripHTML()}
  <p style="font-size:10.5px;color:var(--ink-2);opacity:.8;margin-top:10px;line-height:1.5">Figures are indicative ranges from Realitica, Estitor &amp; Monstat — not investment advice. Deterministic calculation; AI does not generate financial values.</p>`;
}
function rangeRow(label,band,pre){
  const span=band.hi-band.lo, pos=((band.ex-band.lo)/span*100).toFixed(0);
  return `<div class="prow" style="flex-direction:column;align-items:stretch;border-bottom:1px dashed var(--paper-2)">
    <div style="display:flex;justify-content:space-between;align-items:baseline">
      <span class="pk">${label}</span>
      <span class="pv">${eur(band.ex)}</span>
    </div>
    <div class="rangebar"><div class="fill" style="left:12%;right:12%"></div><div class="mark" style="left:${pos}%"></div></div>
    <div style="display:flex;justify-content:space-between;font-family:var(--mono);font-size:9.5px;color:var(--ink-2);opacity:.7;margin-top:3px">
      <span>${eur(band.lo)}</span><span>expected</span><span>${eur(band.hi)}</span></div>
  </div>`;
}

window.setAssume=(k,v)=>{ STATE.assume[k]=+v; renderPanel(); };

/* ============================================================
   AI ASSISTANT (freemium: 3 free / session)
   ============================================================ */
const AISUGGEST=['What can I build here?','Explain FAR and coverage','Why is the planned parcel smaller?','Is this parcel worth developing?'];
let AIMSGS=[{who:'bot',text:"Hi — I'm the UrbanView assistant, trained on Montenegrin planning documents and our own analysis archive. Ask me about any parcel, planning parameter, or plan document. I answer only from UrbanView's approved corpus and cite the source.",cite:null}];

function renderAI(){
  const used=3-STATE.aiQuota;
  $('#aipanel').innerHTML=`
  <div class="aihead">
    <div class="av"><svg width="17" height="17" viewBox="0 0 17 17" fill="none"><path d="M2 3h13v8H7l-3 2.5V11H2z" stroke="#fff" stroke-width="1.3" stroke-linejoin="round"/><circle cx="6" cy="7" r="1" fill="rgba(255,255,255,.14)"/><circle cx="9" cy="7" r="1" fill="#fff"/><circle cx="12" cy="7" r="1" fill="#fff"/></svg></div>
    <div class="t"><h4>UrbanView AI</h4><p>${STATE.paidAI?'Unlimited · subscription':'Custom-trained · cites its sources'}</p></div>
    <button onclick="toggleAI(false)">✕</button>
  </div>
  ${STATE.paidAI?'':`<div class="aiquota">
    <span>${STATE.aiQuota} of 3 free interactions left this session</span>
    <span class="dots">${[0,1,2].map(i=>`<span class="qd${i<used?' used':''}"></span>`).join('')}</span>
  </div>`}
  <div class="aibody" id="aibody"></div>
  <div class="aichips" id="aichips">${AISUGGEST.map(s=>`<button class="aichip" onclick="aiAsk('${s}')">${s}</button>`).join('')}</div>
  <div class="aifoot">
    <input id="aiinput" placeholder="${STATE.aiQuota>0||STATE.paidAI?'Ask about this location…':'Upgrade for unlimited…'}" ${STATE.aiQuota>0||STATE.paidAI?'':'disabled'}>
    <button class="send" onclick="aiSend()"><svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M2 8l12-5-5 12-2-5z" stroke="#fff" stroke-width="1.3" stroke-linejoin="round"/></svg></button>
  </div>`;
  const body=$('#aibody');
  body.innerHTML=AIMSGS.map(m=>`<div class="msg ${m.who}"><div class="b">${m.text}${m.cite?`<span class="cite"><svg width="9" height="10" viewBox="0 0 10 11" fill="none" style="vertical-align:-1px;margin-right:3px"><path d="M1 1h5l3 3v6H1z" stroke="currentColor" stroke-width="1" stroke-linejoin="round"/><path d="M6 1v3h3" stroke="currentColor" stroke-width="1"/></svg>${m.cite}</span>`:''}</div></div>`).join('');
  body.scrollTop=body.scrollHeight;
  const inp=$('#aiinput');
  if(inp) inp.addEventListener('keydown',e=>{if(e.key==='Enter')aiSend();});
}
function toggleAI(show){
  const p=$('#aipanel'), f=$('#aifab');
  const willShow = show!==undefined?show:!p.classList.contains('on');
  p.classList.toggle('on',willShow); f.style.display=willShow?'none':'grid';
  if(willShow) renderAI();
}
window.toggleAI=toggleAI;
window.openAI=(q)=>{ toggleAI(true); if(q) setTimeout(()=>aiAsk(q),250); };

function aiSend(){ const inp=$('#aiinput'); if(inp&&inp.value.trim()){ aiAsk(inp.value.trim()); } }
function aiAsk(q){
  if(!STATE.paidAI && STATE.aiQuota<=0){ openUpgrade('ai'); return; }
  AIMSGS.push({who:'user',text:q});
  if(!STATE.paidAI) STATE.aiQuota--;
  renderAI();
  setTimeout(()=>{ AIMSGS.push(aiReply(q)); updateAIBadge();
    if(!STATE.paidAI&&STATE.aiQuota<=0) AIMSGS.push({who:'bot',text:"You've used your 3 free interactions this session. Upgrade to keep the conversation going with unlimited questions.",cite:null});
    renderAI();
    if(!STATE.paidAI&&STATE.aiQuota<=0){ const c=$('#aichips'); if(c) c.innerHTML=`<button class="aichip" style="border-color:var(--paid);color:var(--paid)" onclick="openUpgrade('ai')">↑ Upgrade for unlimited</button>`; }
  },420);
}
window.aiAsk=aiAsk; window.aiSend=aiSend;
function aiReply(q){
  const p=STATE.sel, z=p?ZONES.find(z=>z.id===p.zone):null;
  const ql=q.toLowerCase();
  if(z&&(ql.includes('build')||ql.includes('what can'))){
    const f=feasibility(p,z,STATE.assume);
    return {who:'bot',text:`On parcel #${p.num}, the adopted plan allows land use "${z.use.split('—')[0].trim()}", up to ${z.floors}, ${z.cov}% site coverage and FAR ${z.far.toFixed(1)}. On the planned area of ${fmt(p.planArea)} m² that's about <b>${fmt(f.gfa)} m² of gross floor area</b>.`,cite:(DOCS[z.doc]||[{n:''}])[0].n};
  }
  if(ql.includes('far')||ql.includes('coverage')){
    return {who:'bot',text:`FAR (Floor Area Ratio, locally II) caps total floor area as a multiple of parcel area. Coverage (IZ) caps the building's footprint as a % of the parcel. ${z?`Here: FAR ${z.far.toFixed(1)}, coverage ${z.cov}%.`:''}`,cite:z?(DOCS[z.doc]||[{n:''}])[0].n:'Planning glossary'};
  }
  if((ql.includes('planned')&&ql.includes('smaller'))||ql.includes('why is the planned')){
    return {who:'bot',text:p?`The plan reparcels land — some of the cadastral parcel is taken for roads and public space. Here the cadastral ${fmt(p.cadArea)} m² becomes a planned ${fmt(p.planArea)} m². You can only build on the planned area, so every calculation uses it.`:`Select a parcel and I can compare its cadastral and planned areas.`,cite:'Existing vs planned'};
  }
  if(ql.includes('worth')||ql.includes('roi')||ql.includes('profit')){
    if(!STATE.paidMarket) return {who:'bot',text:`Whether it's worth developing comes from the market data — land value, build cost, sale value and ROI. That's part of the market-data subscription. I can explain what each figure means for free.`,cite:null};
    const f=feasibility(p,z,STATE.assume);
    return {who:'bot',text:`Indicatively, expected ROI here is about <b>${f.roi.toFixed(0)}%</b> (range ${f.roiLo.toFixed(0)}–${f.roiHi.toFixed(0)}%), on ~${eur(f.profit)} potential profit. Ranges reflect market uncertainty — treat as indicative, not advice.`,cite:'Realitica · Monstat'};
  }
  return {who:'bot',text:p?`For parcel #${p.num} I can explain the planning parameters, the existing-vs-planned difference, or what the plan permits. What would you like to know?`:`Select a parcel on the map and I can walk you through what the plan allows and what it means.`,cite:null};
}
function updateAIBadge(){ const b=$('#aibadge'); if(b){ b.textContent=STATE.paidAI?'∞':STATE.aiQuota+' free'; b.style.background='var(--brand)'; b.style.color='#fff'; } }

/* ============================================================
   MODALS
   ============================================================ */
function openModal(html,wide){
  const o=$('#overlay');
  o.innerHTML=`<div class="modal${wide?' wide':''}">${html}</div>`;
  o.classList.add('on');
  o.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',closeModal));
}
function closeModal(){ $('#overlay').classList.remove('on'); $('#overlay').innerHTML=''; }
window.closeModal=closeModal;

/* ---------- order pricing (prototype: parcel size only) ----------
   ≤ 500 m² → €100 · over 500 m² → €200.
   Later phases can also weigh the planning-document area and other
   drivers of analysis complexity. */
const DELIVERY = '2–5 working days';
function orderPrice(p){
  if(!p) return { area:0, basis:'—', price:100, band:'up to 500 m²' };
  const area = p.up ? p.planArea : p.cadArea;
  return {
    area,
    basis: p.up ? 'urban parcel' : 'cadastral parcel',
    price: area>500 ? 200 : 100,
    band: area>500 ? 'over 500 m²' : 'up to 500 m²'
  };
}
window.setOrderType=function(t){ STATE.orderType=t; openOrder(); };

/* ---------- order expert analysis ---------- */
window.openOrder=function(){
  const p=STATE.sel;
  const loc = p ? `Parcel #${p.num}${p.sub?'/'+p.sub:''} · ${p.katOp}` : 'Selected location';
  const q = orderPrice(p);
  const legal = STATE.orderType==='legal';
  openModal(`
    <div class="mhead">
      <div class="mi" style="background:var(--paid-tint);color:var(--paid)"><svg width="20" height="20" viewBox="0 0 20 20" fill="none"><path d="M3 4h14v12H3z" stroke="currentColor" stroke-width="1.4"/><path d="M3 8h14M6 12h4" stroke="currentColor" stroke-width="1.4"/></svg></div>
      <div class="mt"><div class="meyebrow" style="color:var(--paid)">Pay per service · one-off</div>
        <h2>Order expert analysis</h2>
        <p>A qualified expert produces a site analysis & feasibility study for this parcel — interpretation, hidden risks, development scenarios and benchmarking beyond the automated figures. Delivered by email.</p></div>
      <button class="x" data-close>✕</button>
    </div>
    <div class="mbody">
      <div class="ctx"><span class="ci"><svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M8 1v14M1 8h14" stroke="currentColor" stroke-width="1.2"/></svg></span>
        <div class="cd">Analysing <b>${loc}</b> — carried through automatically, no need to re-enter.</div></div>

      <div class="ordersum">
        <div class="osrow"><span class="osl">Parcel size<em>${q.basis}</em></span><span class="mono">${fmt(q.area)} m²</span></div>
        <div class="osrow"><span class="osl">Analysis fee<em>${q.band}</em></span><span class="mono">€${q.price}</span></div>
        <div class="osrow deliv"><span class="osl">Expected delivery</span><span class="mono">${DELIVERY}</span></div>
      </div>
      <p class="pricenote">Prototype pricing is set by parcel size alone — €100 up to 500 m², €200 above. Later phases can also weigh the planning-document area and other factors affecting the complexity of the analysis.</p>

      <div class="fieldlab">Ordering as</div>
      <div class="seg">
        <button class="segb${legal?'':' on'}" onclick="setOrderType('individual')">Individual</button>
        <button class="segb${legal?' on':''}" onclick="setOrderType('legal')">Legal entity</button>
      </div>

      ${legal ? `
      <div class="frow">
        <div class="field"><label>Company name</label><input placeholder="Company d.o.o."></div>
        <div class="field"><label>PIB / VAT number</label><input placeholder="02345678" class="mono"></div>
      </div>
      <div class="frow">
        <div class="field"><label>Contact person</label><input placeholder="Marko Petrović"></div>
        <div class="field"><label>Telephone</label><input placeholder="+382 …"></div>
      </div>
      <div class="field"><label>Email address</label><input placeholder="office@company.me" type="email"></div>
      <div class="field"><label>Registered address <span class="opt">for the invoice</span></label><input placeholder="Bulevar Svetog Petra Cetinjskog 1, Podgorica"></div>` : `
      <div class="frow">
        <div class="field"><label>First name</label><input placeholder="Marko"></div>
        <div class="field"><label>Last name</label><input placeholder="Petrović"></div>
      </div>
      <div class="frow">
        <div class="field"><label>Telephone</label><input placeholder="+382 …"></div>
        <div class="field"><label>Email address</label><input placeholder="you@email.me" type="email"></div>
      </div>`}

      <div class="methlink" onclick="openMethodology(0)">
        <span class="mli"><svg width="15" height="15" viewBox="0 0 15 15" fill="none"><circle cx="3" cy="3" r="1.6" stroke="currentColor" stroke-width="1.2"/><circle cx="3" cy="12" r="1.6" stroke="currentColor" stroke-width="1.2"/><path d="M3 4.6v5.8M6.5 3H13M6.5 7.5H13M6.5 12H13" stroke="currentColor" stroke-width="1.2"/></svg></span>
        <span class="mlt"><b>How we analyze this parcel</b><span>The six steps this report follows, from locating the parcel to the final package.</span></span>
        <span class="mla">→</span>
      </div>

      <p style="font-size:11px;color:var(--ink-2);line-height:1.5;margin-top:12px">No account needed — guest checkout. You'll get the report and an order reference by email.</p>
    </div>
    <div class="mfoot">
      <span class="fnote">€${q.price} · ${DELIVERY}</span>
      <button class="cta ghost" style="width:auto;padding:0 18px" data-close>Cancel</button>
      <button class="cta gold" style="width:auto;padding:0 22px" onclick="openPay()">Continue to payment →</button>
    </div>`);
};

/* ---------- payment ---------- */
window.openPay=function(){
  const PQ=orderPrice(STATE.sel);
  openModal(`
    <div class="mhead">
      <div class="mi" style="background:var(--brand-tint);color:var(--brand)"><svg width="20" height="20" viewBox="0 0 20 20" fill="none"><rect x="2" y="5" width="16" height="11" rx="1.5" stroke="currentColor" stroke-width="1.4"/><path d="M2 8h16" stroke="currentColor" stroke-width="1.4"/></svg></div>
      <div class="mt"><div class="meyebrow" style="color:var(--brand)">Secure checkout</div>
        <h2>Payment</h2><p>One-off payment in EUR. Card details never touch UrbanView.</p></div>
      <button class="x" data-close>✕</button>
    </div>
    <div class="mbody">
      <div class="paysummary">
        <div class="payline"><span>Expert site &amp; feasibility analysis</span><span class="mono">€${PQ.price}</span></div>
        <div class="payline"><span>Parcel #${STATE.sel?STATE.sel.num:'—'} · ${fmt(PQ.area)} m²</span><span class="mono">${PQ.band}</span></div>
        <div class="payline"><span>Ordering as</span><span class="mono">${STATE.orderType==='legal'?'Legal entity':'Individual'}</span></div>
        <div class="payline"><span>Expected delivery</span><span class="mono">${DELIVERY}</span></div>
        <div class="payline total"><span>Total due</span><span class="mono">€${PQ.price}.00</span></div>
      </div>
      <div class="paycard"><span class="brandmark">Stripe</span><span style="font-size:12px;color:var(--ink-2)">Card · Apple Pay · Google Pay</span><span style="margin-left:auto;font-family:var(--mono);font-size:10px;color:var(--brand)">selected</span></div>
      <div class="paycard" style="opacity:.6"><span class="brandmark">Paddle</span><span style="font-size:12px;color:var(--ink-2)">Merchant of record · handles VAT</span></div>
      <div class="field"><label>Card number</label><input placeholder="4242 4242 4242 4242" class="mono"></div>
      <div class="frow"><div class="field"><label>Expiry</label><input placeholder="12 / 27" class="mono"></div>
        <div class="field"><label>CVC</label><input placeholder="123" class="mono"></div></div>
      <div class="securenote"><svg width="12" height="12" viewBox="0 0 12 12" fill="none"><rect x="2" y="5" width="8" height="6" rx="1" stroke="currentColor" stroke-width="1.1"/><path d="M4 5V3.5a2 2 0 014 0V5" stroke="currentColor" stroke-width="1.1"/></svg> PCI-compliant · provider verification pending (Stripe vs Paddle)</div>
    </div>
    <div class="mfoot">
      <button class="cta ghost" style="width:auto;padding:0 18px" data-close>Cancel</button>
      <button class="cta primary" style="width:auto;padding:0 22px" onclick="orderSuccess()">Pay €${PQ.price}</button>
    </div>`);
};

window.orderSuccess=function(){
  const ref='UV-'+Math.random().toString(36).slice(2,7).toUpperCase();
  openModal(`<div class="mbody"><div class="success">
    <div class="ok"><svg width="30" height="30" viewBox="0 0 30 30" fill="none"><path d="M8 15l5 5 9-11" stroke="#B5613B" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></svg></div>
    <h2>Order confirmed</h2>
    <p>An expert will prepare your site &amp; feasibility analysis and email it within <b>${DELIVERY}</b>. A confirmation is on its way now.</p>
    <div class="orderref">${ref} · Parcel #${STATE.sel?STATE.sel.num:'—'}</div>
  </div></div>
  <div class="mfoot"><button class="cta primary" style="width:auto;padding:0 24px" data-close>Done</button></div>`);
  toast('Order placed — check your email');
};

/* ---------- subscription / upgrade ---------- */
window.openUpgrade=function(focus){
  openModal(`
    <div class="mhead">
      <div class="mi" style="background:var(--ink);color:var(--paper)"><svg width="20" height="20" viewBox="0 0 20 20" fill="none"><path d="M4 16l6-12 6 12M7 12h6" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg></div>
      <div class="mt"><div class="meyebrow" style="color:var(--brand)">Three ways to go deeper</div>
        <h2>Choose your access</h2><p>Planning parameters and exploring the city are always free. These unlock the value layers.</p></div>
      <button class="x" data-close>✕</button>
    </div>
    <div class="mbody">
      <div class="plans">
        <div class="plan">
          <h3>Per-report</h3>
          <div class="pr">€100–200<small>/site</small></div>
          <div class="pd">A one-off, expert-produced feasibility study for a single parcel.</div>
          <ul>${li('Human expert analysis')}${li('Risk & scenarios')}${li('Delivered by email')}${li('No subscription')}</ul>
          <button class="cta gold" style="height:38px;margin-top:12px" onclick="openOrder()">Order a report</button>
        </div>
        <div class="plan ${focus==='market'?'feat':''}">
          ${focus==='market'?'<span class="ptag">Unlocks this</span>':''}
          <h3>Market data</h3>
          <div class="pr">€29<small>/mo</small></div>
          <div class="pd">Land value, build cost, market value, profit & ROI — on every parcel.</div>
          <ul>${li('All financial figures')}${li('Editable assumptions')}${li('Low/expected/high ranges')}${li('Unlimited parcels')}</ul>
          <button class="cta gold" style="height:38px;margin-top:12px" onclick="subscribe('market')">Subscribe</button>
        </div>
        <div class="plan ${focus==='ai'?'feat':''}">
          ${focus==='ai'?'<span class="ptag">Unlocks this</span>':''}
          <h3>AI unlimited</h3>
          <div class="pr">€19<small>/mo</small></div>
          <div class="pd">Unlimited access to the assistant UrbanView trains on its own planning corpus — not a general chatbot.</div>
          <ul>${li('Trained on Montenegrin planning documents')}${li('Answers cite the source PDF and article')}${li('Knows our analysis methodology')}${li('Reads the calculation engine, never invents figures')}${li('Unlimited questions')}</ul>
          <button class="cta primary" style="height:38px;margin-top:12px" onclick="subscribe('ai')">Subscribe</button>
        </div>
      </div>
      <p style="font-size:11px;color:var(--ink-2);text-align:center;margin-top:14px;line-height:1.5">Subscriptions require a free account. Three revenue channels are being tested in the pilot to see which delivers the most value — this is a mockup, no charge.</p>
    </div>`,true);
};
function li(t){return `<li><svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2 7l3 3 7-8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>${t}</li>`;}

window.subscribe=function(which){
  if(which==='market') STATE.paidMarket=true;
  if(which==='ai'){ STATE.paidAI=true; updateAIBadge(); }
  closeModal();
  toast(which==='market'?'Market data unlocked':'AI assistant unlocked — unlimited');
  if(which==='market'){ renderRail(); renderMap(); renderLegend(); }
  if(STATE.sel) renderPanel();
  if(which==='ai') renderAI();
};

/* ============================================================
   METHODOLOGY WIZARD — how a parcel/venture is analysed
   ============================================================ */
const METHOD_STEPS=[
  { t:'Locate the parcel', tag:'Research', phase:'Phase 1 · Research & ownership',
    body:"Searching for the specific cadastral parcel in the urban plans that also contain existing condition and planned condition. First check is if the cadastral parcel is the same geometry and area as planned one - urban parcel. During that research we check the ownership status of the parcel.",
    dia:`<svg width="220" height="118" viewBox="0 0 220 118" fill="none">
      <rect x="14" y="14" width="86" height="90" rx="3" stroke="#B3A894" stroke-width="1.4"/>
      <path d="M14 44h86M14 74h86M44 14v90M72 14v90" stroke="#DDD3C3" stroke-width="1"/>
      <rect x="44" y="44" width="28" height="30" fill="#B5613B" fill-opacity=".16" stroke="#B5613B" stroke-width="1.4"/>
      <path d="M150 30l40 40M150 70l40-40" stroke="#DDD3C3" stroke-width="0"/>
      <circle cx="163" cy="52" r="15" stroke="#B5613B" stroke-width="1.6"/><path d="M174 63l14 14" stroke="#B5613B" stroke-width="1.6" stroke-linecap="round"/>
      <path d="M120 44h20M120 60h14" stroke="#B3A894" stroke-width="1.2"/></svg>` },
  { t:'Extract planning parameters', tag:'Research', phase:'Phase 1 · Read the adopted plan',
    body:"We look for parts from urban planning PDF's that contain specific information:",
    subs:[['a','Planned urban parcels - regulation and its relation to public areas'],
      ['b','Planned urban regulation - Area envisaged for site coverage by the object, building height and number of floors, linear distance from the neighboring parcels and public areas.'],
      ['c','Planned land use within the block or area that has the same markation'],
      ['d','Existing and planned infrastructure utilities - water, sewage, energy infrastructure, heating, access roads etc.']],
    dia:`<svg width="200" height="128" viewBox="0 0 200 128" fill="none">
      <rect x="40" y="8" width="120" height="112" rx="4" fill="#fff" stroke="#B3A894" stroke-width="1.4"/>
      <path d="M56 30h88M56 44h88M56 58h64" stroke="#DDD3C3" stroke-width="1.4"/>
      <rect x="56" y="72" width="88" height="12" fill="#B5613B" fill-opacity=".16" stroke="#B5613B" stroke-width="1.2"/>
      <path d="M56 96h72M56 108h50" stroke="#DDD3C3" stroke-width="1.4"/>
      <circle cx="150" cy="78" r="12" fill="#fff" stroke="#B5613B" stroke-width="1.5"/><path d="M145 78l3 3 6-7" stroke="#B5613B" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>` },
  { t:'2D orthogonal projection', tag:'Design', phase:'Phase 2 · Design',
    body:"After analyzing the parcel and the referent PDF planning document, we start designing the orthogonal projection (2D view from the top) of the object to the parcel, implementing all the rules and regulations.",
    dia:`<svg width="210" height="120" viewBox="0 0 210 120" fill="none">
      <rect x="20" y="14" width="170" height="92" rx="3" stroke="#B3A894" stroke-width="1.4"/>
      <rect x="46" y="34" width="118" height="52" fill="#B5613B" fill-opacity=".14" stroke="#B5613B" stroke-width="1.6"/>
      <path d="M20 34h26M164 34h26M20 86h26M164 86h26" stroke="#B5613B" stroke-width="1" stroke-dasharray="3 3"/>
      <path d="M46 14v20M46 86v20M164 14v20M164 86v20" stroke="#B5613B" stroke-width="1" stroke-dasharray="3 3"/>
      <path d="M76 34v52M106 34v52M136 34v52M46 60h118" stroke="#DDD3C3" stroke-width="1"/></svg>` },
  { t:'3D fitting', tag:'Design', phase:'Phase 2 · Massing in context',
    body:"After the design of the object we start working on 3D view and we start fitting the object into the build environment.",
    dia:`<svg width="210" height="122" viewBox="0 0 210 122" fill="none">
      <path d="M30 92l70-24 70 24-70 24z" fill="#EDEFF2" stroke="#B3A894" stroke-width="1.2"/>
      <path d="M84 66l0-34 32-11 0 34z" fill="#B5613B" fill-opacity=".18" stroke="#B5613B" stroke-width="1.5"/>
      <path d="M84 66l32-11M84 32l32-11" stroke="#B5613B" stroke-width="1.5"/>
      <path d="M116 21l24 8 0 34-24 12" fill="#B5613B" fill-opacity=".1" stroke="#B5613B" stroke-width="1.5"/>
      <path d="M40 84l26-9 0 18-26 9z" fill="#fff" stroke="#B3A894" stroke-width="1.2"/>
      <path d="M150 84l22-8 0 16-22 8z" fill="#fff" stroke="#B3A894" stroke-width="1.2"/></svg>` },
  { t:'Preliminary package & feasibility', tag:'Deliverable', phase:'Phase 3 · Client package',
    body:"This step is the final. After we conclude that it is the design that should be presented to the Client, we prepare preliminary 2D plans, 3D model and comprehensive analysis of real estate development venture.",
    dia:`<svg width="200" height="120" viewBox="0 0 200 120" fill="none">
      <rect x="24" y="30" width="70" height="84" rx="3" fill="#fff" stroke="#B3A894" stroke-width="1.3" transform="rotate(-6 59 72)"/>
      <rect x="34" y="20" width="70" height="84" rx="3" fill="#fff" stroke="#B3A894" stroke-width="1.3"/>
      <path d="M46 40h46M46 54h46M46 68h30" stroke="#DDD3C3" stroke-width="1.3"/>
      <path d="M46 82h46v14H46z" fill="#B5613B" fill-opacity=".14" stroke="#B5613B" stroke-width="1.1"/>
      <path d="M118 96l40-14 0-30-40 14z" fill="#B5613B" fill-opacity=".16" stroke="#B5613B" stroke-width="1.4"/>
      <path d="M118 66l40-14M138 45v52" stroke="#B5613B" stroke-width="1.2"/></svg>` },
  { t:'Offer & design', tag:'Engagement', phase:'Phase 4 · Engagement',
    body:"If the Client decides to go for the venture, we prepare offer and start designing.",
    dia:`<svg width="200" height="118" viewBox="0 0 200 118" fill="none">
      <rect x="48" y="16" width="104" height="86" rx="4" fill="#fff" stroke="#B3A894" stroke-width="1.4"/>
      <path d="M64 34h72M64 48h72M64 62h48" stroke="#DDD3C3" stroke-width="1.3"/>
      <path d="M66 82c8-10 18-10 24 0 6 8 14 6 18-2" stroke="#B5613B" stroke-width="1.8" stroke-linecap="round"/>
      <circle cx="150" cy="86" r="18" fill="#B5613B" fill-opacity=".14" stroke="#B5613B" stroke-width="1.5"/>
      <path d="M143 86l5 5 9-11" stroke="#B5613B" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>` },
];
let METHOD_STEP=0;
window.openMethodology=function(step){ METHOD_STEP=step||0; renderMethodology(); };
window.setMethodStep=function(i){ METHOD_STEP=Math.max(0,Math.min(METHOD_STEPS.length-1,i)); renderMethodology(); };
function renderMethodology(){
  const p=STATE.sel;
  const loc = p ? `Parcel #${p.num}${p.sub?'/'+p.sub:''} · ${p.katOp}` : 'Any parcel';
  const s=METHOD_STEPS[METHOD_STEP];
  const last=METHOD_STEP===METHOD_STEPS.length-1;
  const rail=METHOD_STEPS.map((m,i)=>`
    <div class="mstep${i===METHOD_STEP?' on':''}${i<METHOD_STEP?' done':''}" onclick="setMethodStep(${i})">
      <div class="mn">${i<METHOD_STEP?'✓':i+1}</div>
      <div class="ml"><b>${m.t}</b><span>${m.tag}</span></div>
    </div>`).join('');
  const subs=s.subs?`<div style="height:6px"></div>`+s.subs.map(([k,v])=>`<span class="sub" data-k="${k}">${v}</span>`).join(''):'';
  openModal(`
    <div class="mhead">
      <div class="mi" style="background:var(--brand-tint);color:var(--brand)"><svg width="20" height="20" viewBox="0 0 20 20" fill="none"><circle cx="4" cy="4" r="2" stroke="currentColor" stroke-width="1.4"/><circle cx="4" cy="16" r="2" stroke="currentColor" stroke-width="1.4"/><path d="M4 6v8M8 4h8M8 10h8M8 16h8" stroke="currentColor" stroke-width="1.4"/></svg></div>
      <div class="mt"><div class="meyebrow" style="color:var(--brand)">How we work</div>
        <h2>Our analysis methodology</h2>
        <p>The step-by-step process we follow every time we analyze a parcel — for our services, or under contract to design the object. Context: <b>${loc}</b>.</p></div>
      <button class="x" data-close>✕</button>
    </div>
    <div class="method">
      <div class="mrail">
        <div class="mrail-h">Six steps</div>
        ${rail}
      </div>
      <div class="mpane">
        <div class="peyebrow2">${s.tag}</div>
        <h2>${s.t}</h2>
        <div class="phase">${s.phase}</div>
        <div class="mdiagram">${s.dia}</div>
        <div class="mbodytext">${s.body}${subs}</div>
        <div class="mnav">
          <span class="mcount">Step ${METHOD_STEP+1} / ${METHOD_STEPS.length}</span>
          <div class="mprog"><i style="width:${((METHOD_STEP+1)/METHOD_STEPS.length*100).toFixed(0)}%"></i></div>
          ${METHOD_STEP>0?`<button class="cta ghost" style="width:auto;padding:0 16px;height:38px" onclick="setMethodStep(${METHOD_STEP-1})">← Back</button>`:''}
          ${last
            ? `<button class="cta gold" style="width:auto;padding:0 18px;height:38px" onclick="openOrder()">Order this analysis →</button>`
            : `<button class="cta primary" style="width:auto;padding:0 18px;height:38px" onclick="setMethodStep(${METHOD_STEP+1})">Next step →</button>`}
        </div>
      </div>
    </div>`,true);
}

/* ============================================================
   ADMIN CONSOLE
   ============================================================ */
function renderAdmin(){
  $('#admin').innerHTML=`
  <div class="adminbar">
    <div class="at"><svg width="17" height="17" viewBox="0 0 17 17" fill="none"><circle cx="8.5" cy="8.5" r="6.5" stroke="#12211f" stroke-width="1.4"/><path d="M8.5 3v11M3 8.5h11" stroke="#12211f" stroke-width="1.4"/></svg> Admin console</div>
    <div class="admintabs">
      <button class="active" data-av="over">Overview</button>
      <button data-av="review">AI review queue</button>
      <button data-av="rules">Planning rules</button>
      <button data-av="fin">Financial assumptions</button>
      <button data-av="engine">Calculation engine</button>
      <button data-av="orders">Orders</button>
      <button data-av="data">Data sources</button>
    </div>
    <button class="abtn ghost" style="margin-left:auto" id="adminExit">← Back to map</button>
  </div>
  <div class="adminbody">
    <div class="adminview on" id="av-over">${adminOverview()}</div>
    <div class="adminview" id="av-review">${adminReview()}</div>
    <div class="adminview" id="av-rules">${adminRules()}</div>
    <div class="adminview" id="av-fin">${adminFin()}</div>
    <div class="adminview" id="av-engine">${adminEngine()}</div>
    <div class="adminview" id="av-orders">${adminOrders()}</div>
    <div class="adminview" id="av-data">${adminData()}</div>
  </div>`;
  $('#adminExit').addEventListener('click',()=>toggleAdmin(false));
  $('#admin').querySelectorAll('.admintabs button').forEach(b=>b.addEventListener('click',()=>{
    $('#admin').querySelectorAll('.admintabs button').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    $('#admin').querySelectorAll('.adminview').forEach(v=>v.classList.remove('on'));
    $('#av-'+b.dataset.av).classList.add('on');
  }));
}
function adminOverview(){
  return `<div class="astat-grid">
    <div class="astat"><div class="sl">Parcels ingested</div><div class="sv">${fmt(PARCELS.length)}</div><div class="sd up">▲ Podgorica pilot</div></div>
    <div class="astat"><div class="sl">Planning documents</div><div class="sv">11</div><div class="sd">9 adopted · 2 in progress</div></div>
    <div class="astat"><div class="sl">Pending AI review</div><div class="sv warn">7</div><div class="sd warn">◐ needs expert sign-off</div></div>
    <div class="astat"><div class="sl">Paid orders</div><div class="sv">4 <small>· €600</small></div><div class="sd up">▲ this week</div></div>
  </div>
  <div class="card"><div class="cardhd"><div><h3>Pipeline status</h3><div class="sub">The repeatable ingestion methodology, per district</div></div></div>
    <table class="tbl"><thead><tr><th>District</th><th>Documents</th><th>Extraction</th><th>Expert review</th><th>Live</th></tr></thead><tbody>
    ${[['Stari Aerodrom','3','Done','100%','Yes'],['Centar','2','Done','100%','Yes'],['Konik','2','In progress','60%','Partial'],['Zabjelo','2','Done','100%','Yes'],['Gorica','1','Queued','0%','No'],['Zabjelo 5','1','Done','100%','Yes']]
      .map(r=>`<tr><td>${r[0]}</td><td class="mono">${r[1]}</td><td>${r[2]}</td><td class="mono">${r[3]}</td><td><span class="st ${r[4]==='Yes'?'ok':r[4]==='No'?'pend':'rev'}">${r[4]}</span></td></tr>`).join('')}
    </tbody></table></div>`;
}
function adminReview(){
  const rows=[
    ['Max floors — Blok VII','DUP Podgorica – Blok VII · p.14','P+5+Pk'],
    ['Site coverage — Zona C2','DUP Centar – Zona C2 · tbl.3','55%'],
    ['FAR — Zabjelo 3','DUP Zabjelo 3 · p.9','2.0'],
    ['Land use — Konik Sjever','DUP Konik – Sjever · p.4','Residential'],
  ];
  return `<div class="card"><div class="cardhd"><div><h3>AI extraction — review queue</h3><div class="sub">100% of extracted values need expert approval before they publish</div></div><span class="st pend">7 pending</span></div>
  ${rows.map(r=>`<div class="review-item">
    <div class="rv"><div class="rq">${r[0]}</div><div class="rsrc"><svg width="9" height="10" viewBox="0 0 10 11" fill="none" style="vertical-align:-1px;margin-right:3px"><path d="M1 1h5l3 3v6H1z" stroke="currentColor" stroke-width="1" stroke-linejoin="round"/><path d="M6 1v3h3" stroke="currentColor" stroke-width="1"/></svg>${r[1]}</div></div>
    <span class="rextract">${r[2]}</span>
    <div class="review-acts"><button class="abtn sm" onclick="toast('Approved & published')">Approve</button><button class="abtn sm ghost" onclick="toast('Sent back for edit')">Amend</button></div>
  </div>`).join('')}</div>`;
}
function adminRules(){
  return `<div class="card"><div class="cardhd"><div><h3>Planning rules</h3><div class="sub">Zone parameter sets — each carries its source & verification date</div></div><button class="abtn">+ New rule</button></div>
  <table class="tbl"><thead><tr><th>Zone</th><th>Use</th><th>FAR</th><th>Coverage</th><th>Height</th><th>Source</th><th>Status</th></tr></thead><tbody>
  ${ZONES.map(z=>`<tr><td><b>${z.name.split('—')[0].trim()}</b></td><td>${ZTYPES[z.type].name}</td><td class="mono">${z.far.toFixed(1)}</td><td class="mono">${z.cov}%</td><td class="mono">${z.floors}</td><td class="mono" style="font-size:10.5px">${(DOCS[z.doc]||[{n:'—'}])[0].n}</td><td><span class="st ok">Verified</span></td></tr>`).join('')}
  </tbody></table></div>`;
}
function adminFin(){
  return `<div class="card"><div class="cardhd"><div><h3>Financial assumptions</h3><div class="sub">Benchmarks by district — feed the deterministic engine. Sources: Realitica, Estitor, Monstat</div></div><button class="abtn">Save changes</button></div>
  <table class="tbl"><thead><tr><th>District</th><th>Land €/m²</th><th>Construction €/m²</th><th>Sale €/m²</th><th>Range ±</th></tr></thead><tbody>
  ${[['Stari Aerodrom',900,780,1650],['Centar',1350,860,2450],['Konik',640,720,1180],['Zabjelo',780,760,1420],['Poslovna Istok',1080,800,1980]]
    .map(r=>`<tr><td><b>${r[0]}</b></td><td class="mono">€${r[1]}</td><td class="mono">€${r[2]}</td><td class="mono">€${r[3]}</td><td class="mono">±14%</td></tr>`).join('')}
  </tbody></table></div>`;
}
function adminOrders(){
  return `<div class="card"><div class="cardhd"><div><h3>Expert analysis orders</h3><div class="sub">Manual fulfilment queue</div></div></div>
  <table class="tbl"><thead><tr><th>Ref</th><th>Parcel</th><th>Customer</th><th>Placed</th><th>Status</th><th></th></tr></thead><tbody>
  ${[['UV-7K2QX','A1042','Marko P. · Adria d.o.o.','2h ago','New'],['UV-3M9WL','B2011','Ana V.','Yesterday','In progress'],['UV-8ZQ4T','E5003','BuildCo','2 days ago','Delivered'],['UV-1PX7R','A1017','J. Nikolić','3 days ago','Delivered']]
    .map(r=>`<tr><td class="mono">${r[0]}</td><td class="mono">#${r[1]}</td><td>${r[2]}</td><td>${r[3]}</td><td><span class="st ${r[4]==='Delivered'?'ok':r[4]==='New'?'pend':'rev'}">${r[4]}</span></td><td><button class="abtn sm ghost" onclick="toast('Opening order…')">Open</button></td></tr>`).join('')}
  </tbody></table></div>`;
}
function adminData(){
  return `<div class="card"><div class="cardhd"><div><h3>Data sources</h3><div class="sub">Public official data — version-controlled, licence recorded</div></div><button class="abtn">+ Upload document</button></div>
  <table class="tbl"><thead><tr><th>Source</th><th>Provides</th><th>Format</th><th>Status</th></tr></thead><tbody>
  ${[['eRegistri (lamp.gov.me)','Adopted planning documents','PDF','Linked'],['mondarchitects.com/site-check','Zone & document structure','Web','Linked'],['eKatastar','Ownership & legal burdens','API','Linked'],['eMapa','Spatial & cadastral data','GIS','Linked'],['Geoportal UZN','Cadastral parcels','GIS','Linked'],['Realitica / Estitor','Market listings','Web','Linked'],['Monstat','Prices & cost indices','Table','Linked']]
    .map(r=>`<tr><td><b>${r[0]}</b></td><td>${r[1]}</td><td class="mono">${r[2]}</td><td><span class="st ok">${r[3]}</span></td></tr>`).join('')}
  </tbody></table></div>`;
}
function adminEngine(){
  return `
  <div class="card"><div class="cardhd"><div><h3>Formulas</h3><div class="sub">Calculated outputs the engine derives for every parcel — extend the model over time</div></div><button class="abtn" onclick="addEngineFormula()">+ Add formula</button></div>
  <table class="tbl"><thead><tr><th>Output</th><th>Expression</th><th>Source</th><th>Status</th></tr></thead><tbody>
  ${ENGINE.formulas.map(f=>`<tr><td><b>${f[0]}</b></td><td class="mono" style="font-size:12px">${f[1]}</td><td>${f[2]}</td><td><span class="st ${f[3]?'pend':'ok'}">${f[3]||'Live'}</span></td></tr>`).join('')}
  </tbody></table></div>
  <div class="card"><div class="cardhd"><div><h3>Input data</h3><div class="sub">Datasets the formulas draw on — add new sources to improve accuracy</div></div><button class="abtn" onclick="addEngineInput()">+ Add data input</button></div>
  <table class="tbl"><thead><tr><th>Dataset</th><th>Provides</th><th>Status</th></tr></thead><tbody>
  ${ENGINE.inputs.map(i=>`<tr><td><b>${i[0]}</b></td><td>${i[1]}</td><td><span class="st ${i[2]?'pend':'ok'}">${i[2]||'Connected'}</span></td></tr>`).join('')}
  </tbody></table></div>
  <p style="font-size:12px;color:var(--ink-2);opacity:.85;margin-top:2px">Engine v${ENGINE.version} · ${ENGINE.formulas.length} formulas · ${ENGINE.inputs.length} input sources · last updated ${ENGINE.updated}. New formulas and datasets apply to all future calculations.</p>`;
}
window.refreshEngineView=function(){ const v=$('#av-engine'); if(v) v.innerHTML=adminEngine(); };
window.addEngineFormula=function(){
  openModal(`<div class="mhead"><div class="mt"><div class="meyebrow" style="color:var(--brand)">Calculation engine</div><h2>Add a formula</h2><p>A new calculated output. It applies to every parcel in future calculations.</p></div><button class="x" data-close>✕</button></div>
  <div class="mbody">
    <div class="field"><label>Output name</label><input id="efName" placeholder="e.g. Parking spaces required"></div>
    <div class="field"><label>Expression</label><input id="efExpr" placeholder="e.g. GFA ÷ 60"></div>
    <div class="field"><label>Source <span class="opt">optional</span></label><input id="efSrc" placeholder="e.g. adopted plan"></div>
  </div>
  <div class="mfoot"><button class="cta ghost" style="width:auto;padding:0 18px" data-close>Cancel</button><button class="cta primary" style="width:auto;padding:0 22px" onclick="commitEngineFormula()">Add formula</button></div>`);
};
window.commitEngineFormula=function(){
  const n=$('#efName').value.trim(), e=$('#efExpr').value.trim(), s=$('#efSrc').value.trim()||'custom';
  if(!n||!e){ toast('Name and expression are required'); return; }
  ENGINE.formulas.push([n,e,s,'New']); closeModal(); refreshEngineView(); toast('Formula added to the engine');
};
window.addEngineInput=function(){
  openModal(`<div class="mhead"><div class="mt"><div class="meyebrow" style="color:var(--brand)">Calculation engine</div><h2>Add a data input</h2><p>A new dataset the formulas can draw on.</p></div><button class="x" data-close>✕</button></div>
  <div class="mbody">
    <div class="field"><label>Dataset name</label><input id="eiName" placeholder="e.g. Utility connection costs"></div>
    <div class="field"><label>What it provides</label><input id="eiDesc" placeholder="e.g. per-parcel water, sewage & power hookup rates"></div>
  </div>
  <div class="mfoot"><button class="cta ghost" style="width:auto;padding:0 18px" data-close>Cancel</button><button class="cta primary" style="width:auto;padding:0 22px" onclick="commitEngineInput()">Add data input</button></div>`);
};
window.commitEngineInput=function(){
  const n=$('#eiName').value.trim(), d=$('#eiDesc').value.trim();
  if(!n||!d){ toast('Name and description are required'); return; }
  ENGINE.inputs.push([n,d,'Pending']); closeModal(); refreshEngineView(); toast('Data input added to the engine');
};
function toggleAdmin(show){
  const a=$('#admin');
  const on = show!==undefined?show:!a.classList.contains('on');
  a.classList.toggle('on',on);
  $('#navAdmin').classList.toggle('active',on);
  $('#navMap').classList.toggle('active',!on);
  if(on) renderAdmin();
}

/* ============================================================
   SEARCH
   ============================================================ */
const SEARCH_ITEMS=[
  {t:'Bulevar Save Kovačevića 12',s:'Address · Centar',act:()=>selectParcel(PARCELS.find(p=>p.zone==='B').id)},
  {t:'Parcel #1042',s:'Cadastral ref · Podgorica I',act:()=>selectParcel(PARCELS.find(p=>p.zone==='A').id)},
  {t:'Ulica Slobode 8',s:'Address · Stari Aerodrom',act:()=>selectParcel(PARCELS.find(p=>p.zone==='A').id)},
  {t:'Zabjelo — residential block',s:'Zone',act:()=>selectZone('E')},
  {t:'Parcel #4003',s:'Cadastral ref · Podgorica II',act:()=>selectParcel(PARCELS.find(p=>p.zone==='E').id)},
  {t:'Konik (partial coverage)',s:'Zone',act:()=>selectZone('C')},
  {t:'Industrijska zona bb',s:'Outside coverage · no adopted plan',act:()=>showUncovered()}
];
function wireSearch(){
  const inp=$('#search'), sug=$('#searchsug');
  const render=(q='')=>{
    const items=SEARCH_ITEMS.filter(i=>i.t.toLowerCase().includes(q.toLowerCase())||i.s.toLowerCase().includes(q.toLowerCase()));
    sug.innerHTML=items.map((i,idx)=>`<button data-i="${SEARCH_ITEMS.indexOf(i)}">
      <span class="ico">${i.s.startsWith('Zone')?'▤':i.s.startsWith('Address')?'⌂':i.s.startsWith('Outside')?'⚠':'#'}</span>
      <span><div>${i.t}</div><div class="sub">${i.s}</div></span></button>`).join('')||'<div style="padding:14px;font-size:12px;color:var(--ink-2)">No match. The client will supply available data locations.</div>';
    sug.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>{
      SEARCH_ITEMS[+b.dataset.i].act(); sug.classList.remove('on'); inp.value=SEARCH_ITEMS[+b.dataset.i].t;
    }));
  };
  inp.addEventListener('focus',()=>{render(inp.value);sug.classList.add('on');});
  inp.addEventListener('input',()=>{render(inp.value);sug.classList.add('on');});
  document.addEventListener('click',e=>{ if(!e.target.closest('.searchwrap')) sug.classList.remove('on'); });
  document.addEventListener('keydown',e=>{ if((e.metaKey||e.ctrlKey)&&e.key==='k'){e.preventDefault();inp.focus();} if(e.key==='Escape'){sug.classList.remove('on');closeModal();} });
}
function showUncovered(){
  clearSelection(); $('#panel').classList.add('hidden');
  const w=$('#coverwarn'); w.classList.add('on');
  const u=$('#uncovered'); if(u){u.setAttribute('fill','#d0c9b8');}
  setTimeout(()=>{w.classList.remove('on'); $('#panel').classList.remove('hidden'); if(u)u.setAttribute('fill','#ded9cc');},2600);
}

/* ---------- toast ---------- */
let toastT;
function toast(msg){
  const t=$('#toast');
  t.innerHTML=`<span class="ti">✦</span> ${msg}`;
  t.classList.add('on'); clearTimeout(toastT);
  toastT=setTimeout(()=>t.classList.remove('on'),2600);
}
window.toast=toast; window.clearSelection=clearSelection;

/* ---------- map zoom & pan ---------- */
const ZMIN=0.4, ZMAX=6;
let zoom=1, panX=0, panY=0;
function applyZoom(){
  const g=$('#map'); if(!g)return;
  const wrap=g.parentElement;
  zoom=Math.max(ZMIN,Math.min(ZMAX,zoom));
  if(zoom>=1){
    panX=Math.min(0,Math.max(-wrap.clientWidth*(zoom-1),panX));
    panY=Math.min(0,Math.max(-wrap.clientHeight*(zoom-1),panY));
  } else {
    // zoomed out past the city framing → centre Podgorica within the broader field
    panX=wrap.clientWidth*(1-zoom)/2;
    panY=wrap.clientHeight*(1-zoom)/2;
  }
  g.style.transformOrigin='0 0';
  g.style.transform=`translate(${panX}px,${panY}px) scale(${zoom})`;
  const zl=$('#zlabel'); if(zl) zl.textContent=zoom.toFixed(1)+'×';
}
/* zoom keeping the point under the cursor fixed */
function zoomAt(cx,cy,factor){
  const nz=Math.max(ZMIN,Math.min(ZMAX,zoom*factor));
  const k=nz/zoom;
  panX=cx-k*(cx-panX); panY=cy-k*(cy-panY);
  zoom=nz; applyZoom();
}
function wireMapTools(){
  const wrap=$('#map').parentElement;
  const ctr=fn=>{ fn(wrap.clientWidth/2, wrap.clientHeight/2); };
  $('#zin').addEventListener('click',()=>ctr((x,y)=>zoomAt(x,y,1.4)));
  $('#zout').addEventListener('click',()=>ctr((x,y)=>zoomAt(x,y,1/1.4)));
  $('#zreset').addEventListener('click',()=>{zoom=1;panX=0;panY=0;applyZoom();clearSelection();});
  const home=$('#homePill');
  if(home) home.addEventListener('click',()=>{ zoom=1;panX=0;panY=0;applyZoom(); toast('Centred on Podgorica'); });

  wrap.addEventListener('wheel',e=>{
    e.preventDefault();
    const r=wrap.getBoundingClientRect();
    zoomAt(e.clientX-r.left, e.clientY-r.top, Math.exp(-e.deltaY*0.0018));
  },{passive:false});

  let dragging=false,sx=0,sy=0,ox=0,oy=0;
  wrap.addEventListener('pointerdown',e=>{
    if(e.button!==0)return;
    dragging=true; DRAGGED=false; sx=e.clientX; sy=e.clientY; ox=panX; oy=panY;
  });
  wrap.addEventListener('pointermove',e=>{
    if(!dragging)return;
    const dx=e.clientX-sx, dy=e.clientY-sy;
    if(!DRAGGED && (Math.abs(dx)>3||Math.abs(dy)>3)){
      DRAGGED=true; wrap.classList.add('drag');
      try{ wrap.setPointerCapture(e.pointerId); }catch(_){}   // capture only once a real drag starts,
    }                                                          // so a plain click still reaches the parcel
    if(DRAGGED && zoom>1){ panX=ox+dx; panY=oy+dy; applyZoom(); }
  });
  const end=()=>{ dragging=false; wrap.classList.remove('drag'); };
  wrap.addEventListener('pointerup',end);
  wrap.addEventListener('pointercancel',end);
  wrap.addEventListener('pointerleave',end);
  window.addEventListener('resize',applyZoom);
}

/* ============================================================
   INIT
   ============================================================ */
function init(){
  renderApp();
  renderRail();
  renderMap();
  renderLegend();
  renderPanelEmpty();
  wireSearch();
  wireMapTools();
  updateAIBadge();
  $('#aifab').addEventListener('click',()=>toggleAI(true));
  $('#navAdmin').addEventListener('click',()=>toggleAdmin(true));
  $('#navMap').addEventListener('click',()=>toggleAdmin(false));
  $('#overlay').addEventListener('click',e=>{ if(e.target.id==='overlay') closeModal(); });
  // gentle intro
  setTimeout(()=>toast('Click any parcel to see what can be built'),900);
}
if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',init); else init();

