"""Local browser view + Chrome-extension ingest for xtracker-v2 (stdlib only).

Serves on 127.0.0.1 only:
  GET  /           HTML table
  GET  /api/table  JSON snapshot
  POST /api/ingest batch of scraped posts (optional shared-secret header)
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import urlparse

STATE = {"payload": {"rows": [], "summary": {}, "asof": "", "status": "starting", "flagged": []}}
_LOCK = threading.Lock()
_INGEST_CB: Optional[Callable[[list], dict]] = None
_CLEAR_CB: Optional[Callable[[], dict]] = None
_SECRET = ""


def publish(payload: dict):
    with _LOCK:
        STATE["payload"] = payload


def set_ingest_handler(cb: Callable[[list], dict] | None, secret: str = "", clear_cb: Callable[[], dict] | None = None):
    """Register ingest (+ optional clear) callbacks for the Chrome extension / ops."""
    global _INGEST_CB, _CLEAR_CB, _SECRET
    _INGEST_CB = cb
    _CLEAR_CB = clear_cb
    _SECRET = secret or os.environ.get("XTRACKER_INGEST_SECRET", "")


# Visual target: tg-labs.netlify.app/#watchlist — same phosphor desk, Watchlist layout
# (vhead + filter chips + full-width .dt table). No Method section. Data columns unchanged.
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#000000">
<title>xtracker v2 · Watchlist</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=VT323&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
:root{
  color-scheme:dark;
  --bg:#000; --panel:#020604; --panel2:#04100a; --line:#0d2416; --line2:#1a4a2c;
  --ink:#eafff1; --dim:#8fb39c; --faint:#4f6b59;
  --amber:#39ff88; --amber2:#9dffc3; --gold:#ffe14d; --cyan:#4fd8ff;
  --up:#39ff88; --down:#ff4f4f;
  --num:"VT323","IBM Plex Mono",ui-monospace,Menlo,monospace;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
  --head:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg)}
body{color:var(--ink);font-family:var(--mono);font-size:14px;line-height:1.55;
  text-shadow:0 0 1px rgba(255,255,255,.2)}
body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:50;
  background:repeating-linear-gradient(to bottom,rgba(0,0,0,0) 0,rgba(0,0,0,0) 2px,rgba(0,0,0,.16) 3px),
  radial-gradient(ellipse at center,rgba(0,0,0,0) 65%,rgba(0,0,0,.45) 100%)}
.wrap{max-width:1760px;margin:0 auto;padding:0 max(24px,env(safe-area-inset-left,0px))}
@media (max-width:700px){.wrap{padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}}
.top{position:sticky;top:0;z-index:20;background:rgba(0,0,0,.96);border-bottom:1px solid var(--line2)}
.top .wrap{display:flex;align-items:center;gap:18px;flex-wrap:wrap;padding-block:10px}
.brand{font-family:var(--head);font-weight:600;letter-spacing:.08em;font-size:17px;color:var(--amber);
  text-shadow:0 0 6px rgba(57,255,136,.45);text-transform:uppercase}
.brand .tag{font-weight:400;font-size:11px;color:var(--dim);letter-spacing:.12em;margin-left:10px;text-transform:uppercase}
.stamp{margin-left:auto;font-size:12px;border:1px solid var(--line2);padding:5px 10px;color:var(--dim);max-width:100%}
main.view{padding:18px 0 48px;position:relative;z-index:1;display:grid;grid-template-columns:minmax(0,1fr);gap:12px}
.vhead{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap}
.vhead h1{font-family:var(--head);font-weight:600;color:#fff;text-transform:uppercase;
  text-shadow:0 0 8px rgba(57,255,136,.4);font-size:clamp(22px,2.6vw,32px);letter-spacing:.02em;line-height:1.05;margin:0}
.chips{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.chips .sep{width:1px;height:16px;background:var(--line2);margin:0 2px}
.chip{font:inherit;background:transparent;border:1px solid var(--line2);color:var(--dim);padding:4px 10px;cursor:pointer;font-size:12px}
.chip:hover{border-color:var(--amber);color:var(--amber2)}
.chip[aria-pressed="true"]{color:#000;background:var(--amber);border-color:var(--amber);text-shadow:none}
.kpis{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 14px;padding:6px 0 2px;border-bottom:1px solid var(--line);min-width:0}
.kpi{display:inline-flex;align-items:baseline;gap:6px;white-space:nowrap;min-width:0}
.kpi .k{color:var(--dim);font-size:10px;letter-spacing:.06em;text-transform:uppercase}
.kpi .v{font-family:var(--num);font-size:18px;line-height:1;color:var(--ink)}
.kpi .v.up{color:var(--up)}.kpi .v.down{color:var(--down)}.kpi .v.hi{color:var(--amber)}.kpi .v.mid{color:var(--gold)}.kpi .v.lo{color:var(--down)}
.panel{background:var(--panel);border:1px solid var(--line);min-width:0;margin-top:2px}
.tw{overflow-x:auto}
table.dt{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
.dt th,.dt td{padding:9px 12px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
.dt th{font-family:var(--head);font-weight:600;letter-spacing:.08em;text-transform:uppercase;font-size:11px;
  color:var(--amber);background:#03150b;position:sticky;top:0;cursor:pointer;user-select:none}
.dt th:hover{color:var(--amber2)}
.dt th.sort-on{color:#fff}
.dt tbody tr:hover td{background:rgba(57,255,136,.06)}
.dt td.why{white-space:normal;max-width:min(42vw,640px);min-width:240px;color:var(--dim);line-height:1.4;overflow-wrap:anywhere}
.dt td.poster{white-space:normal;max-width:320px;line-height:1.45}
.dt td.num{font-family:var(--num);font-size:17px;letter-spacing:.02em}
.av{width:28px;height:28px;border-radius:50%;vertical-align:middle;margin-right:6px;object-fit:cover;
  background:var(--line);border:1px solid var(--line2);flex-shrink:0}
.poster-chip{display:inline-flex;align-items:center;gap:2px;margin:2px 10px 2px 0;white-space:nowrap;max-width:100%}
tr.grp td{background:#1a0812;color:#ff7ab8;font-family:var(--head);font-weight:600;letter-spacing:.08em;
  text-transform:uppercase;font-size:11px;padding:7px 12px;border-bottom:1px solid #4a1835}
tr.grp td .n{color:#c48aa8;font-weight:400;margin-left:8px;letter-spacing:.04em}
tr.grp td .best{color:#ffb3d4;font-family:var(--num);font-size:15px;font-weight:400;margin-left:10px;text-transform:none}
tr.sec td{background:#061812;color:var(--cyan);font-family:var(--head);font-weight:600;letter-spacing:.1em;
  text-transform:uppercase;font-size:12px;padding:10px 12px;border-bottom:1px solid #0e3a2c}
tr.sec td .n{color:var(--dim);font-weight:400;margin-left:8px;letter-spacing:.04em}
tr.C td.cp,tr.C td.tk{color:var(--up);font-weight:600}
tr.P td.cp,tr.P td.tk{color:var(--down);font-weight:600}
tr.multi td.tk{text-shadow:0 0 6px rgba(57,255,136,.35)}
.hi{color:var(--amber);font-weight:600}.mid{color:var(--gold)}.lo{color:var(--down);opacity:.9}
.tg-green{color:#39ff88;font-weight:600}
.tg-orange{color:#ff9a3c;font-weight:600}
.tg-yellow{color:#ffe14d;font-weight:600}
tr.tg-green{box-shadow:inset 3px 0 0 #39ff88;background:rgba(57,255,136,.07)}
tr.tg-orange{box-shadow:inset 3px 0 0 #ff9a3c;background:rgba(255,154,60,.07)}
tr.tg-yellow{box-shadow:inset 3px 0 0 #ffe14d;background:rgba(255,225,77,.06)}
tr.tg-hit td.tk{font-weight:700}
.tg-badge{display:inline-block;font-family:var(--head);font-size:10px;font-weight:600;letter-spacing:.08em;
  text-transform:uppercase;padding:1px 6px;margin-right:6px;border:1px solid;line-height:1.4;vertical-align:middle}
.tg-badge.tg-green{color:#03150b;background:#39ff88;border-color:#39ff88;text-shadow:none}
.tg-badge.tg-orange{color:#1a0c00;background:#ff9a3c;border-color:#ff9a3c;text-shadow:none}
.tg-badge.tg-yellow{color:#1a1600;background:#ffe14d;border-color:#ffe14d;text-shadow:none}
a.who{color:inherit}
.ent{color:var(--dim);font-size:12px}
.legend{font-size:11px;letter-spacing:.04em;color:var(--faint)}
.legend .tg,.legend .tg-badge{margin-right:10px}
.tag{color:var(--faint)}.reason{color:var(--gold)}
.flag-tag{color:var(--amber2);font-size:11px;letter-spacing:.04em;margin-left:6px}
a{color:var(--cyan);text-decoration:none}a:hover{color:var(--amber2);text-decoration:underline}
.empty{padding:12px;color:var(--dim);font-size:13px;margin:0}
#st{color:var(--faint);font-size:11px;letter-spacing:.04em;margin:0}
.foot{color:var(--faint);font-size:11px;letter-spacing:.04em;max-width:80ch}
</style>
</head>
<body>
<header class="top"><div class="wrap">
  <div class="brand">xtracker v2<span class="tag">watchlist</span></div>
  <div class="stamp" id="asof">—</div>
</div></header>
<main class="wrap view">
  <div class="vhead">
    <div><h1 id="title">Live contracts</h1></div>
    <div class="chips">
      <div class="chips" id="filters" role="group" aria-label="Scope">
        <button type="button" class="chip" data-f="live" aria-pressed="true">Live</button>
        <button type="button" class="chip" data-f="top" aria-pressed="false">Top</button>
        <button type="button" class="chip" data-f="flagged" aria-pressed="false">Flagged</button>
        <button type="button" class="chip" data-f="all" aria-pressed="false">All</button>
      </div>
      <span class="sep" id="sep-kinds" aria-hidden="true"></span>
      <div class="chips" id="kinds" role="group" aria-label="Ticker kind">
        <button type="button" class="chip" data-kind="both" aria-pressed="true">Both</button>
        <button type="button" class="chip" data-kind="stocks" aria-pressed="false">Stocks</button>
        <button type="button" class="chip" data-kind="etfs" aria-pressed="false">ETFs</button>
      </div>
      <span class="sep" id="sep-layouts" aria-hidden="true"></span>
      <div class="chips" id="layouts" role="group" aria-label="Layout">
        <button type="button" class="chip" data-l="list" aria-pressed="false">Complete list</button>
        <button type="button" class="chip" data-l="group" aria-pressed="true">Group by ticker</button>
      </div>
    </div>
  </div>

  <div class="kpis" id="sum"></div>
  <p id="st"></p>

  <section class="panel" id="live-panel">
    <div class="tw"><table class="dt"><thead><tr>
      <th data-k="time">Time ET</th><th data-k="ticker">Ticker</th><th data-k="type">C/P</th><th data-k="strike">Strike</th>
      <th data-k="expiry">Expiry</th><th data-k="entry">Entry</th><th data-k="opt">Opt last/mid</th><th data-k="underlying">Undl</th>
      <th data-k="dist">Dist %</th><th data-k="posters">#Post</th><th data-k="conf">Conf</th><th data-k="grade">TG</th><th>Poster(s)</th><th>Why / flags</th><th>Post</th>
    </tr></thead><tbody id="tb"></tbody></table></div>
    <p class="empty" id="empty" hidden>No live contracts with a real strike and call/put.</p>
  </section>

  <section class="panel" id="flag-panel" hidden>
    <div class="tw"><table class="dt"><thead><tr>
      <th>Time ET</th><th>TG</th><th>Why</th><th>Poster</th><th>Note</th><th>Post</th>
    </tr></thead><tbody id="fb"></tbody></table></div>
    <p class="empty" id="fempty" hidden>No flagged notes.</p>
  </section>

  <p class="legend">TraderGrader · <span class="tg-badge tg-green">TG #</span> in the TG column + tinted row when listed · poster avatars when known (ingest or unavatar) · <span class="tg tg-green">green</span><span class="tg tg-orange">orange</span><span class="tg tg-yellow">yellow</span> handles. Edit tradergrader.json to add.</p>
  <p class="foot">xtracker-v2 · watchlist desk · auto-refresh every __INT__s · not financial advice</p>
</main>
<script>
let rows=[],flagged=[],sk="conf",asc=false,view="live",savedSort=null;
const LAYOUT_KEY="xtracker-v2-layout";
const KIND_KEY="xtracker-v2-kind";
const ETF_TICKERS=new Set(["SPY","QQQ","IWM","SPX","SPXW","XSP","NDX","RUT","VIX","DIA","TQQQ","SQQQ","UVXY","VXX"]);
let layout=(()=>{try{const v=localStorage.getItem(LAYOUT_KEY);return v==="list"||v==="group"?v:"group"}catch(e){return"group"}})();
let kind=(()=>{try{const v=localStorage.getItem(KIND_KEY);return v==="stocks"||v==="etfs"||v==="both"?v:"both"}catch(e){return"both"}})();
const f=x=>x==null?"-":x;
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function isEtf(t){return ETF_TICKERS.has(String(t||"").toUpperCase())}
function visibleRows(){
  let list=view==="top"?rows.filter(o=>o.band==="green"):rows;
  if(kind==="stocks") list=list.filter(o=>!isEtf(o.ticker));
  else if(kind==="etfs") list=list.filter(o=>isEtf(o.ticker));
  return list;
}
function setView(v,redraw){
  if(v==="top"&&view!=="top"){savedSort={sk,asc};sk="grade";asc=false}
  else if(view==="top"&&v!=="top"&&savedSort){sk=savedSort.sk;asc=savedSort.asc;savedSort=null}
  view=v;
  document.querySelectorAll("#filters .chip").forEach(c=>c.setAttribute("aria-pressed",String(c.dataset.f===v)));
  const live=document.getElementById("live-panel"), fl=document.getElementById("flag-panel");
  const showLive=v==="live"||v==="all"||v==="top", showFl=v==="flagged"||v==="all";
  live.hidden=!showLive; fl.hidden=!showFl;
  document.getElementById("title").textContent=v==="flagged"?"Flagged":v==="all"?"Watchlist":v==="top"?"Top traders":"Live contracts";
  syncLayoutChips();
  if(redraw!==false) draw();
}
function setKind(k){
  kind=(k==="stocks"||k==="etfs")?k:"both";
  try{localStorage.setItem(KIND_KEY,kind)}catch(e){}
  syncLayoutChips();
  draw();
}
function setLayout(l){
  layout=l==="list"?"list":"group";
  try{localStorage.setItem(LAYOUT_KEY,layout)}catch(e){}
  syncLayoutChips();
  draw();
}
function syncLayoutChips(){
  const show=view==="live"||view==="all"||view==="top";
  document.getElementById("layouts").hidden=!show;
  document.getElementById("kinds").hidden=!show;
  document.getElementById("sep-layouts").hidden=!show;
  document.getElementById("sep-kinds").hidden=!show;
  document.querySelectorAll("#layouts .chip").forEach(c=>c.setAttribute("aria-pressed",String(c.dataset.l===layout)));
  document.querySelectorAll("#kinds .chip").forEach(c=>c.setAttribute("aria-pressed",String(c.dataset.kind===kind)));
}
function tgBadge(band,grade){
  if(!band)return"";
  const n=grade!=null?` #${grade}`:"";
  return `<span class="tg-badge tg-${band}" title="TraderGrader ${band}${n}">TG${n}</span>`;
}
function avatarHtml(handle,avatar){
  const h=String(handle||"").replace(/^@/,"").trim();
  if(!h)return"";
  const src=avatar||("https://unavatar.io/twitter/"+encodeURIComponent(h)+"?fallback=false");
  return `<img class="av" src="${esc(src)}" alt="" width="28" height="28" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">`;
}
function posterHtml(o){
  const list=(o.marks&&o.marks.length)?o.marks:(o.handles||[]).map(h=>({handle:h}));
  return list.map(p=>{
    const who=p.band?`<span class="tg tg-${p.band}">@${esc(p.handle)}</span>`:`@${esc(p.handle)}`;
    const name=p.url?`<a class="who" href="${esc(p.url)}" target="_blank" rel="noopener">${who}</a>`:who;
    const ent=p.entry!=null?` <span class="ent">${esc(p.entry)}</span>`:"";
    return `<span class="poster-chip">${avatarHtml(p.handle,p.avatar)}${name}${ent}</span>`;
  }).join("");
}
function rowHtml(o){const cc=o.conf>=75?"hi":o.conf>=50?"mid":"lo";
const tags=(o.flags&&o.flags.length)?` <span class="tag">[${esc(o.flags.join(","))}]</span>`:"";
const tg=o.grade?`<td class="num">${tgBadge(o.band,o.grade)}</td>`:`<td class="num">-</td>`;
const tgCls=o.band?` tg-${o.band} tg-hit`:"";
return `<tr class="${o.type}${o.posters>=2?" multi":""}${tgCls}"><td class="num">${o.time}</td><td class="tk">${esc(o.ticker)}</td><td class="cp">${o.type=="C"?"CALL":"PUT"}</td>
<td class="num">${o.strike}</td><td>${esc(o.expiry)}</td><td class="num">${f(o.entry)}</td><td class="num">${esc(o.opt)}</td><td class="num">${f(o.underlying)}</td><td class="num">${o.dist==null?"-":o.dist.toFixed(2)}</td>
<td class="num">${o.posters}</td><td class="num ${cc}">${o.conf}</td>${tg}<td class="poster">${posterHtml(o)}</td><td class="why">${esc(o.why)}${tags}</td>
<td>${o.url?`<a href="${esc(o.url)}" target="_blank" rel="noopener">post</a>`:""}</td></tr>`}
function cmpVal(a,b,k){let x=a[k],y=b[k];if(k==="ticker"){x=(x||"").toString().toUpperCase();y=(y||"").toString().toUpperCase();return x<y?-1:x>y?1:0}
if(k==="grade"){x=a.grade==null?-1e9:-a.grade;y=b.grade==null?-1e9:-b.grade;return x>y?1:x<y?-1:0}
x=x==null?-1e9:x;y=y==null?-1e9:y;return x>y?1:x<y?-1:0}
function sortRows(list){
  return [...list].sort((a,b)=>{const d=cmpVal(a,b,sk)*(asc?1:-1);return d||cmpVal(b,a,"conf")});
}
function groupRows(list){
  const by={};for(const o of list){const t=(o.ticker||"?").toUpperCase();(by[t]||(by[t]=[])).push(o)}
  const groups=Object.keys(by).map(t=>{
    const items=sortRows(by[t]);
    const best=Math.max(...items.map(i=>i.conf==null?-1e9:i.conf));
    const bestGrade=Math.min(...items.map(i=>i.grade==null?1e9:i.grade));
    return{t,items,best,bestGrade,n:items.length}});
  if(sk==="ticker")groups.sort((a,b)=>asc?a.t.localeCompare(b.t):b.t.localeCompare(a.t));
  else if(sk==="grade")groups.sort((a,b)=>a.bestGrade-b.bestGrade||a.t.localeCompare(b.t));
  else groups.sort((a,b)=>b.best-a.best||a.t.localeCompare(b.t));
  return groups;
}
function renderBucket(parts,label,list,showHeader){
  if(!list.length)return;
  if(showHeader)parts.push(`<tr class="sec"><td colspan="15">${esc(label)}<span class="n">${list.length} contract${list.length===1?"":"s"}</span></td></tr>`);
  if(layout==="list"){for(const o of sortRows(list))parts.push(rowHtml(o));return}
  for(const g of groupRows(list)){
    parts.push(`<tr class="grp"><td colspan="15">${esc(g.t)}<span class="n">${g.n} contract${g.n===1?"":"s"}</span><span class="best">best ${g.best<-1e8?"—":g.best}</span></td></tr>`);
    for(const o of g.items)parts.push(rowHtml(o));
  }
}
function draw(){
const shown=visibleRows();
const empty=document.getElementById("empty");
empty.hidden=shown.length>0;
empty.textContent=view==="top"?(kind==="etfs"?"No green-list ETF posts yet.":kind==="stocks"?"No green-list stock posts yet.":"No posts yet from the green list.")
  :(kind==="etfs"?"No ETF / index contracts yet.":kind==="stocks"?"No stock contracts yet.":"No live contracts with a real strike and call/put.");
document.querySelectorAll("th[data-k]").forEach(th=>th.classList.toggle("sort-on",th.dataset.k===sk));
const parts=[];
const showSplit=kind==="both";
const etfs=shown.filter(o=>isEtf(o.ticker));
const stocks=shown.filter(o=>!isEtf(o.ticker));
if(kind==="etfs")renderBucket(parts,"ETFs / indexes",etfs,false);
else if(kind==="stocks")renderBucket(parts,"Stocks",stocks,false);
else{renderBucket(parts,"ETFs / indexes",etfs,showSplit);renderBucket(parts,"Stocks",stocks,showSplit)}
document.getElementById("tb").innerHTML=parts.join("")}
function drawFlagged(items){
const bandRank={green:0,orange:1,yellow:2};
const list=[...(items||[])].sort((a,b)=>{
  const ar=a.band!=null?bandRank[a.band]??9:9, br=b.band!=null?bandRank[b.band]??9:9;
  if(ar!==br)return ar-br;
  const ag=a.grade==null?1e9:a.grade, bg=b.grade==null?1e9:b.grade;
  if(ag!==bg)return ag-bg;
  return String(b.time||"").localeCompare(String(a.time||""));
});
document.getElementById("fempty").hidden=list.length>0;
document.getElementById("fb").innerHTML=list.map(o=>{
  const tgCls=o.band?` tg-${o.band} tg-hit`:"";
  const tg=o.band?tgBadge(o.band,o.grade):"-";
  const who=o.band?`<span class="tg tg-${o.band}">@${esc(o.handle)}</span>`:"@"+esc(o.handle);
  return `<tr class="${tgCls}"><td class="num">${esc(o.time)}</td><td class="num">${tg}</td><td class="reason">${esc(o.detail||o.label||o.reason)}</td><td class="poster"><span class="poster-chip">${avatarHtml(o.handle,o.avatar)}${who}</span></td><td class="why">${esc(o.text)}</td>
<td>${o.url?`<a href="${esc(o.url)}" target="_blank" rel="noopener">post</a>`:""}</td></tr>`;
}).join("")}
document.querySelectorAll("th[data-k]").forEach(th=>th.onclick=()=>{const k=th.dataset.k;asc=(sk==k)?!asc:false;sk=k;draw()});
document.querySelectorAll("#filters .chip").forEach(ch=>ch.onclick=()=>setView(ch.dataset.f));
document.querySelectorAll("#kinds .chip").forEach(ch=>ch.onclick=()=>setKind(ch.dataset.kind));
document.querySelectorAll("#layouts .chip").forEach(ch=>ch.onclick=()=>setLayout(ch.dataset.l));
async function load(){try{const j=await (await fetch("/api/table",{cache:"no-store"})).json();rows=j.rows||[];flagged=j.flagged||[];
document.getElementById("asof").textContent="as of "+(j.asof||"—")+" ET";const s=j.summary||{};
document.getElementById("sum").innerHTML=`
<div class="kpi"><span class="k">Posts</span><span class="v">${s.posts||0}</span></div>
<div class="kpi"><span class="k">Live calls</span><span class="v">${s.calls||0}</span></div>
<div class="kpi"><span class="k">Contracts</span><span class="v">${s.contracts||0}</span></div>
<div class="kpi"><span class="k">Calls</span><span class="v up">${s.call_contracts||0}</span></div>
<div class="kpi"><span class="k">Puts</span><span class="v down">${s.put_contracts||0}</span></div>
<div class="kpi"><span class="k">Conf ≥75</span><span class="v hi">${s.high||0}</span></div>
<div class="kpi"><span class="k">50–74</span><span class="v mid">${s.medium||0}</span></div>
<div class="kpi"><span class="k">&lt;50</span><span class="v lo">${s.low||0}</span></div>
<div class="kpi"><span class="k">≥2 posters</span><span class="v">${s.multi||0}</span></div>
<div class="kpi"><span class="k">Flagged</span><span class="v mid">${s.flagged||0}</span></div>`;
document.getElementById("st").textContent=(j.status||"")+" · fetched "+new Date().toLocaleTimeString();draw();drawFlagged(flagged)}
catch(e){document.getElementById("st").textContent="fetch error: "+e}}
setView("live",false);load();setInterval(load,__INT__*1000);
</script>
</body></html>"""


