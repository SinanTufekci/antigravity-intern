"""Multi-channel live "watch" dashboard for antigravity_swarm.

The single-worker watch mode in server.py is a singleton (one _WATCH_STATE, one
server, one window). A swarm runs N workers at once, so this serves ONE thin
dashboard window listing the workers vertically — each row shows only the repo,
the prompt, and a short snippet of the *latest* operation. Clicking a row opens a
dedicated detail window for that agent (the full step-by-step stream), positioned
right next to the dashboard. Bound to 127.0.0.1 only. Imported lazily by swarm.py
only when watch=True, so this top-level `from server import` runs after server is
fully loaded — no circular import.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from server import _chromium_app_browsers, _detect_image_format

_STATE: dict = {"title": "Antigravity Swarm", "started": 0.0, "workers": []}
_LOCK = threading.Lock()
_SERVER: Optional[tuple] = None  # (httpd, port)
_CF = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# Geometry of the dashboard window, so detail windows can open beside it.
_GEO = {"x": 40, "y": 60, "w": 400, "h": 320}


# ------------------------------------------------------------------- state mutation
def init(
    labels: list[str],
    repos: list[str],
    start: float,
    prompts: Optional[list[str]] = None,
) -> None:
    """Seed dashboard state. `labels` are the short, single-line row captions;
    `prompts` (optional) are the full untruncated prompts shown in each worker's
    detail window. When omitted, the detail window falls back to the label.
    """
    with _LOCK:
        _STATE["started"] = start
        _STATE["workers"] = [
            {
                "index": i,
                "label": labels[i],
                "prompt": prompts[i] if prompts and i < len(prompts) else labels[i],
                "repo": repos[i] if i < len(repos) else "",
                "status": "queued",
                "elapsed": 0.0,
                "events": [],
                "answer": "",
                "image": "",
            }
            for i in range(len(labels))
        ]


def worker_update(index: int, **fields) -> None:
    with _LOCK:
        _STATE["workers"][index].update(fields)


def worker_append(index: int, events: list[dict]) -> None:
    with _LOCK:
        _STATE["workers"][index]["events"].extend(events)


def worker_finish(index: int, status: str, answer: str, elapsed: float, image: str = "") -> None:
    with _LOCK:
        w = _STATE["workers"][index]
        w["status"] = status
        w["answer"] = answer
        w["elapsed"] = round(elapsed, 1)
        if image:
            w["image"] = image


def _snapshot() -> dict:
    with _LOCK:
        return json.loads(json.dumps(_STATE))  # cheap deep copy


def _allowed_images() -> set:
    with _LOCK:
        return {w["image"] for w in _STATE["workers"] if w["image"]}


# ------------------------------------------------------------------- HTTP server
def ensure_server() -> int:
    global _SERVER
    if _SERVER is not None:
        return _SERVER[1]

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/events"):
                self._send(json.dumps(_snapshot()).encode("utf-8"), "application/json")
            elif self.path.startswith("/open"):
                from urllib.parse import parse_qs, urlparse

                q = parse_qs(urlparse(self.path).query)
                try:
                    idx = int(q.get("i", ["-1"])[0])
                except ValueError:
                    idx = -1
                if 0 <= idx < len(_snapshot()["workers"]):
                    threading.Thread(target=open_worker_window, args=(idx,), daemon=True).start()
                self._send(b'{"ok":true}', "application/json")
            elif self.path.startswith("/worker"):
                self._send(_WORKER_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path.startswith("/image"):
                from urllib.parse import unquote

                path = unquote(self.path.split("?", 1)[1]) if "?" in self.path else ""
                fmt = (
                    _detect_image_format(path)
                    if path in _allowed_images() and os.path.isfile(path)
                    else None
                )
                if fmt:
                    mime = {
                        "JPEG": "image/jpeg",
                        "PNG": "image/png",
                        "GIF": "image/gif",
                        "WEBP": "image/webp",
                    }[fmt]
                    with open(path, "rb") as fh:
                        self._send(fh.read(), mime)
                else:
                    self.send_response(404)
                    self.end_headers()
            else:
                self._send(_HTML.encode("utf-8"), "text/html; charset=utf-8")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _SERVER = (httpd, port)
    return port


def _port() -> int:
    return ensure_server()


def _launch(url: str, w: int, h: int, x: Optional[int] = None, y: Optional[int] = None) -> None:
    """Open `url` in a chromeless --app window at a given size/position.

    Uses a fresh, dedicated --user-data-dir per window so Chrome spawns a NEW
    process that actually honors --window-size/--window-position (attaching to an
    already-running profile makes Chrome ignore those flags and reuse old bounds —
    which is why earlier windows opened too wide and stacked on top of each other).
    """
    pos = [f"--window-position={x},{y}"] if x is not None and y is not None else []
    prof = tempfile.mkdtemp(prefix="agy_chrome_")
    flags = [
        f"--app={url}",
        f"--window-size={w},{h}",
        f"--user-data-dir={prof}",
        "--no-first-run",
        "--no-default-browser-check",
        *pos,
    ]
    for exe in _chromium_app_browsers():
        try:
            subprocess.Popen(
                [exe, *flags],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CF,
            )
            return
        except OSError:
            continue
    try:
        webbrowser.open(url, new=1)
    except Exception:  # noqa: BLE001
        pass


def open_window(n_workers: int) -> None:
    """Open the thin vertical dashboard window (one compact row per worker)."""
    url = f"http://127.0.0.1:{_port()}/"
    print(f"[swarm-watch] dashboard: {url}", flush=True)
    # Fixed, narrow window; panes flex to fill it, so they spread out with few
    # workers and shrink as more are added.
    w, h, x, y = 440, 660, 40, 60
    _GEO.update(x=x, y=y, w=w, h=h)
    _launch(url, w, h, x, y)


def open_worker_window(index: int) -> None:
    """Open a dedicated detail window for one worker, right beside the dashboard."""
    x = _GEO["x"] + _GEO["w"] + 14
    y = _GEO["y"] + index * 28  # slight cascade so multiple detail windows don't fully overlap
    _launch(f"http://127.0.0.1:{_port()}/worker?i={index}", 680, 820, x, y)


# ------------------------------------------------------------------- dashboard page
# Thin vertical list: each row = repo + prompt + a SHORT snippet of the latest op.
# Whole row is clickable -> opens that agent's detail window (full steps) beside.
_HTML = """<!doctype html><html lang="en" translate="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Antigravity Swarm</title><style>
:root{--bg:#0a0c10;--fg:#d6d6d6;--dim:#6a7480;--green:#3fdf7f;--cyan:#5cd6e6;--red:#ff6b6b;--bd:#191c22}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg)}
body{color:var(--fg);font:12px/1.5 ui-monospace,"Cascadia Mono",Consolas,monospace;display:flex;flex-direction:column;height:100vh}
header{display:flex;align-items:center;gap:8px;padding:6px 11px;background:#0d0f14;border-bottom:1px solid var(--bd);flex:none;font-size:11px}
.name{color:var(--green);font-weight:700;text-shadow:0 0 9px rgba(63,223,127,.4)}
#tot{margin-left:auto;color:#556}
.grid{display:flex;flex-direction:column;flex:1;overflow:auto}
.pane{flex:1 1 0;min-height:46px;padding:9px 12px;border-bottom:1px solid var(--bd);cursor:pointer;user-select:none;display:flex;flex-direction:column;justify-content:center;gap:5px;overflow:hidden}
.pane:hover{background:#12151c}
.r1{display:flex;align-items:flex-start;gap:7px}
.r1 .dot{margin-top:5px}
.dot{width:7px;height:7px;border-radius:50%;flex:none}
.queued .dot{background:#556}
.working .dot{background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 1s infinite}
.done .dot{background:var(--cyan)}.error .dot{background:var(--red);box-shadow:0 0 8px var(--red)}
@keyframes pulse{50%{opacity:.3}}
.repo{color:#0a0c10;background:var(--green);border-radius:4px;padding:0 5px;font-size:9.5px;font-weight:700;flex:none;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.prompt{color:#e9eef3;font-weight:600;flex:1;min-width:0;overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;word-break:break-word}
.st{color:var(--dim);font-size:10.5px;flex:none}
.pop{color:var(--green);opacity:.55;flex:none;font-size:11px}
.pane:hover .pop{opacity:1;text-shadow:0 0 7px var(--green)}
.sub{display:flex;gap:6px;align-items:baseline;margin-top:3px;padding-left:14px;color:var(--dim);font-size:11px}
.sub .sym{flex:none;width:9px}
.sub .txt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sub.command .sym,.sub.command .txt{color:#cdd3d9}.sub.command .sym{color:var(--green)}
.sub.narration .sym,.sub.narration .txt{color:var(--cyan)}
.sub.done .sym,.sub.done .txt{color:var(--cyan)}
.sub.error .sym,.sub.error .txt{color:#ffb3b3}
.spin{color:var(--green);text-shadow:0 0 7px rgba(63,223,127,.6)}
</style></head><body>
<header><span class="name">Antigravity Swarm</span><span id="tot"></span></header>
<div class="grid" id="grid"></div>
<script>
const SYM={narration:"▸",command:"$",result:"✓",done:"✓",error:"✗"};
const FR="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";let fi=0;
let started=null,fin={};
const $=id=>document.getElementById(id);
function openWorker(i){fetch("/open?i="+i,{cache:"no-store"}).catch(()=>{});}
function esc(s){return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function cut(s,n){s=s||"";return s.length>n?s.slice(0,n)+"…":s;}
function build(ws){
 const g=$("grid");g.innerHTML="";fin={};
 ws.forEach(w=>{
  const p=document.createElement("div");p.className="pane "+w.status;p.id="p"+w.index;
  p.title="click to open this agent's full step log";p.onclick=()=>openWorker(w.index);
  p.innerHTML="<div class='r1'><span class='dot'></span>"+
   (w.repo?"<span class='repo' title='"+esc(w.repo)+"'>"+esc(w.repo)+"</span>":"")+
   "<span class='prompt' title='"+esc(w.label)+"'>"+esc(w.label||("Worker "+w.index))+"</span>"+
   "<span class='st' id='st"+w.index+"'></span><span class='pop'>↗</span></div>"+
   "<div class='sub' id='sub"+w.index+"'></div>";
  g.appendChild(p);
 });
}
async function tick(){
 fi=(fi+1)%FR.length;
 try{
  const s=await(await fetch("/events",{cache:"no-store"})).json();
  if(s.started!==started){started=s.started;build(s.workers);}
  let done=0;
  s.workers.forEach(w=>{
   const p=$("p"+w.index);if(p)p.className="pane "+w.status;
   const st=$("st"+w.index);
   if(st)st.textContent=w.status==="queued"?"queued":
     (w.status==="working"?w.elapsed.toFixed(1)+"s":w.status+" "+w.elapsed.toFixed(1)+"s");
   const sub=$("sub"+w.index);
   if(sub){
    const e=w.events.length?w.events[w.events.length-1]:null;
    if(w.status==="working"){
     sub.className="sub "+(e?e.kind:"");
     sub.innerHTML="<span class='sym spin'>"+FR[fi]+"</span><span class='txt'>"+
       esc(cut(e?e.text:"starting…",46))+"</span>";
    }else if(w.status==="done"||w.status==="error"){
     const k=w.status;
     sub.className="sub "+k;
     sub.innerHTML="<span class='sym'>"+SYM[k]+"</span><span class='txt'>"+
       esc(cut((w.answer||"").split("\\n")[0]||w.status,46))+"</span>";
     done++;
    }else if(e){
     sub.className="sub "+e.kind;
     sub.innerHTML="<span class='sym'>"+(SYM[e.kind]||"·")+"</span><span class='txt'>"+
       esc(cut(e.text,46))+"</span>";
    }
   }
  });
  $("tot").textContent=done+"/"+s.workers.length+" done";
 }catch(e){}
 setTimeout(tick,400);
}
tick();
</script></body></html>"""


# Dedicated single-worker detail page (opened when a row is clicked). Shows the
# repo + full prompt, the full step-by-step stream, and the final answer/image.
_WORKER_HTML = """<!doctype html><html lang="en" translate="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Antigravity Intern</title><style>
:root{--bg:#0a0c10;--fg:#d6d6d6;--dim:#6a7480;--green:#3fdf7f;--cyan:#5cd6e6;--red:#ff6b6b;--bd:#191c22}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg)}
body{color:var(--fg);font:13px/1.6 ui-monospace,"Cascadia Mono",Consolas,monospace;display:flex;flex-direction:column;height:100vh}
header{display:flex;align-items:center;gap:9px;padding:9px 14px;background:#0d0f14;border-bottom:1px solid var(--bd);flex:none}
.name{color:var(--green);font-weight:700;text-shadow:0 0 10px rgba(63,223,127,.4)}
.repo{color:#0a0c10;background:var(--green);border-radius:4px;padding:0 6px;font-size:10px;font-weight:700}
.dot{width:8px;height:8px;border-radius:50%;flex:none}
.working .dot{background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 1s infinite}
.queued .dot{background:#556}.done .dot{background:var(--cyan)}.error .dot{background:var(--red);box-shadow:0 0 8px var(--red)}
@keyframes pulse{50%{opacity:.3}}
.st{margin-left:auto;color:var(--dim);font-size:12px}
.pbar{padding:9px 15px;border-bottom:1px solid var(--bd);background:#0c0e13;flex:none}
.plabel{color:var(--green);font-size:9px;letter-spacing:1.5px;font-weight:700;display:flex;align-items:center;gap:8px;margin-bottom:4px;opacity:.85}
.ptoggle{margin-left:auto;color:var(--dim);cursor:pointer;font-size:9px;letter-spacing:.5px;user-select:none}
.ptoggle:hover{color:var(--green)}
.ptext{color:#e9eef3;white-space:pre-wrap;word-break:break-word;cursor:pointer}
.ptext.clamp{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
main{padding:11px 15px;overflow:auto;flex:1}
.row{display:flex;gap:9px;align-items:baseline;padding:2px 0;animation:sl .2s ease both}
@keyframes sl{from{opacity:0;transform:translateX(-6px)}}
.t{color:#4a4f57;min-width:50px;text-align:right;font-size:11px;flex:none}
.sym{width:10px;flex:none}.txt{white-space:pre-wrap;word-break:break-word}
.command .sym{color:var(--green)}.command .txt{color:#f2f2f2}
.narration .sym,.narration .txt{color:var(--cyan)}
.result .sym,.result .txt{color:var(--green);opacity:.5}
.ans{margin-top:13px;background:#0c0e13;border:1px solid var(--bd);border-radius:8px;padding:13px 15px;white-space:pre-wrap;word-break:break-word;animation:fd .4s}
.ans.err{border-color:#5a2a2a;color:#ffb3b3}@keyframes fd{from{opacity:0}}
.shot{max-width:100%;border:1px solid var(--bd);border-radius:8px;margin-top:11px;display:block;animation:fd .4s}
.cur{display:inline-block;width:7px;height:14px;background:var(--green);box-shadow:0 0 8px var(--green);animation:bl 1.05s steps(1) infinite}
@keyframes bl{50%{opacity:0}}
</style></head><body>
<header id="hd" class="working"><span class="name">Antigravity Intern</span>
<span class="dot"></span><span class="repo" id="repo" style="display:none"></span>
<span class="st" id="st"></span></header>
<div class="pbar"><span class="plabel">PROMPT<span class="ptoggle" id="ptoggle" style="display:none">EXPAND ▾</span></span><div class="ptext clamp" id="ptext"></div></div>
<main><div id="steps"></div><div id="cur"><span class="cur"></span></div><div id="ans"></div></main>
<script>
const SYM={narration:"▸",command:"$",result:"✓"};
const IDX=parseInt(new URLSearchParams(location.search).get("i")||"0",10);
let started=null,seen=0,fin=false,promptText=null;
const $=id=>document.getElementById(id);
function esc(s){return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function reset(){$("steps").innerHTML="";$("ans").innerHTML="";$("cur").style.display="";seen=0;fin=false;promptText=null;}
function applyPrompt(t){
 if(t===promptText)return;promptText=t;
 const pt=$("ptext"),tg=$("ptoggle");
 pt.textContent=t||"";pt.classList.add("clamp");tg.textContent="EXPAND ▾";
 const of=pt.scrollHeight>pt.clientHeight+1;  // a toggle only helps if the prompt overflows the clamp
 tg.style.display=of?"":"none";pt.style.cursor=of?"pointer":"default";
}
function togglePrompt(){
 if($("ptoggle").style.display==="none")return;  // short prompt: nothing to expand
 const clamped=$("ptext").classList.toggle("clamp");
 $("ptoggle").textContent=clamped?"EXPAND ▾":"COLLAPSE ▴";
}
function addStep(e){
 const row=document.createElement("div");row.className="row "+e.kind;
 row.innerHTML="<span class='t'>["+e.t.toFixed(1)+"s]</span><span class='sym'>"+
  (SYM[e.kind]||"·")+"</span><span class='txt'>"+esc(e.text)+"</span>";
 $("steps").appendChild(row);window.scrollTo(0,document.body.scrollHeight);
}
function finish(w){
 fin=true;$("cur").style.display="none";
 if(w.image){const im=document.createElement("img");im.className="shot";
  im.src="/image?"+encodeURIComponent(w.image);$("ans").appendChild(im);}
 if(w.answer){const a=document.createElement("div");
  a.className="ans"+(w.status==="error"?" err":"");a.textContent=w.answer;$("ans").appendChild(a);}
}
async function tick(){
 try{
  const s=await(await fetch("/events",{cache:"no-store"})).json();
  const w=s.workers[IDX];
  if(w){
   if(s.started!==started){started=s.started;reset();}
   document.title="Intern · "+(w.repo?w.repo+" · ":"")+(w.label||("Worker "+IDX));
   if(w.repo){$("repo").style.display="";$("repo").textContent=w.repo;}
   applyPrompt(w.prompt||w.label||"");
   $("hd").className=w.status;
   $("st").textContent=w.status==="queued"?"queued":
     (w.status==="working"?w.elapsed.toFixed(1)+"s":w.status+" "+w.elapsed.toFixed(1)+"s");
   for(let i=seen;i<w.events.length;i++)addStep(w.events[i]);
   seen=w.events.length;
   if((w.status==="done"||w.status==="error")&&!fin)finish(w);
  }
 }catch(e){}
 setTimeout(tick,fin?1500:400);
}
$("ptext").onclick=togglePrompt;$("ptoggle").onclick=togglePrompt;
tick();
</script></body></html>"""
