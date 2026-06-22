"""Multi-channel live "watch" dashboard for antigravity_swarm.

The single-worker watch mode in server.py is a singleton (one _WATCH_STATE, one
server, one window). A swarm runs N workers at once, so this serves ONE thin
dashboard window listing the workers vertically — each row shows the repo, the
prompt, a short snippet of the *latest* operation, and a per-worker time bar.
Clicking a row (or selecting with the keyboard and pressing Enter) opens a
dedicated detail window for that agent — the full step-by-step stream, rendered
with the same typewriter + Markdown treatment as the single-worker viewer.
Bound to 127.0.0.1 only. Imported lazily by swarm.py only when watch=True, so
this top-level `from server import` runs after server is fully loaded — no
circular import.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from server import _chromium_app_browsers, _detect_image_format, _env_truthy

_STATE: dict = {"title": "Agent Swarm", "started": 0.0, "timeout": 0.0, "workers": []}
_LOCK = threading.Lock()
_SERVER: Optional[tuple] = None  # (httpd, port)
_CF = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# An open dashboard polls /events a few times a second; track the last poll so
# repeated swarm runs reuse the open window instead of stacking a new one each time.
_LAST_POLL = 0.0
_VIEWER_ALIVE_S = 4.0

# Geometry of the dashboard window, so detail windows can open beside it.
_GEO = {"x": 40, "y": 60, "w": 400, "h": 320}


# ------------------------------------------------------------------- state mutation
def init(
    labels: list[str],
    repos: list[str],
    start: float,
    prompts: Optional[list[str]] = None,
    timeout: float = 0.0,
) -> None:
    """Seed dashboard state. `labels` are the short, single-line row captions;
    `prompts` (optional) are the full untruncated prompts shown in each worker's
    detail window (falls back to the label when omitted). `timeout` is the
    per-worker timeout_s, used to draw each row's time progress bar.
    """
    with _LOCK:
        _STATE["started"] = start
        _STATE["timeout"] = timeout
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
                global _LAST_POLL
                _LAST_POLL = time.time()
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


def _dashboard_is_live() -> bool:
    """True if a dashboard polled /events within _VIEWER_ALIVE_S — reuse it rather
    than stacking another window for this run."""
    return (time.time() - _LAST_POLL) < _VIEWER_ALIVE_S


def open_window(n_workers: int) -> None:
    """Open the thin vertical dashboard window (one compact row per worker).

    Reuses an already-open dashboard (detected via recent /events polls) so repeated
    swarm runs don't pile up browser windows; the open page rebuilds itself for the
    new run. Set AGY_WATCH_ALWAYS_NEW=1 to force a fresh window each time."""
    # Fixed, narrow window; panes flex to fill it, so they spread out with few
    # workers and shrink as more are added.
    w, h, x, y = 440, 660, 40, 60
    _GEO.update(x=x, y=y, w=w, h=h)
    url = f"http://127.0.0.1:{_port()}/"
    if _dashboard_is_live() and not _env_truthy("AGY_WATCH_ALWAYS_NEW"):
        print(f"[swarm-watch] reusing open dashboard: {url}", flush=True)
        return
    print(f"[swarm-watch] dashboard: {url}", flush=True)
    _launch(url, w, h, x, y)


def open_worker_window(index: int) -> None:
    """Open a dedicated detail window for one worker, right beside the dashboard."""
    x = _GEO["x"] + _GEO["w"] + 14
    y = _GEO["y"] + index * 28  # slight cascade so multiple detail windows don't fully overlap
    _launch(f"http://127.0.0.1:{_port()}/worker?i={index}", 680, 820, x, y)


# ------------------------------------------------------------------- dashboard page
# Thin vertical list: each row = repo + prompt + a SHORT snippet of the latest op
# + a per-worker time bar. A row is selectable (↑/↓), openable (click or Enter),
# and opens that agent's detail window (full steps) beside the dashboard.
_HTML = """<!doctype html><html lang="en" translate="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Swarm</title><style>
:root{--bg:#0a0c10;--fg:#d6d6d6;--dim:#6a7480;--green:#3fdf7f;--cyan:#5cd6e6;--red:#ff6b6b;--bd:#191c22}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg)}
body{color:var(--fg);font:12px/1.5 ui-monospace,"Cascadia Mono",Consolas,monospace;display:flex;flex-direction:column;height:100vh}
::-webkit-scrollbar{width:9px}::-webkit-scrollbar-thumb{background:#23262d;border-radius:6px}
header{display:flex;align-items:center;gap:9px;padding:7px 11px;background:#0d0f14;border-bottom:1px solid var(--bd);flex:none;font-size:11px}
.name{color:var(--green);font-weight:700;text-shadow:0 0 9px rgba(63,223,127,.45)}
.clock{color:#566;font-variant-numeric:tabular-nums}
#tot{margin-left:auto;color:#7c8896;font-variant-numeric:tabular-nums}
.gbar{height:2px;background:#11141a;flex:none}
.gfill{height:100%;width:0;background:linear-gradient(90deg,var(--green),var(--cyan));box-shadow:0 0 8px rgba(92,214,230,.5);transition:width .5s ease}
.grid{display:flex;flex-direction:column;flex:1;overflow:auto}
.pane{position:relative;flex:1 1 0;min-height:54px;padding:9px 12px 12px;border-bottom:1px solid var(--bd);cursor:pointer;user-select:none;display:flex;flex-direction:column;justify-content:center;gap:5px;overflow:hidden;transition:background .15s ease,opacity .3s ease}
.pane:hover{background:#12151c}
.pane.sel{background:#141923;box-shadow:inset 2px 0 0 var(--cyan)}
.pane.done,.pane.error{opacity:.82}
.pane.done:hover,.pane.error:hover,.pane.sel{opacity:1}
.r1{display:flex;align-items:flex-start;gap:7px}
.r1 .dot{margin-top:5px}
.dot{width:7px;height:7px;border-radius:50%;flex:none;transition:background .25s ease}
.queued .dot{background:#556}
.working .dot{background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 1s infinite}
.done .dot{background:var(--cyan);box-shadow:0 0 7px var(--cyan);animation:pop .45s ease}
.error .dot{background:var(--red);box-shadow:0 0 8px var(--red);animation:pop .45s ease}
@keyframes pulse{50%{opacity:.3}}
@keyframes pop{0%{transform:scale(.2)}55%{transform:scale(1.5)}100%{transform:scale(1)}}
.repo{color:#0a0c10;background:var(--green);border-radius:4px;padding:0 5px;font-size:9.5px;font-weight:700;flex:none;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-top:1px}
.prompt{color:#e9eef3;font-weight:600;flex:1;min-width:0;overflow:hidden;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;word-break:break-word}
.st{color:var(--dim);font-size:10.5px;flex:none;font-variant-numeric:tabular-nums;margin-top:1px}
.pop{color:var(--green);opacity:.55;flex:none;font-size:11px;margin-top:1px}
.pane:hover .pop{opacity:1;text-shadow:0 0 7px var(--green)}
.sub{display:flex;gap:6px;align-items:baseline;padding-left:14px;color:var(--dim);font-size:11px}
.sub .sym{flex:none;width:9px}
.sub .txt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sub.command .sym,.sub.command .txt{color:#cdd3d9}.sub.command .sym{color:var(--green)}
.sub.narration .sym,.sub.narration .txt{color:var(--cyan)}
.sub.done .sym,.sub.done .txt{color:var(--cyan)}
.sub.error .sym,.sub.error .txt{color:#ffb3b3}
.spin{color:var(--green);text-shadow:0 0 7px rgba(63,223,127,.6)}
.rbar{position:absolute;left:0;right:0;bottom:0;height:2px;background:#11141a}
.rfill{height:100%;width:0;transition:width .4s linear}
.rfill.working{background:linear-gradient(90deg,rgba(63,223,127,.45),var(--green));background-size:200% 100%;animation:flow 1.1s linear infinite}
.rfill.done{background:var(--cyan)}
.rfill.error{background:var(--red)}
@keyframes flow{from{background-position:200% 0}to{background-position:0 0}}
.foot{flex:none;padding:4px 11px;border-top:1px solid var(--bd);color:#3b414a;font-size:10px;background:#0d0f14;text-align:center}
</style></head><body>
<header><span class="name">Agent Swarm</span><span class="clock" id="clock"></span><span id="tot"></span></header>
<div class="gbar"><div class="gfill" id="gfill"></div></div>
<div class="grid" id="grid"></div>
<div class="foot">↑/↓ select · ↵ open · click a row for its full log</div>
<script>
const SYM={narration:"▸",command:"$",result:"✓",done:"✓",error:"✗"};
const FR="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";let fi=0;
let started=null,sel=-1,nWork=0,timeout=0,statuses={};
const $=id=>document.getElementById(id);
function openWorker(i){fetch("/open?i="+i,{cache:"no-store"}).catch(()=>{});}
function esc(s){return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function cut(s,n){s=s||"";return s.length>n?s.slice(0,n)+"…":s;}
function applySel(){for(let i=0;i<nWork;i++){const p=$("p"+i);if(p)p.classList.toggle("sel",i===sel);}}
function build(ws){
 const g=$("grid");g.innerHTML="";statuses={};
 ws.forEach(w=>{
  const p=document.createElement("div");p.className="pane "+w.status;p.id="p"+w.index;
  p.title="click to open this agent's full step log";p.onclick=()=>openWorker(w.index);
  p.innerHTML="<div class='r1'><span class='dot'></span>"+
   (w.repo?"<span class='repo' title='"+esc(w.repo)+"'>"+esc(w.repo)+"</span>":"")+
   "<span class='prompt' title='"+esc(w.label)+"'>"+esc(w.label||("Worker "+w.index))+"</span>"+
   "<span class='st' id='st"+w.index+"'></span><span class='pop'>↗</span></div>"+
   "<div class='sub' id='sub"+w.index+"'></div>"+
   "<div class='rbar'><div class='rfill' id='rf"+w.index+"'></div></div>";
  g.appendChild(p);statuses[w.index]=w.status;
 });
 if(sel>=ws.length)sel=ws.length-1;
 applySel();
}
document.addEventListener("keydown",e=>{
 if(!nWork)return;
 if(e.key==="ArrowDown"||e.key==="ArrowUp"){
  e.preventDefault();
  sel=(sel<0)?0:sel+(e.key==="ArrowDown"?1:-1);
  if(sel<0)sel=0;if(sel>=nWork)sel=nWork-1;
  applySel();const el=$("p"+sel);if(el)el.scrollIntoView({block:"nearest"});
 }else if(e.key==="Enter"&&sel>=0){openWorker(sel);}
});
async function tick(){
 fi=(fi+1)%FR.length;
 try{
  const s=await(await fetch("/events",{cache:"no-store"})).json();
  if(s.started!==started){started=s.started;nWork=s.workers.length;timeout=s.timeout||0;build(s.workers);}
  s.workers.forEach(w=>{
   const p=$("p"+w.index);
   if(p&&statuses[w.index]!==w.status){statuses[w.index]=w.status;p.className="pane "+w.status;applySel();}
   const st=$("st"+w.index);
   if(st)st.textContent=w.status==="queued"?"queued":
     (w.status==="working"?w.elapsed.toFixed(1)+"s":w.status+" "+w.elapsed.toFixed(1)+"s");
   const sub=$("sub"+w.index);
   if(sub){
    const e=w.events.length?w.events[w.events.length-1]:null;
    if(w.status==="working"){
     sub.className="sub "+(e?e.kind:"");
     sub.innerHTML="<span class='sym spin'>"+FR[fi]+"</span><span class='txt'>"+
       esc(cut(e?e.text:"starting…",54))+"</span>";
    }else if(w.status==="done"||w.status==="error"){
     const k=w.status;sub.className="sub "+k;
     sub.innerHTML="<span class='sym'>"+SYM[k]+"</span><span class='txt'>"+
       esc(cut((w.answer||"").split("\\n")[0]||w.status,54))+"</span>";
    }else if(e){
     sub.className="sub "+e.kind;
     sub.innerHTML="<span class='sym'>"+(SYM[e.kind]||"·")+"</span><span class='txt'>"+
       esc(cut(e.text,54))+"</span>";
    }else{sub.className="sub";sub.innerHTML="<span class='txt' style='opacity:.5'>queued…</span>";}
   }
   const rf=$("rf"+w.index);
   if(rf){
    let frac=0;
    if(w.status==="done"||w.status==="error")frac=1;
    else if(w.status==="working")frac=timeout>0?Math.min(w.elapsed/timeout,.98):0.06;
    rf.className="rfill "+w.status;rf.style.width=Math.round(frac*100)+"%";
   }
  });
  const done=s.workers.filter(w=>w.status==="done"||w.status==="error").length;
  $("gfill").style.width=(nWork?Math.round(done/nWork*100):0)+"%";
  $("tot").textContent=done+"/"+nWork+" done";
  const el=started?(Date.now()/1000-started):0;
  $("clock").textContent=el>0?el.toFixed(0)+"s":"";
 }catch(e){}
 setTimeout(tick,400);
}
tick();
</script></body></html>"""


# Dedicated single-worker detail page (opened when a row is clicked / Enter). Shows
# the repo + expandable full prompt, a time progress bar, the step-by-step stream
# revealed with a typewriter, and the final Markdown answer / image (with a copy
# button). Mirrors the single-worker viewer in server.py.
_WORKER_HTML = """<!doctype html><html lang="en" translate="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Intern</title><style>
:root{--bg:#0a0c10;--fg:#d6d6d6;--dim:#6a7480;--green:#3fdf7f;--cyan:#5cd6e6;--red:#ff6b6b;--bd:#191c22;--code:#06080b}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg)}
body{color:var(--fg);font:13px/1.6 ui-monospace,"Cascadia Mono",Consolas,monospace;display:flex;flex-direction:column;height:100vh}
::-webkit-scrollbar{width:9px}::-webkit-scrollbar-thumb{background:#23262d;border-radius:6px}
header{display:flex;align-items:center;gap:9px;padding:9px 14px;background:#0d0f14;border-bottom:1px solid var(--bd);flex:none}
.name{color:var(--green);font-weight:700;text-shadow:0 0 10px rgba(63,223,127,.4)}
.repo{color:#0a0c10;background:var(--green);border-radius:4px;padding:0 6px;font-size:10px;font-weight:700}
.dot{width:8px;height:8px;border-radius:50%;flex:none}
.working .dot{background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 1s infinite}
.queued .dot{background:#556}
.done .dot{background:var(--cyan);box-shadow:0 0 7px var(--cyan);animation:pop .45s ease}
.error .dot{background:var(--red);box-shadow:0 0 8px var(--red);animation:pop .45s ease}
@keyframes pulse{50%{opacity:.3}}
@keyframes pop{0%{transform:scale(.2)}55%{transform:scale(1.5)}100%{transform:scale(1)}}
.st{margin-left:auto;color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums}
.gbar{height:2px;background:#11141a;flex:none}
.gfill{height:100%;width:0;background:linear-gradient(90deg,var(--green),var(--cyan));box-shadow:0 0 8px rgba(92,214,230,.5);transition:width .4s linear}
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
.cur{display:inline-block;width:7px;height:14px;background:var(--green);box-shadow:0 0 8px var(--green);animation:bl 1.05s steps(1) infinite}
@keyframes bl{50%{opacity:0}}
.ans{position:relative;margin-top:13px;background:#0c0e13;border:1px solid var(--bd);border-radius:8px;padding:14px 15px;animation:fd .4s}
.ans.err{border-color:#5a2a2a;color:#ffb3b3}@keyframes fd{from{opacity:0}}
.ans .h{font-weight:700;margin:13px 0 5px;color:#cdd9e5}
.ans .h1{font-size:16px;color:#fff}.ans .h2{font-size:14px}.ans .h3{font-size:12.5px;color:var(--green)}
.ans .p{margin:3px 0;white-space:pre-wrap;word-break:break-word}
.ans .li{display:flex;gap:8px;margin:2px 0}
.ans .bul{color:var(--green);flex:none;min-width:14px;text-align:right}
.ans .lit{white-space:pre-wrap;word-break:break-word}
.ans pre.code{background:var(--code);border-left:2px solid var(--green);border-radius:4px;padding:9px 11px;margin:7px 0;overflow:auto;white-space:pre;color:#e9efe9}
.ans code{background:#16191f;padding:1px 5px;border-radius:4px;color:#9fe6ad}
.ans .lnk{color:var(--cyan);border-bottom:1px dotted #2a6b73}
.ans strong{color:#fff}
.copy{position:absolute;top:7px;right:8px;background:#11151c;border:1px solid var(--bd);color:var(--dim);font:inherit;font-size:10px;padding:2px 8px;border-radius:5px;cursor:pointer;opacity:.55;transition:opacity .15s,color .15s,border-color .15s}
.copy:hover{opacity:1;color:var(--green);border-color:#2a3340}
.shot{max-width:100%;border:1px solid var(--bd);border-radius:8px;margin-top:11px;display:block;animation:fd .4s}
.jump{position:fixed;bottom:12px;left:50%;transform:translateX(-50%);background:#12161d;border:1px solid #2a3340;color:var(--cyan);font-size:11px;padding:5px 13px;border-radius:20px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.5);animation:fd .3s}
</style></head><body>
<header id="hd" class="working"><span class="name">Agent Intern</span>
<span class="dot"></span><span class="repo" id="repo" style="display:none"></span>
<span class="st" id="st"></span></header>
<div class="gbar"><div class="gfill" id="gfill"></div></div>
<div class="pbar"><span class="plabel">PROMPT<span class="ptoggle" id="ptoggle" style="display:none">EXPAND ▾</span></span><div class="ptext clamp" id="ptext"></div></div>
<main><div id="steps"></div><div id="cur"><span class="cur"></span></div><div id="ans"></div></main>
<div class="jump" id="jump" style="display:none">↓ jump to latest</div>
<script>
const SYM={narration:"▸",command:"$",result:"✓"};
const IDX=parseInt(new URLSearchParams(location.search).get("i")||"0",10);
let started=null,seen=0,fin=false,promptText=null,tq=[],typing=false,follow=true,timeout=0;
const $=id=>document.getElementById(id);
function esc(s){return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function toBottom(){window.scrollTo(0,document.body.scrollHeight);}
function maybeBottom(){if(follow)toBottom();}
window.addEventListener("scroll",()=>{
 follow=window.innerHeight+window.scrollY>=document.body.scrollHeight-44;
 $("jump").style.display=follow?"none":"";
});
$("jump").onclick=()=>{follow=true;$("jump").style.display="none";toBottom();};
function reset(){
 $("steps").innerHTML="";$("ans").innerHTML="";$("cur").style.display="";
 seen=0;fin=false;promptText=null;tq=[];typing=false;follow=true;$("jump").style.display="none";
}
function applyPrompt(t){
 if(t===promptText)return;promptText=t;
 const pt=$("ptext"),tg=$("ptoggle");
 pt.textContent=t||"";pt.classList.add("clamp");tg.textContent="EXPAND ▾";
 const of=pt.scrollHeight>pt.clientHeight+1;
 tg.style.display=of?"":"none";pt.style.cursor=of?"pointer":"default";
}
function togglePrompt(){
 if($("ptoggle").style.display==="none")return;
 const c=$("ptext").classList.toggle("clamp");
 $("ptoggle").textContent=c?"EXPAND ▾":"COLLAPSE ▴";
}
$("ptext").onclick=togglePrompt;$("ptoggle").onclick=togglePrompt;
function drain(){
 if(!tq.length){typing=false;return;}
 typing=true;const[el,text]=tq.shift();let i=0;
 (function step(){
  el.textContent=text.slice(0,i++);maybeBottom();
  if(i<=text.length)setTimeout(step,text.length>90?3:9);else drain();
 })();
}
function type(el,text){tq.push([el,text]);if(!typing)drain();}
function addStep(e){
 const row=document.createElement("div");row.className="row "+e.kind;
 const t=document.createElement("span");t.className="t";t.textContent="["+e.t.toFixed(1)+"s]";
 const sy=document.createElement("span");sy.className="sym";sy.textContent=SYM[e.kind]||"·";
 const tx=document.createElement("span");tx.className="txt";
 row.append(t,sy,tx);$("steps").appendChild(row);type(tx,e.text);
}
function inl(s){
 return s.replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g,"<span class='lnk'>$1</span>")
         .replace(/`([^`]+)`/g,(m,c)=>"<code>"+c+"</code>")
         .replace(/\\*\\*([^*]+)\\*\\*/g,"<strong>$1</strong>");
}
function md(src){
 const lines=esc(src).split("\\n"),out=[];let inC=false,code="";
 for(const ln of lines){
  const f=ln.match(/^```(\\w*)\\s*$/);
  if(f){if(!inC){inC=true;code="";}else{inC=false;
   out.push("<pre class='code'>"+code.replace(/\\n$/,"")+"</pre>");}continue;}
  if(inC){code+=ln+"\\n";continue;}
  const h=ln.match(/^(#{1,6})\\s+(.*)$/);
  if(h){out.push("<div class='h h"+h[1].length+"'>"+inl(h[2])+"</div>");continue;}
  const b=ln.match(/^\\s*[-*]\\s+(.*)$/);
  if(b){out.push("<div class='li'><span class='bul'>•</span>"+
   "<span class='lit'>"+inl(b[1])+"</span></div>");continue;}
  const n=ln.match(/^\\s*(\\d+)\\.\\s+(.*)$/);
  if(n){out.push("<div class='li'><span class='bul'>"+n[1]+".</span>"+
   "<span class='lit'>"+inl(n[2])+"</span></div>");continue;}
  if(ln.trim()==="")continue;
  out.push("<div class='p'>"+inl(ln)+"</div>");
 }
 if(inC)out.push("<pre class='code'>"+code+"</pre>");
 return out.join("");
}
function copyText(txt,btn){
 navigator.clipboard.writeText(txt).then(()=>{
  const o=btn.textContent;btn.textContent="copied ✓";setTimeout(()=>btn.textContent=o,1200);
 }).catch(()=>{});
}
function finish(w){
 fin=true;$("cur").style.display="none";
 if(w.image){const im=document.createElement("img");im.className="shot";
  im.onload=maybeBottom;im.src="/image?"+encodeURIComponent(w.image);$("ans").appendChild(im);}
 if(w.answer){
  const a=document.createElement("div");a.className="ans"+(w.status==="error"?" err":"");
  a.innerHTML=md(w.answer);
  const cp=document.createElement("button");cp.className="copy";cp.textContent="copy";
  cp.onclick=()=>copyText(w.answer,cp);a.appendChild(cp);
  $("ans").appendChild(a);
 }
 maybeBottom();
}
async function tick(){
 try{
  const s=await(await fetch("/events",{cache:"no-store"})).json();
  const w=s.workers[IDX];
  if(w){
   if(s.started!==started){started=s.started;timeout=s.timeout||0;reset();}
   document.title="Intern · "+(w.repo?w.repo+" · ":"")+(w.label||("Worker "+IDX));
   if(w.repo){$("repo").style.display="";$("repo").textContent=w.repo;}
   applyPrompt(w.prompt||w.label||"");
   $("hd").className=w.status;
   $("st").textContent=w.status==="queued"?"queued":
     (w.status==="working"?w.elapsed.toFixed(1)+"s":w.status+" "+w.elapsed.toFixed(1)+"s");
   let frac=0;
   if(w.status==="done"||w.status==="error")frac=1;
   else if(w.status==="working")frac=timeout>0?Math.min(w.elapsed/timeout,.98):0.06;
   $("gfill").style.width=Math.round(frac*100)+"%";
   $("gfill").style.background=w.status==="error"?"var(--red)":"linear-gradient(90deg,var(--green),var(--cyan))";
   for(let i=seen;i<w.events.length;i++)addStep(w.events[i]);
   seen=w.events.length;
   if((w.status==="done"||w.status==="error")&&!fin)finish(w);
  }
 }catch(e){}
 setTimeout(tick,fin?1500:400);
}
tick();
</script></body></html>"""
