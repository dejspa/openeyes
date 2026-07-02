"""Live dashboard — watch and interact with agent browser sessions at localhost:6080."""

from __future__ import annotations

import asyncio
import fcntl
import http.server
import json
import os
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import websockets

CDP_PORT = 9222  # fallback when no session file exists (pre-multisession deployments)
HTTP_PORT = 6080
WS_PORT = 6081
SESSION_FILE = "/tmp/openeyes-web-sessions.json"
HISTORY_ROOT = os.path.expanduser("~/.openeyes/web/history")


@contextmanager
def _session_lock():
    """Same flock the MCP server uses — the session file is shared across processes."""
    lf = open(SESSION_FILE + ".lock", "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()


def _load_sessions() -> dict:
    try:
        with open(SESSION_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _session_port(session_id: str) -> int:
    """Resolve CDP port for a session. Falls back to CDP_PORT for legacy 'default'."""
    data = _load_sessions()
    rec = data.get(session_id)
    if rec and "port" in rec:
        return rec["port"]
    return CDP_PORT

_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>OpenEyes Web</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0f1117;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
.hdr{background:#1a1d27;padding:12px 24px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #2a2d37}
.hdr h1{font-size:16px;font-weight:600}
.st{font-size:13px;color:#888}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.dot.on{background:#4ade80}.dot.off{background:#ef4444}
.grid{padding:16px;display:flex;flex-direction:column;gap:20px}
.session{border:1px solid #2a2d37;border-radius:10px;background:#13151c;overflow:hidden}
.sesshead{padding:10px 16px;background:#1a1d27;border-bottom:1px solid #2a2d37;display:flex;align-items:center;justify-content:space-between;cursor:pointer;user-select:none}
.sesshead:hover{background:#22252f}
.sesshead .sid{font-weight:600;font-size:14px;color:#e0e0e0;display:flex;align-items:center;gap:8px}
.sesshead .sid .caret{font-size:10px;color:#888;transition:transform .15s}
.session.collapsed .sesshead .caret{transform:rotate(-90deg)}
.sesshead .smeta{font-size:12px;color:#888;display:flex;gap:12px}
.sesshead .smeta .act{color:#4ade80}
.sesshead .smeta .idle{color:#f0c040}
.session.collapsed .stiles{display:none}
.stiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(480px,1fr));gap:12px;padding:14px}
.tile{background:#1a1d27;border-radius:8px;overflow:hidden;border:1px solid #2a2d37}
.tile:hover{border-color:#4a9eff}
.th{padding:8px 12px;background:#22252f;font-size:13px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;display:flex;align-items:center;justify-content:space-between}
.th .title{overflow:hidden;text-overflow:ellipsis;flex:1}
.th .hbtn{background:#333;color:#aaa;border:none;border-radius:3px;padding:2px 6px;cursor:pointer;font-size:10px;margin-left:6px}
.th .hbtn:hover{background:#4a9eff;color:#fff}
.th .tkn{color:#4ade80;font-size:11px;margin-left:8px;white-space:nowrap;cursor:pointer}
.th .tkn:hover{color:#fff}
.tmod{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:200;display:none;align-items:center;justify-content:center}
.tmod.on{display:flex}
.tmod .tbox{background:#1a1d27;border-radius:12px;padding:24px;width:520px;max-height:80vh;overflow-y:auto;border:1px solid #2a2d37}
.hmod{position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:200;display:none;flex-direction:column}
.hmod.on{display:flex}
.hmod .hbar{background:#1a1d27;padding:8px 16px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #2a2d37}
.hmod .hbar button{background:#333;color:#fff;border:none;padding:6px 16px;border-radius:4px;cursor:pointer}
.hmod .hbar button:hover{background:#4a9eff}
.hmod .hgrid{flex:1;overflow-y:auto;padding:16px;display:flex;flex-wrap:wrap;gap:12px;align-content:flex-start}
.hmod .hitem{background:#22252f;border-radius:6px;overflow:hidden;width:280px;border:1px solid #2a2d37}
.hmod .hitem img{width:100%;display:block;cursor:pointer}
.hmod .hitem img:hover{opacity:.8}
.hmod .hmeta{padding:4px 8px;font-size:11px;color:#888;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tmod h2{font-size:18px;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center}
.tmod .xbtn{background:none;border:none;color:#888;font-size:20px;cursor:pointer}
.tmod .xbtn:hover{color:#fff}
.tmod table{width:100%;border-collapse:collapse;margin-bottom:16px}
.tmod th{text-align:left;color:#888;font-size:11px;text-transform:uppercase;padding:6px 8px;border-bottom:1px solid #2a2d37}
.tmod td{padding:6px 8px;font-size:13px;border-bottom:1px solid #1f222c}
.tmod .num{text-align:right;font-variant-numeric:tabular-nums}
.tmod .section{color:#888;font-size:12px;margin:16px 0 8px;text-transform:uppercase;letter-spacing:1px}
.tmod .total{font-weight:600;color:#4ade80}
.tmod .cost{color:#f0c040}
.th .close{background:#ef4444;color:#fff;border:none;border-radius:3px;padding:2px 8px;cursor:pointer;font-size:11px;margin-left:8px;opacity:.7}
.th .close:hover{opacity:1}
.tb{position:relative;cursor:pointer;background:#000}
.tb img{width:100%;display:block}
.tb .ov{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.5);opacity:0;transition:opacity .2s;font-size:14px}
.tb:hover .ov{opacity:1}
.fs{position:fixed;inset:0;background:#000;z-index:100;display:none;flex-direction:column}
.fs.on{display:flex}
.bar{background:#1a1d27;padding:8px 16px;display:flex;align-items:center;justify-content:space-between}
.bar button{background:#333;color:#fff;border:none;padding:6px 16px;border-radius:4px;cursor:pointer}
.bar button:hover{background:#4a9eff}
.vp{flex:1;display:flex;align-items:center;justify-content:center;overflow:hidden}
.vp img{max-width:100%;max-height:100%;cursor:crosshair}
.badge{background:#4ade80;color:#000;font-size:10px;padding:2px 6px;border-radius:3px;margin-left:8px;font-weight:600}
.mt{text-align:center;padding:80px 20px;color:#666}
.mt h2{margin-bottom:12px;color:#888}
</style>
</head>
<body>
<div class="hdr"><h1>OpenEyes Web</h1><div style="display:flex;align-items:center;gap:12px"><select id="modelSel" onchange="setActiveModel(this.value)" style="background:#22252f;color:#e0e0e0;border:1px solid #333;padding:4px 8px;border-radius:4px;font-size:12px"></select><div class="st" id="st"><span class="dot off"></span>Connecting...</div></div></div>
<div class="grid" id="grid"></div>
<div class="mt" id="mt"><h2>No active browser sessions</h2><p>Sessions appear when an agent starts browsing.</p></div>
<div class="tmod" id="tmod" onclick="if(event.target===this)closeTmod()">
  <div class="tbox">
    <h2>Token Usage <button class="xbtn" onclick="closeTmod()">&times;</button></h2>
    <div id="tmod-content"></div>
  </div>
</div>
<div class="hmod" id="hmod">
  <div class="hbar"><div id="hmod-title">Screenshot History</div><button onclick="closeHmod()">Close (Esc)</button></div>
  <div class="hgrid" id="hmod-grid"></div>
</div>
<div class="fs" id="fs">
  <div class="bar"><div><span id="fst"></span><span class="badge">LIVE</span></div><button onclick="xfs()">Close (Esc)</button></div>
  <div class="vp"><img id="fsi"/></div>
</div>
<script>
const WS_PORT = __WS_PORT__;
const S={};let fid=null;
const DEFAULT_MODELS=[
  {name:'Fable 5',rate:10},{name:'Opus 4.8',rate:5},{name:'Sonnet 5',rate:3},{name:'Haiku 4.5',rate:1},
  {name:'GPT-4o',rate:2.5},{name:'GPT-4o mini',rate:0.15},{name:'Gemini 2.5 Pro',rate:1.25}
];
let MODELS=DEFAULT_MODELS.map(m=>({...m}));
let activeModel=0;
const userCustomizedModels=!!localStorage.getItem('vp-models');
try{const s=localStorage.getItem('vp-models');if(s)MODELS=JSON.parse(s);}catch(e){}
try{const a=localStorage.getItem('vp-active-model');if(a!==null)activeModel=parseInt(a);}catch(e){}
// Server is the source of truth for pricing unless the user edited rates locally.
if(!userCustomizedModels){
  fetch('/api/models').then(r=>r.json()).then(list=>{
    if(Array.isArray(list)&&list.length){MODELS=list;initModelSel();}
  }).catch(()=>{});
}
function setActiveModel(i){activeModel=parseInt(i);localStorage.setItem('vp-active-model',activeModel);}
function initModelSel(){
  const sel=document.getElementById('modelSel');
  sel.innerHTML=MODELS.map((m,i)=>'<option value="'+i+'"'+(i===activeModel?' selected':'')+'>'+m.name+' ($'+m.rate+'/M)</option>').join('');
}
initModelSel();

async function gsess(){
  try{
    const r=await fetch('/api/sessions');
    return await r.json();
  }catch(e){return[];}
}

async function gt(sessionId){
  try{
    const r=await fetch('/api/tabs?session='+encodeURIComponent(sessionId));
    const t=await r.json();
    if(!Array.isArray(t))return[];
    return t.filter(x=>x.type==='page'&&!x.url.startsWith('chrome')&&!x.parentId);
  }catch(e){return[];}
}

function fmtIdle(sec){
  if(sec==null)return '';
  if(sec<60)return sec+'s idle';
  if(sec<3600)return Math.floor(sec/60)+'m idle';
  if(sec<86400)return Math.floor(sec/3600)+'h idle';
  return Math.floor(sec/86400)+'d idle';
}

function tkey(session,tabId){return session+'/'+tabId;}

function ct(session,t){
  const k=tkey(session,t.id);
  if(S[k])return;
  const wsUrl='ws://'+location.hostname+':'+WS_PORT+'/ws/'+encodeURIComponent(session)+'/'+t.id;
  const ws=new WebSocket(wsUrl);
  const s={ws,t,session,k,n:0,f:null,m:null};S[k]=s;
  ws.onopen=()=>ws.send(JSON.stringify({id:1,method:'Page.startScreencast',
    params:{format:'jpeg',quality:50,maxWidth:1280,maxHeight:900,everyNthFrame:2}}));
  ws.onmessage=e=>{
    const msg=JSON.parse(e.data);
    if(msg.method==='Page.screencastFrame'){
      s.n++;s.f=msg.params.data;s.m=msg.params.metadata;
      const i=document.getElementById('i-'+k);
      if(i)i.src='data:image/jpeg;base64,'+msg.params.data;
      if(fid===k)document.getElementById('fsi').src='data:image/jpeg;base64,'+msg.params.data;
      ws.send(JSON.stringify({id:100+s.n,method:'Page.screencastFrameAck',
        params:{sessionId:msg.params.sessionId}}));
    }
  };
  ws.onclose=()=>{delete S[k];rt(k);};
}

function fmt(n){if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'k';return n.toString();}

function sessContainer(sess){
  let el=document.getElementById('s-'+sess.id);
  if(el){
    el.querySelector('.smeta').innerHTML=
      '<span>port '+(sess.port||'?')+'</span>'+
      '<span class="'+(sess.active?'act':'')+'">'+(sess.active?'active':'inactive')+'</span>'+
      (sess.idle_seconds!=null?'<span class="idle">'+fmtIdle(sess.idle_seconds)+'</span>':'');
    return el.querySelector('.stiles');
  }
  el=document.createElement('div');el.className='session';el.id='s-'+sess.id;
  const isCollapsed=localStorage.getItem('vp-collapsed-'+sess.id)==='1';
  if(isCollapsed)el.classList.add('collapsed');
  const hd=document.createElement('div');hd.className='sesshead';
  hd.onclick=function(){
    el.classList.toggle('collapsed');
    localStorage.setItem('vp-collapsed-'+sess.id,el.classList.contains('collapsed')?'1':'0');
  };
  const sid=document.createElement('div');sid.className='sid';
  sid.innerHTML='<span class="caret">▼</span>session: '+sess.id;
  const meta=document.createElement('div');meta.className='smeta';
  meta.innerHTML=
    '<span>port '+(sess.port||'?')+'</span>'+
    '<span class="'+(sess.active?'act':'')+'">'+(sess.active?'active':'inactive')+'</span>'+
    (sess.idle_seconds!=null?'<span class="idle">'+fmtIdle(sess.idle_seconds)+'</span>':'');
  hd.appendChild(sid);hd.appendChild(meta);
  const tiles=document.createElement('div');tiles.className='stiles';
  el.appendChild(hd);el.appendChild(tiles);
  document.getElementById('grid').appendChild(el);
  return tiles;
}

function mt(session,t,container){
  const k=tkey(session,t.id);
  let el=document.getElementById('t-'+k);
  if(el){el.querySelector('.title').textContent=t.title||t.url;return;}
  el=document.createElement('div');el.className='tile';el.id='t-'+k;
  const hdr=document.createElement('div');hdr.className='th';
  const title=document.createElement('span');title.className='title';title.textContent=t.title||t.url;
  const tkn=document.createElement('span');tkn.className='tkn';tkn.id='tk-'+k;
  tkn.onclick=function(e){e.stopPropagation();openTmod();};
  const hbtn=document.createElement('button');hbtn.className='hbtn';hbtn.textContent='History';
  hbtn.onclick=function(e){e.stopPropagation();openHmod(t.url,t.title);};
  const cbtn=document.createElement('button');cbtn.className='close';cbtn.textContent='Close';
  cbtn.onclick=function(e){e.stopPropagation();ctab(session,t.id);};
  hdr.appendChild(title);hdr.appendChild(tkn);hdr.appendChild(hbtn);hdr.appendChild(cbtn);
  const body=document.createElement('div');body.className='tb';
  body.onclick=function(){ofs(k);};
  const img=document.createElement('img');img.id='i-'+k;
  const ov=document.createElement('div');ov.className='ov';ov.textContent='Click to interact';
  body.appendChild(img);body.appendChild(ov);
  el.appendChild(hdr);el.appendChild(body);
  container.appendChild(el);
}
function rt(k){const x=document.getElementById('t-'+k);if(x)x.remove();ue();}
function rs(sessId){const x=document.getElementById('s-'+sessId);if(x)x.remove();}

function sm(k,img,ev,ty){
  const s=S[k];if(!s||!s.m)return;
  const r=img.getBoundingClientRect();
  const x=Math.round((ev.clientX-r.left)*s.m.deviceWidth/r.width);
  const y=Math.round((ev.clientY-r.top)*s.m.deviceHeight/r.height);
  s.ws.send(JSON.stringify({id:200,method:'Input.dispatchMouseEvent',
    params:{type:ty,x,y,button:'left',clickCount:1}}));
}

function ofs(k){
  fid=k;const s=S[k];
  document.getElementById('fs').classList.add('on');
  document.getElementById('fst').textContent=(s?.session?'['+s.session+'] ':'')+(s?.t?.title||'');
  const img=document.getElementById('fsi');
  if(s?.f)img.src='data:image/jpeg;base64,'+s.f;
  img.onmousedown=e=>sm(k,img,e,'mousePressed');
  img.onmouseup=e=>sm(k,img,e,'mouseReleased');
  document.onkeydown=e=>{
    if(e.key==='Escape'){xfs();return;}
    const s=S[fid];if(!s)return;
    s.ws.send(JSON.stringify({id:300,method:'Input.dispatchKeyEvent',
      params:{type:'keyDown',key:e.key,code:e.code,windowsVirtualKeyCode:e.keyCode}}));
    if(e.key.length===1)s.ws.send(JSON.stringify({id:301,method:'Input.dispatchKeyEvent',
      params:{type:'char',text:e.key}}));
    e.preventDefault();
  };
  document.onkeyup=e=>{
    const s=S[fid];if(!s)return;
    s.ws.send(JSON.stringify({id:302,method:'Input.dispatchKeyEvent',
      params:{type:'keyUp',key:e.key,code:e.code,windowsVirtualKeyCode:e.keyCode}}));
    e.preventDefault();
  };
}
function xfs(){fid=null;document.getElementById('fs').classList.remove('on');document.onkeydown=null;document.onkeyup=null;}
function ctab(session,tabId){
  if(!confirm('Close this tab?'))return;
  const k=tkey(session,tabId);
  fetch('/api/close/'+encodeURIComponent(session)+'/'+tabId,{method:'POST'}).then(()=>{
    const s=S[k];if(s&&s.ws)s.ws.close();
    delete S[k];rt(k);
  });
}
function ue(){document.getElementById('mt').style.display=document.getElementById('grid').children.length?'none':'block';}

function closeHmod(){document.getElementById('hmod').classList.remove('on');}
async function openHmod(url,title){
  document.getElementById('hmod').classList.add('on');
  document.getElementById('hmod-title').textContent='History: '+(title||url);
  const grid=document.getElementById('hmod-grid');
  grid.innerHTML='Loading...';
  try{
    const r=await fetch('/api/screenshot-history');
    const entries=await r.json();
    // Filter by URL domain
    const domain=url.replace(/https?:\/\//,'').split('/')[0].replace('www.','');
    const filtered=entries.filter(e=>{
      const d=e.url.replace(/https?:\/\//,'').split('/')[0].replace('www.','');
      return d===domain;
    }).sort((a,b)=>b.ts.localeCompare(a.ts));
    if(!filtered.length){grid.innerHTML='<div style="color:#666;padding:40px">No screenshots recorded yet for this tab.</div>';return;}
    grid.innerHTML='';
    for(const e of filtered){
      const item=document.createElement('div');item.className='hitem';
      const img=document.createElement('img');
      img.src='/screenshots/'+e._dir+'/'+e.file;
      img.loading='lazy';
      img.onclick=function(){window.open(img.src,'_blank');};
      const meta=document.createElement('div');meta.className='hmeta';
      const t=new Date(e.ts);
      meta.textContent=t.toLocaleString()+' — '+e.url.slice(0,60);
      item.appendChild(img);item.appendChild(meta);
      grid.appendChild(item);
    }
  }catch(ex){grid.innerHTML='Failed to load history.';}
}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeHmod();});


function closeTmod(){document.getElementById('tmod').classList.remove('on');}
function saveModels(){
  const rows=document.querySelectorAll('.mrow');
  MODELS=[];
  rows.forEach(r=>{
    const n=r.querySelector('.mname').value.trim();
    const v=parseFloat(r.querySelector('.mrate').value);
    if(n&&v>0)MODELS.push({name:n,rate:v});
  });
  localStorage.setItem('vp-models',JSON.stringify(MODELS));
  initModelSel();
  openTmod();
}
function renderModelConfig(){
  let h='<table>';
  for(const m of MODELS){
    h+='<tr class="mrow"><td><input class="mname" value="'+m.name+'" style="background:#22252f;border:1px solid #333;color:#e0e0e0;padding:3px 6px;border-radius:3px;width:100px"></td>';
    h+='<td class="num"><input class="mrate" type="number" step="0.1" value="'+m.rate+'" style="background:#22252f;border:1px solid #333;color:#e0e0e0;padding:3px 6px;border-radius:3px;width:70px;text-align:right"></td>';
    h+='<td><button onclick="this.closest(&quot;tr&quot;).remove()" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:16px">&times;</button></td></tr>';
  }
  h+='</table>';
  h+='<div style="margin-top:8px"><button onclick="addModelRow()" style="background:#333;color:#fff;border:none;padding:4px 12px;border-radius:3px;cursor:pointer;margin-right:8px">+ Add</button>';
  h+='<button onclick="saveModels()" style="background:#4ade80;color:#000;border:none;padding:4px 12px;border-radius:3px;cursor:pointer;font-weight:600">Save</button></div>';
  return h;
}
function addModelRow(){
  const table=document.querySelector('.mrow')?.closest('table');
  if(!table)return;
  const tr=document.createElement('tr');tr.className='mrow';
  tr.innerHTML='<td><input class="mname" value="" placeholder="Model" style="background:#22252f;border:1px solid #333;color:#e0e0e0;padding:3px 6px;border-radius:3px;width:100px"></td><td class="num"><input class="mrate" type="number" step="0.1" value="1" style="background:#22252f;border:1px solid #333;color:#e0e0e0;padding:3px 6px;border-radius:3px;width:70px;text-align:right"></td><td><button onclick="this.closest(&quot;tr&quot;).remove()" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:16px">&times;</button></td>';
  table.appendChild(tr);
}
async function openTmod(){
  document.getElementById('tmod').classList.add('on');
  const el=document.getElementById('tmod-content');
  el.innerHTML='Loading...';
  try{
    const r=await fetch('/api/token-history');
    const entries=await r.json();
    const now=new Date();
    const todayStr=now.toISOString().slice(0,10);
    const monthStr=now.toISOString().slice(0,7);
    let todayTok=0,monthTok=0,allTok=0,todayCost=0,monthCost=0,allCost=0;
    const byDay={},byUrl={},byModel={};
    const modelRates=Object.fromEntries(MODELS.map(m=>[m.name.toLowerCase().replace(/[^a-z0-9.-]/g,''),m.rate]));
    function findRate(m){
      if(!m||m==='unknown')return 0;
      const k=m.toLowerCase();
      for(const[name,rate]of Object.entries(modelRates)){if(k.includes(name)||name.includes(k))return rate;}
      return 0;
    }
    for(const e of entries){
      const day=e.ts.slice(0,10);
      const mo=e.ts.slice(0,7);
      const rate=findRate(e.model);
      const c=e.tokens*rate/1e6;
      allTok+=e.tokens;allCost+=c;
      if(day===todayStr){todayTok+=e.tokens;todayCost+=c;}
      if(mo===monthStr){monthTok+=e.tokens;monthCost+=c;}
      byDay[day]=byDay[day]||{tokens:0,cost:0};byDay[day].tokens+=e.tokens;byDay[day].cost+=c;
      const host=e.url.replace(/https?:\\/\\//,'').split('/')[0];
      byUrl[host]=byUrl[host]||{tokens:0,cost:0};byUrl[host].tokens+=e.tokens;byUrl[host].cost+=c;
      const mn=e.model||'unknown';
      byModel[mn]=byModel[mn]||{tokens:0,cost:0};byModel[mn].tokens+=e.tokens;byModel[mn].cost+=c;
    }
    const cost=(v)=>'$'+v.toFixed(4);
    let h='<div class="section">Summary</div><table>';
    h+='<tr><th>Period</th><th class="num">Tokens</th><th class="num">Actual Cost</th></tr>';
    h+='<tr><td>Today</td><td class="num total">'+fmt(todayTok)+'</td><td class="num cost">'+cost(todayCost)+'</td></tr>';
    h+='<tr><td>This month</td><td class="num total">'+fmt(monthTok)+'</td><td class="num cost">'+cost(monthCost)+'</td></tr>';
    h+='<tr><td>All time</td><td class="num total">'+fmt(allTok)+'</td><td class="num cost">'+cost(allCost)+'</td></tr>';
    h+='</table>';
    h+='<div class="section">By model</div><table>';
    h+='<tr><th>Model</th><th class="num">Tokens</th><th class="num">Rate</th><th class="num">Cost</th></tr>';
    for(const[mn,d]of Object.entries(byModel).sort((a,b)=>b[1].tokens-a[1].tokens)){
      const r=findRate(mn);
      h+='<tr><td>'+mn+'</td><td class="num">'+fmt(d.tokens)+'</td><td class="num">$'+r+'/M</td><td class="num cost">'+cost(d.cost)+'</td></tr>';
    }
    h+='</table>';
    h+='<div class="section">By site (this month)</div><table>';
    h+='<tr><th>Site</th><th class="num">Tokens</th><th class="num">Cost</th></tr>';
    const sorted=Object.entries(byUrl).sort((a,b)=>b[1].tokens-a[1].tokens);
    for(const[site,d]of sorted.slice(0,15)){
      h+='<tr><td>'+site+'</td><td class="num">'+fmt(d.tokens)+'</td><td class="num cost">'+cost(d.cost)+'</td></tr>';
    }
    h+='</table>';
    h+='<div class="section">Daily (last 14 days)</div><table>';
    h+='<tr><th>Date</th><th class="num">Tokens</th><th class="num">Cost</th></tr>';
    const days=Object.entries(byDay).sort((a,b)=>b[0].localeCompare(a[0]));
    for(const[day,d]of days.slice(0,14)){
      h+='<tr><td>'+day+'</td><td class="num">'+fmt(d.tokens)+'</td><td class="num cost">'+cost(d.cost)+'</td></tr>';
    }
    h+='</table>';
    h+='<div class="section">Model Pricing (edit rates)</div>';
    h+=renderModelConfig();
    h+='<div style="color:#555;font-size:11px;margin-top:12px">Cost based on actual model used per call (reported via set_model tool). Agents call set_model() once at session start.</div>';
    el.innerHTML=h;
  }catch(e){el.innerHTML='Failed to load token history.';}
}

async function poll(){
  const sessions=await gsess();
  let connected=false,totalTabs=0;
  const allKeys=new Set();
  const liveSessionIds=new Set();
  // Fetch tabs per session in parallel
  const results=await Promise.all(sessions.map(async s=>({s,tabs:await gt(s.id)})));
  for(const{s,tabs} of results){
    liveSessionIds.add(s.id);
    const tiles=sessContainer(s);
    if(tabs.length)connected=true;
    totalTabs+=tabs.length;
    for(const t of tabs){
      const k=tkey(s.id,t.id);
      allKeys.add(k);
      mt(s.id,t,tiles);
      ct(s.id,t);
    }
  }
  // Remove tiles for tabs that are gone
  for(const k of Object.keys(S)){if(!allKeys.has(k)){const ws=S[k].ws;if(ws)try{ws.close();}catch(e){}delete S[k];rt(k);}}
  // Remove sessions that no longer exist
  document.querySelectorAll('.session').forEach(el=>{
    const id=el.id.slice(2);if(!liveSessionIds.has(id))rs(id);
  });
  document.getElementById('st').innerHTML=connected||sessions.length
    ?'<span class="dot on"></span>'+sessions.length+' session(s) · '+totalTabs+' tab(s)'
    :'<span class="dot off"></span>No active sessions';
  ue();
  // Token stats per tab (URL match)
  try{
    const r=await fetch('/api/tokens');
    const stats=await r.json();
    for(const{s,tabs} of results){
      for(const t of tabs){
        const k=tkey(s.id,t.id);
        const el=document.getElementById('tk-'+k);
        if(!el)continue;
        const st=stats.find(x=>x.url===t.url&&x.session===s.id);
        if(st&&st.tokens){
          const model=st.model||'unknown';
          const rateKey=model.toLowerCase();
          const RATES=Object.fromEntries(MODELS.map(m=>[m.name.toLowerCase().replace(/[^a-z0-9.-]/g,''),m.rate]));
          let rate=0;
          for(const[kk,vv]of Object.entries(RATES)){if(rateKey.includes(kk)||kk.includes(rateKey)){rate=vv;break;}}
          if(!rate){const am=MODELS[activeModel]||MODELS[0];rate=am?.rate||3;}
          const c=(st.tokens*rate/1e6).toFixed(3);
          el.textContent=model+' · '+fmt(st.tokens)+' · $'+c;
        } else el.textContent='';
      }
    }
  }catch(e){}
}
setInterval(poll,2000);poll();
</script>
</body>
</html>""".replace('__WS_PORT__', str(WS_PORT))


class _HTTPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress request logs

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except BrokenPipeError:
            pass  # Browser closed connection early — harmless

    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            body = _HTML.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/api/sessions':
            # Return live sessions only — ping each CDP port and drop dead ones
            # (so closing all tabs in a session makes it vanish from the UI).
            try:
                from .server import get_sessions
                sessions = get_sessions()
            except Exception:
                import time as _t
                data = _load_sessions()
                now = _t.time()
                sessions = []
                for sid, rec in data.items():
                    last = rec.get("last_active", 0)
                    sessions.append({
                        "id": sid,
                        "port": rec.get("port"),
                        "last_active": last,
                        "idle_seconds": int(now - last) if last else None,
                        "active": False,
                    })
                sessions.sort(key=lambda r: -(r["last_active"] or 0))
            # Ping each port concurrently; reap dead sessions from the session file.
            def _ping(port):
                try:
                    urllib.request.urlopen(f'http://localhost:{port}/json/version', timeout=0.3)
                    return True
                except Exception:
                    return False

            pingable = [s for s in sessions if s.get("port") is not None]
            with ThreadPoolExecutor(max_workers=8) as pool:
                alive = list(pool.map(_ping, [s["port"] for s in pingable]))
            live = [s for s, ok in zip(pingable, alive) if ok]
            dead_ids = [s["id"] for s, ok in zip(pingable, alive) if not ok]
            if dead_ids:
                try:
                    with _session_lock():
                        persisted = _load_sessions()
                        reaped = False
                        for sid in dead_ids:
                            if persisted.pop(sid, None) is not None:
                                reaped = True
                        if reaped:
                            tmp = SESSION_FILE + ".tmp"
                            with open(tmp, "w") as f:
                                json.dump(persisted, f)
                            os.replace(tmp, SESSION_FILE)
                except Exception:
                    pass
            # Back-compat: if nothing is persisted but the legacy CDP port is up, show it.
            if not live:
                try:
                    urllib.request.urlopen(f'http://localhost:{CDP_PORT}/json/version', timeout=0.3)
                    live = [{"id": "default", "port": CDP_PORT, "last_active": 0, "idle_seconds": None, "active": False}]
                except Exception:
                    pass
            body = json.dumps(live).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith('/api/tabs'):
            # Optional ?session=X query param picks which CDP endpoint to query.
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            session = (q.get('session') or ['default'])[0]
            port = _session_port(session)
            try:
                resp = urllib.request.urlopen(f'http://localhost:{port}/json', timeout=3)
                raw = json.loads(resp.read())
                # Tag each tab with its session so the frontend can route clicks/WS correctly.
                for t in raw:
                    t['_session'] = session
                data = json.dumps(raw).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                body = json.dumps({"error": str(e), "session": session, "port": port}).encode()
                self.send_response(502)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        elif self.path == '/api/tokens':
            try:
                from .server import get_token_stats
                data = json.dumps(get_token_stats()).encode()
            except Exception:
                data = b'[]'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == '/api/token-history':
            log_path = os.path.expanduser("~/.openeyes/web/token-log.jsonl")
            entries = []
            try:
                with open(log_path) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            entries.append(json.loads(line))
            except Exception:
                pass
            data = json.dumps(entries).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == '/api/screenshot-history':
            try:
                entries = []
                hist_root = os.path.expanduser("~/.openeyes/web/history")
                for name in sorted(os.listdir(hist_root)) if os.path.isdir(hist_root) else []:
                    hist_dir = os.path.join(hist_root, name)
                    idx = os.path.join(hist_dir, "index.jsonl")
                    try:
                        with open(idx) as f:
                            for line in f:
                                line = line.strip()
                                if line:
                                    e = json.loads(line)
                                    e["_dir"] = os.path.basename(hist_dir)
                                    entries.append(e)
                    except Exception:
                        pass
                data = json.dumps(entries).encode()
            except Exception:
                import traceback
                print(traceback.format_exc(), file=sys.stderr)
                data = b'[]'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith('/screenshots/'):
            # Serve screenshot files: /screenshots/{dir}/{filename}.
            # Resolve and confine to the history root — this endpoint is
            # network-reachable, so reject any traversal out of it.
            from urllib.parse import unquote
            parts = unquote(self.path).split('/')
            filepath = None
            if len(parts) >= 4:
                root = os.path.realpath(HISTORY_ROOT)
                candidate = os.path.realpath(os.path.join(root, parts[2], parts[3]))
                if candidate.startswith(root + os.sep) and candidate.endswith('.jpg'):
                    filepath = candidate
            if filepath:
                try:
                    with open(filepath, "rb") as f:
                        data = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/jpeg')
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Cache-Control', 'public, max-age=86400')
                    self.end_headers()
                    self.wfile.write(data)
                except Exception:
                    self.send_response(404)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path == '/api/models':
            # Model pricing — single source of truth lives in server._MODEL_RATES.
            try:
                from .server import get_model_display_list
                data = json.dumps(get_model_display_list()).encode()
            except Exception:
                data = b'[]'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.startswith('/api/close/'):
            # Path format: /api/close/{session_id}/{tab_id} (session optional for legacy).
            parts = self.path.split('/')
            if len(parts) >= 5:
                session = parts[3]
                tab_id = parts[4]
            else:
                session = 'default'
                tab_id = parts[-1]
            port = _session_port(session)
            try:
                url = f'http://localhost:{port}/json/close/{tab_id}'
                urllib.request.urlopen(url, timeout=3)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        else:
            self.send_response(404)
            self.end_headers()


async def _ws_proxy(ws):
    """Proxy WebSocket to Chrome CDP.

    Path format: /ws/{session_id}/{tab_id}. Legacy /ws/{tab_id} routes to 'default'.
    """
    path = ws.request.path
    if not path.startswith('/ws/'):
        await ws.close()
        return

    parts = path.split('/')
    # /ws/{session}/{tab_id} → parts = ['', 'ws', session, tab_id]
    if len(parts) >= 4:
        session = parts[2]
        tab_id = parts[3]
    else:
        session = 'default'
        tab_id = parts[-1]
    port = _session_port(session)
    cdp_url = f'ws://localhost:{port}/devtools/page/{tab_id}'

    try:
        async with websockets.connect(cdp_url, max_size=10_000_000) as cdp:
            async def fwd_in():
                try:
                    async for msg in ws:
                        await cdp.send(msg)
                except websockets.exceptions.ConnectionClosed:
                    pass

            async def fwd_out():
                try:
                    async for msg in cdp:
                        await ws.send(msg)
                except websockets.exceptions.ConnectionClosed:
                    pass

            await asyncio.gather(fwd_in(), fwd_out())
    except Exception:
        pass


def _run_http(port: int):
    # Threading server — otherwise a slow screenshot download blocks the
    # 2-second /api/sessions poll and the whole UI stutters.
    server = http.server.ThreadingHTTPServer(('0.0.0.0', port), _HTTPHandler)
    server.daemon_threads = True
    server.serve_forever()


async def run_dashboard(http_port: int = HTTP_PORT, ws_port: int = WS_PORT):
    # HTTP server in a thread (serves HTML + API proxy)
    http_thread = threading.Thread(target=_run_http, args=(http_port,), daemon=True)
    http_thread.start()

    # WebSocket proxy server (async, proxies CDP)
    async with websockets.serve(_ws_proxy, '0.0.0.0', ws_port, max_size=10_000_000):
        print(f"[dashboard] Live at http://localhost:{http_port}")
        print(f"[dashboard] CDP proxy on ws://localhost:{ws_port}")
        await asyncio.Future()  # run forever


def main():
    http_port = HTTP_PORT
    if len(sys.argv) > 1:
        http_port = int(sys.argv[1])
    try:
        asyncio.run(run_dashboard(http_port))
    except KeyboardInterrupt:
        print("\n[dashboard] Stopped")


if __name__ == '__main__':
    main()
