"""Live dashboard — watch and interact with agent browser sessions at localhost:6080."""

from __future__ import annotations

import asyncio
import http.server
import json
import sys
import threading
import urllib.request

import websockets

CDP_PORT = 9222
HTTP_PORT = 6080
WS_PORT = 6081

_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>ViewPort</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0f1117;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
.hdr{background:#1a1d27;padding:12px 24px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #2a2d37}
.hdr h1{font-size:16px;font-weight:600}
.st{font-size:13px;color:#888}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.dot.on{background:#4ade80}.dot.off{background:#ef4444}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(480px,1fr));gap:16px;padding:16px}
.tile{background:#1a1d27;border-radius:8px;overflow:hidden;border:1px solid #2a2d37}
.tile:hover{border-color:#4a9eff}
.th{padding:8px 12px;background:#22252f;font-size:13px;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;display:flex;align-items:center;justify-content:space-between}
.th .title{overflow:hidden;text-overflow:ellipsis;flex:1}
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
<div class="hdr"><h1>ViewPort</h1><div class="st" id="st"><span class="dot off"></span>Connecting...</div></div>
<div class="grid" id="grid"></div>
<div class="mt" id="mt"><h2>No active browser sessions</h2><p>Sessions appear when an agent starts browsing.</p></div>
<div class="fs" id="fs">
  <div class="bar"><div><span id="fst"></span><span class="badge">LIVE</span></div><button onclick="xfs()">Close (Esc)</button></div>
  <div class="vp"><img id="fsi"/></div>
</div>
<script>
const WS_PORT = __WS_PORT__;
const S={};let fid=null;

async function gt(){
  try{
    const r=await fetch('/api/tabs');
    const t=await r.json();
    const p=t.filter(x=>x.type==='page'&&!x.url.startsWith('chrome')&&!x.parentId);
    document.getElementById('st').innerHTML='<span class="dot on"></span>Connected — '+p.length+' tab(s)';
    return p;
  }catch(e){
    document.getElementById('st').innerHTML='<span class="dot off"></span>Not connected';
    return[];
  }
}

function ct(t){
  if(S[t.id])return;
  const wsUrl='ws://'+location.hostname+':'+WS_PORT+'/ws/'+t.id;
  const ws=new WebSocket(wsUrl);
  const s={ws,t,n:0,f:null,m:null};S[t.id]=s;
  ws.onopen=()=>ws.send(JSON.stringify({id:1,method:'Page.startScreencast',
    params:{format:'jpeg',quality:50,maxWidth:1280,maxHeight:900,everyNthFrame:2}}));
  ws.onmessage=e=>{
    const msg=JSON.parse(e.data);
    if(msg.method==='Page.screencastFrame'){
      s.n++;s.f=msg.params.data;s.m=msg.params.metadata;
      const i=document.getElementById('i-'+t.id);
      if(i)i.src='data:image/jpeg;base64,'+msg.params.data;
      if(fid===t.id)document.getElementById('fsi').src='data:image/jpeg;base64,'+msg.params.data;
      ws.send(JSON.stringify({id:100+s.n,method:'Page.screencastFrameAck',
        params:{sessionId:msg.params.sessionId}}));
    }
  };
  ws.onclose=()=>{delete S[t.id];rt(t.id);};
}

function mt(t){
  let el=document.getElementById('t-'+t.id);
  if(el){el.querySelector('.title').textContent=t.title||t.url;return;}
  el=document.createElement('div');el.className='tile';el.id='t-'+t.id;
  const hdr=document.createElement('div');hdr.className='th';
  const title=document.createElement('span');title.className='title';title.textContent=t.title||t.url;
  const cbtn=document.createElement('button');cbtn.className='close';cbtn.textContent='Close';
  cbtn.onclick=function(e){e.stopPropagation();ctab(t.id);};
  hdr.appendChild(title);hdr.appendChild(cbtn);
  const body=document.createElement('div');body.className='tb';
  body.onclick=function(){ofs(t.id);};
  const img=document.createElement('img');img.id='i-'+t.id;
  const ov=document.createElement('div');ov.className='ov';ov.textContent='Click to interact';
  body.appendChild(img);body.appendChild(ov);
  el.appendChild(hdr);el.appendChild(body);
  document.getElementById('grid').appendChild(el);
}
function rt(id){const x=document.getElementById('t-'+id);if(x)x.remove();ue();}

function sm(id,img,ev,ty){
  const s=S[id];if(!s||!s.m)return;
  const r=img.getBoundingClientRect();
  const x=Math.round((ev.clientX-r.left)*s.m.deviceWidth/r.width);
  const y=Math.round((ev.clientY-r.top)*s.m.deviceHeight/r.height);
  s.ws.send(JSON.stringify({id:200,method:'Input.dispatchMouseEvent',
    params:{type:ty,x,y,button:'left',clickCount:1}}));
}

function ofs(id){
  fid=id;const s=S[id];
  document.getElementById('fs').classList.add('on');
  document.getElementById('fst').textContent=s?.t?.title||'';
  const img=document.getElementById('fsi');
  if(s?.f)img.src='data:image/jpeg;base64,'+s.f;
  img.onmousedown=e=>sm(id,img,e,'mousePressed');
  img.onmouseup=e=>sm(id,img,e,'mouseReleased');
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
function ctab(id){
  fetch('/api/close/'+id,{method:'POST'}).then(()=>{
    const s=S[id];if(s&&s.ws)s.ws.close();
    delete S[id];rt(id);
  });
}
function ue(){document.getElementById('mt').style.display=document.getElementById('grid').children.length?'none':'block';}

async function poll(){
  const tabs=await gt();
  const ids=new Set();
  for(const t of tabs){ids.add(t.id);mt(t);ct(t);}
  for(const id of Object.keys(S)){if(!ids.has(id))rt(id);}
  ue();
}
setInterval(poll,2000);poll();
</script>
</body>
</html>""".replace('__WS_PORT__', str(WS_PORT))


class _HTTPHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            body = _HTML.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/api/tabs':
            try:
                resp = urllib.request.urlopen(f'http://localhost:{CDP_PORT}/json', timeout=3)
                data = resp.read()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
                self.send_response(502)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.startswith('/api/close/'):
            tab_id = self.path.split('/')[-1]
            try:
                url = f'http://localhost:{CDP_PORT}/json/close/{tab_id}'
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

    def log_message(self, format, *args):
        pass


async def _ws_proxy(ws):
    """Proxy WebSocket to Chrome CDP."""
    path = ws.request.path
    if not path.startswith('/ws/'):
        await ws.close()
        return

    tab_id = path.split('/')[-1]
    cdp_url = f'ws://localhost:{CDP_PORT}/devtools/page/{tab_id}'

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
    server = http.server.HTTPServer(('0.0.0.0', port), _HTTPHandler)
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