def _cors(handler: BaseHTTPRequestHandler):
    # Extension pages and content scripts hit localhost from chrome-extension:// origins.
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, X-Ingest-Secret")


def serve(port=8081, interval=30, host="127.0.0.1"):
    page = PAGE.replace("__INT__", str(interval)).encode()

    class H(BaseHTTPRequestHandler):
        def do_OPTIONS(self):
            self.send_response(204)
            _cors(self)
            self.end_headers()

        def do_GET(self):
            path = urlparse(self.path).path
            if path.startswith("/api/table"):
                with _LOCK:
                    body, ct = json.dumps(STATE["payload"]).encode(), "application/json"
            elif path in ("/", "/index.html"):
                body, ct = page, "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            _cors(self)
            self.send_header("Content-Type", ct)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict):
            body = json.dumps(obj).encode()
            self.send_response(code)
            _cors(self)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth_ok(self) -> bool:
            if not _SECRET:
                return True
            return self.headers.get("X-Ingest-Secret", "") == _SECRET

        def do_POST(self):
            path = urlparse(self.path).path
            if path not in ("/api/ingest", "/api/clear"):
                self.send_error(404)
                return
            if not self._auth_ok():
                self._json(401, {"error": "unauthorized"})
                return
            if path == "/api/clear":
                if _CLEAR_CB is None:
                    self._json(503, {"error": "clear not ready"})
                    return
                try:
                    self._json(200, _CLEAR_CB())
                except Exception as e:
                    self._json(500, {"error": str(e)[:200]})
                return
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid json"})
                return
            posts = data.get("posts") if isinstance(data, dict) else None
            if posts is None and isinstance(data, list):
                posts = data
            if not isinstance(posts, list):
                self._json(400, {"error": 'expected {"posts": [...]}'})
                return
            if _INGEST_CB is None:
                self._json(200, {"accepted": 0, "duplicates": 0, "rejected": len(posts), "error": "ingest not ready"})
                return
            try:
                self._json(200, _INGEST_CB(posts))
            except Exception as e:
                self._json(200, {"accepted": 0, "duplicates": 0, "rejected": len(posts), "error": str(e)[:200]})

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
