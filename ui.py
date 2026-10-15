#!/usr/bin/env python3
"""
GemmaMorph local web UI (standard library only, bound to 127.0.0.1).

Pick a repo and a Gemma 4 backend, start a run, watch the six stages finish,
then open the Exit Capsule. The UI does not analyse anything itself: it starts
`run.py` as a subprocess and shows its real output and exit code.

    python ui.py            # then open http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
STAGE_NAMES = ("S1 discover", "S2 risk_scan", "S3 usage", "S4 propose", "S5 rehearse", "S6 capsule")
SERVED_FILES = {"EXIT_CAPSULE.html", "EXIT_CAPSULE.md", "EXIT_CAPSULE.json", "proposed.patch"}
RUN_ID = re.compile(r"^[0-9a-f]{12}$")
STATUS_LINE = re.compile(r"^\[(ok|failed|skipped)\s*\]\s+(S\d [a-z_]+)\s+(.*)$")
STAGE_MARK = re.compile(r"^--- (S\d [a-z_]+)")

RUNS: dict[str, "Run"] = {}
LOCK = threading.Lock()


class Run:
    def __init__(self, run_id: str, repo: str, out_dir: Path, command: list[str]):
        self.id, self.repo, self.out_dir, self.command = run_id, repo, out_dir, command
        self.log_path = out_dir / "run.log"
        self.started = time.time()
        self.proc: Optional[subprocess.Popen] = None
        self.start_error: Optional[str] = None

    def start(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        try:
            log = open(self.log_path, "wb")
            self.proc = subprocess.Popen(self.command, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)
        except OSError as exc:
            self.start_error = f"could not start run.py: {exc}"

    @property
    def returncode(self) -> Optional[int]:
        return None if self.proc is None else self.proc.poll()


def parse_log(text: str) -> dict:
    """Turn run.py's real output into stage states. Nothing here is invented: unknown stays 'pending'."""
    stages = {name: {"name": name, "state": "pending", "detail": ""} for name in STAGE_NAMES}
    current = None
    outputs, exit_line = [], None
    for line in text.splitlines():
        m = STAGE_MARK.match(line)
        if m and m.group(1) in stages:
            current = m.group(1)
            if stages[current]["state"] == "pending":
                stages[current]["state"] = "running"
            continue
        m = STATUS_LINE.match(line.strip())
        if m and m.group(2) in stages:
            stages[m.group(2)] = {"name": m.group(2), "state": m.group(1), "detail": m.group(3)}
            continue
        if line.startswith("output: "):
            outputs.append(line[len("output: "):].strip())
        elif line.startswith("exit code "):
            exit_line = line.strip()
    return {"stages": list(stages.values()), "current": current, "outputs": outputs, "exit_line": exit_line}


def capsule_stats(out_dir: Path) -> Optional[dict]:
    """Numbers for the results cards, read from the real capsule JSON (None until it exists)."""
    path = out_dir / "EXIT_CAPSULE.json"
    if not path.is_file():
        return None
    try:
        capsule = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    signals = capsule.get("signals", [])
    proposals = capsule.get("proposals", [])
    counts: dict[str, int] = {}
    rows = []
    for p in proposals:
        verdict = "no proposal" if p.get("error") else p.get("verdict", "unrehearsed")
        counts[verdict] = counts.get(verdict, 0) + 1
        reasons = p.get("verdict_reasons") or []
        rows.append({
            "dependency": p.get("dependency", ""),
            "replacement": p.get("replacement") or "",
            "verdict": verdict,
            "tag": p.get("tag", ""),
            "reason": (p.get("error") or (reasons[0] if reasons else ""))[:220],
        })
    return {
        "dependencies": len(capsule.get("dependencies", [])),
        "conflicts": len(capsule.get("conflicts", [])),
        "signals": len(signals),
        "flagged": len({s.get("dependency") for s in signals if s.get("dependency")}),
        "usages": sum(len(v) for v in capsule.get("usages", {}).values()),
        "proposals": rows,
        "counts": counts,
        "summary": capsule.get("summary"),
        "summary_error": capsule.get("summary_error"),
        "model": capsule.get("model", {}),
    }


def run_state(run: Run) -> dict:
    text = run.log_path.read_text(encoding="utf-8", errors="replace") if run.log_path.is_file() else ""
    info = parse_log(text)
    code = run.returncode
    if run.start_error:
        state = "failed"
    elif code is None:
        state = "running"
    else:
        state = "finished" if code == 0 else "failed"
    files = [n for n in sorted(SERVED_FILES) if (run.out_dir / n).is_file()]
    return {
        "id": run.id, "repo": run.repo, "state": state, "returncode": code, "error": run.start_error,
        "stages": info["stages"], "exit_line": info["exit_line"], "files": files,
        "stats": capsule_stats(run.out_dir),
        "log_tail": "\n".join(text.splitlines()[-60:]),
    }


def build_command(repo: str, out_dir: Path, form: dict[str, str]) -> list[str]:
    cmd = [sys.executable, str(ROOT / "run.py"), repo, "--output", str(out_dir)]
    for flag in ("backend", "model", "base-url"):
        value = form.get(flag.replace("-", "_"), "").strip()
        if value:
            cmd += [f"--{flag}", value]
    if form.get("skip_rehearsal"):
        cmd.append("--skip-rehearsal")
    return cmd


def start_run(form: dict[str, str], runs_dir: Path, command_builder=build_command) -> Run:
    repo = form.get("repo", "").strip()
    if not repo or not Path(repo).is_dir():
        raise ValueError(f"repo folder not found: {repo or '(empty)'}")
    run_id = uuid.uuid4().hex[:12]
    run = Run(run_id, repo, runs_dir / run_id, [])
    run.command = command_builder(repo, run.out_dir, form)
    with LOCK:
        RUNS[run_id] = run
    run.start()
    return run


# --------------------------------------------------------------------- pages

CSS = """
:root{--bg:#f3f4fb;--card:#ffffff;--fg:#14172b;--muted:#636a88;--line:#e2e5f2;--acc:#5b5bf0;--acc2:#9b5cf6;
--ok:#12a150;--bad:#e5484d;--run:#2f7bff;--skip:#c68a00;--shadow:0 6px 24px rgba(40,44,110,.08);
--t-verified:#12a150;--t-observed:#2f7bff;--t-proposed:#d98c00;--t-refuted:#e5484d;--t-generated:#9b5cf6}
@media (prefers-color-scheme:dark){:root{--bg:#0c0e1a;--card:#151831;--fg:#eceefb;--muted:#9aa1c6;--line:#262b4f;--acc:#8b8bff;--acc2:#c08bff;
--ok:#3ddc84;--bad:#ff7b80;--run:#6aa6ff;--skip:#f0c04a;--shadow:0 6px 24px rgba(0,0,0,.35);
--t-verified:#3ddc84;--t-observed:#6aa6ff;--t-proposed:#f0b23a;--t-refuted:#ff7b80;--t-generated:#c08bff}}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 "Segoe UI",system-ui,-apple-system,sans-serif}
.hero{background:linear-gradient(135deg,#4338ca 0%,#7c3aed 52%,#db2777 100%);color:#fff;padding:38px 16px 64px}
.hero .in{max-width:1040px;margin:0 auto}
.brand{display:flex;align-items:center;gap:12px;font-weight:800;font-size:1.9rem;letter-spacing:-.02em}
.logo{width:42px;height:42px;border-radius:12px;background:rgba(255,255,255,.18);display:grid;place-items:center;font-size:1.4rem;backdrop-filter:blur(4px)}
.tagline{font-size:1.15rem;margin:10px 0 4px;font-weight:600}.lead{margin:0;opacity:.88;max-width:640px}
.chips{margin-top:18px;display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-flex;align-items:center;gap:6px;padding:4px 11px;border-radius:999px;font-size:.78rem;font-weight:700;background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.28)}
.chip i{width:9px;height:9px;border-radius:50%;display:inline-block}
main{max-width:1040px;margin:-38px auto 0;padding:0 16px 64px;position:relative}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px;margin:0 0 18px;box-shadow:var(--shadow)}
h2{margin:0 0 12px;font-size:1.05rem;letter-spacing:.01em}.sub{color:var(--muted);margin:0 0 14px}
label{display:block;font-weight:600;margin:14px 0 5px;font-size:.88rem}
input[type=text],select{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--fg);font:inherit}
input:focus,select:focus{outline:2px solid var(--acc);border-color:transparent}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:700px){.row{grid-template-columns:1fr}}
.check{display:flex;gap:9px;align-items:center;font-weight:500;margin-top:16px}.check input{width:18px;height:18px;accent-color:var(--acc)}
button.go{margin-top:20px;padding:13px 26px;border:0;border-radius:12px;color:#fff;font:inherit;font-weight:700;font-size:1rem;cursor:pointer;
background:linear-gradient(135deg,var(--acc),var(--acc2));box-shadow:0 8px 20px rgba(91,91,240,.35);transition:transform .12s}
button.go:hover{transform:translateY(-1px)}.hint{color:var(--muted);font-size:.82rem;margin:6px 0 0}
.steps{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}@media(max-width:860px){.steps{grid-template-columns:repeat(2,1fr)}}
.step{position:relative;border:1px solid var(--line);border-radius:14px;padding:14px 12px;background:var(--bg);min-height:104px}
.step .n{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;font-weight:800;font-size:.8rem;background:var(--line);color:var(--muted)}
.step .nm{font-weight:700;margin-top:8px;font-size:.9rem}.step .st{font-size:.76rem;color:var(--muted);margin-top:2px;word-break:break-word}
.step.ok{border-color:var(--ok)}.step.ok .n{background:var(--ok);color:#fff}.step.failed{border-color:var(--bad)}.step.failed .n{background:var(--bad);color:#fff}
.step.skipped{border-color:var(--skip)}.step.skipped .n{background:var(--skip);color:#fff}
.step.running{border-color:var(--run);animation:pulse 1.4s infinite}.step.running .n{background:var(--run);color:#fff}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(47,123,255,.35)}50%{box-shadow:0 0 0 7px rgba(47,123,255,0)}}
.bar{height:8px;border-radius:99px;background:var(--line);overflow:hidden;margin:16px 0 4px}.bar div{height:100%;width:0;background:linear-gradient(90deg,var(--acc),var(--acc2));transition:width .5s}
.state{font-weight:700;font-size:1.05rem}.state.ok{color:var(--ok)}.state.failed{color:var(--bad)}.state.running{color:var(--run)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.stat{border-radius:14px;padding:16px;border:1px solid var(--line);background:var(--bg)}.stat b{display:block;font-size:1.9rem;line-height:1.1}.stat span{color:var(--muted);font-size:.8rem}
.stat.v b{color:var(--t-verified)}.stat.u b{color:var(--t-proposed)}.stat.r b{color:var(--t-refuted)}
table{width:100%;border-collapse:collapse;font-size:.88rem}th{text-align:left;color:var(--muted);font-weight:600;padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:10px;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border-bottom:0}.tbl{overflow-x:auto}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:.74rem;font-weight:800;color:#fff;white-space:nowrap}
.b-verified{background:var(--t-verified)}.b-unproven{background:var(--t-proposed)}.b-refuted{background:var(--t-refuted)}.b-other{background:var(--muted)}
.b-ai-proposed{background:var(--t-proposed)}.b-observed{background:var(--t-observed)}.b-ai-generated{background:var(--t-generated)}
.summary{border-left:4px solid var(--t-generated);padding:4px 0 4px 14px}
.links a{display:inline-block;margin:0 8px 8px 0;padding:8px 14px;border-radius:10px;border:1px solid var(--line);color:var(--acc);text-decoration:none;font-weight:600;background:var(--bg)}
.links a:hover{border-color:var(--acc)}
pre{background:#0d1020;color:#d9dcf5;border-radius:12px;padding:14px;overflow:auto;max-height:300px;font-size:.78rem;white-space:pre-wrap;word-break:break-word}
details summary{cursor:pointer;font-weight:700}
iframe{width:100%;height:80vh;border:1px solid var(--line);border-radius:12px;background:#fff}
.legend{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}.legend div{border:1px solid var(--line);border-radius:12px;padding:10px 12px;background:var(--bg);font-size:.84rem}
.legend b{display:block;margin-bottom:2px}.foot{text-align:center;color:var(--muted);font-size:.82rem;margin-top:8px}a{color:var(--acc)}
"""

TAGS = (("VERIFIED", "--t-verified", "a command we ran"), ("OBSERVED", "--t-observed", "real output or PyPI data"),
        ("AI-PROPOSED", "--t-proposed", "Gemma's suggestion, not yet proven"), ("REFUTED", "--t-refuted", "proposed, and the sandbox showed it fails"),
        ("AI-GENERATED", "--t-generated", "prose written by Gemma"))


def page(title: str, body: str, script: str = "", hero: str = "") -> bytes:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body>{hero}<main>{body}</main>{script}</body></html>").encode("utf-8")


def hero_html(small: bool = False) -> str:
    chips = "".join(f"<span class='chip'><i style='background:var({v})'></i>{t}</span>" for t, v, _ in TAGS)
    lead = "" if small else ("<p class='lead'>Finds at-risk Python dependencies, asks Gemma 4 for replacements, then "
                             "rehearses each proposal in a sandbox. Every claim carries a tag.</p>")
    return (f"<div class='hero'><div class='in'><div class='brand'><span class='logo'>&#129516;</span>GemmaMorph</div>"
            f"<div class='tagline'>Gemma 4 proposes. Your test suite decides.</div>{lead}<div class='chips'>{chips}</div></div></div>")


def steps_html() -> str:
    return "".join(f"<div class='step' id='s{i}'><div class='n'>{i + 1}</div><div class='nm'>{html.escape(n.split(' ', 1)[1].replace('_', ' '))}</div>"
                   f"<div class='st'>waiting</div></div>" for i, n in enumerate(STAGE_NAMES))


def index_page(runs_dir: Path) -> bytes:
    recent = sorted(RUNS.values(), key=lambda r: r.started, reverse=True)[:8]
    items = "".join(f"<li><a href='/run/{r.id}'>{html.escape(r.repo)}</a> <span class='hint'>({run_state(r)['state']})</span></li>" for r in recent)
    legend = "".join(f"<div><b style='color:var({v})'>{t}</b>{html.escape(d)}</div>" for t, v, d in TAGS)
    body = f"""
<div class="card"><h2>Start a run</h2>
<p class="sub">Point GemmaMorph at a Python repository. Your folder is only read; edits are tried in throwaway copies.</p>
<form method="post" action="/run">
<label for="repo">Repository folder</label>
<input id="repo" name="repo" type="text" placeholder="D:\\path\\to\\the\\repo" required autofocus>
<div class="row"><div><label for="backend">Model backend</label>
<select id="backend" name="backend"><option value="">Ollama, local (default)</option><option value="gemini">Gemini API (needs GEMINI_API_KEY)</option></select></div>
<div><label for="model">Gemma 4 model (optional)</label><input id="model" name="model" type="text" placeholder="gemma4:e2b"></div></div>
<label for="base_url">Base URL (optional)</label><input id="base_url" name="base_url" type="text" placeholder="http://localhost:11434">
<label class="check"><input type="checkbox" name="skip_rehearsal" value="1"> Skip the sandbox rehearsal (faster, proposals stay unrehearsed)</label>
<button class="go" type="submit">Run GemmaMorph &rarr;</button></form></div>
<div class="card"><h2>What happens</h2><div class="steps">{steps_html()}</div>
<p class="hint">Discover reads the manifests; risk scan uses PyPI data and your tests in a fresh venv; usage traces the code with ast; Gemma 4 proposes; the sandbox tests each proposal; the capsule reports it all.</p></div>
<div class="card"><h2>Every claim is tagged</h2><div class="legend">{legend}</div></div>
{('<div class="card"><h2>Recent runs</h2><ul>' + items + '</ul></div>') if items else ''}
<div class="foot">Gemma 4 &middot; runs locally &middot; nothing leaves your machine in the default mode</div>"""
    return page("GemmaMorph", body, hero=hero_html())


def run_page(run: Run) -> bytes:
    body = f"""
<div class="card"><div class="state running" id="state">starting...</div>
<p class="sub" id="repo">{html.escape(run.repo)}</p>
<div class="bar"><div id="bar"></div></div>
<div class="steps" style="margin-top:14px">{steps_html()}</div></div>
<div id="results"></div>
<div class="card links" id="links" hidden></div>
<div class="card"><details><summary>Live log</summary><pre id="log" style="margin-top:10px"></pre></details></div>
<div id="viewer"></div><p class="foot"><a href="/">&larr; New run</a></p>"""
    script = f"""<script>
const id={json.dumps(run.id)};let shown=false;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const cls=v=>({{verified:'b-verified',unproven:'b-unproven',refuted:'b-refuted'}}[v]||'b-other');
function results(s){{
  if(!s)return'';const c=s.counts||{{}};
  const stat=(n,l,k)=>'<div class="stat '+(k||'')+'"><b>'+n+'</b><span>'+l+'</span></div>';
  let h='<div class="card"><h2>Results'+(s.model&&s.model.name?' <span class="hint">&middot; '+esc(s.model.name)+'</span>':'')+'</h2><div class="stats">'+
    stat(s.dependencies,'dependencies found')+stat(s.signals,'risk signals')+stat(s.flagged,'dependencies flagged')+stat(s.usages,'code usages traced')+
    stat(c.verified||0,'proposals verified','v')+stat(c.unproven||0,'unproven','u')+stat(c.refuted||0,'refuted by the sandbox','r')+'</div></div>';
  if(s.summary)h+='<div class="card"><h2>Summary <span class="badge b-ai-generated">AI-GENERATED</span></h2><div class="summary">'+esc(s.summary)+'</div></div>';
  else if(s.summary_error)h+='<div class="card"><h2>Summary</h2><p class="hint">unavailable: '+esc(s.summary_error)+'</p></div>';
  if(s.proposals.length)h+='<div class="card"><h2>Gemma 4 proposals and their rehearsal</h2><div class="tbl"><table><tr><th>Dependency</th><th>Replacement</th><th>Rehearsal verdict</th><th>Why</th></tr>'+
    s.proposals.map(p=>'<tr><td><b>'+esc(p.dependency)+'</b></td><td>'+esc(p.replacement||'-')+'</td><td><span class="badge '+cls(p.verdict)+'">'+esc(p.verdict)+'</span> <span class="badge b-'+esc((p.tag||'').toLowerCase())+'">'+esc(p.tag)+'</span></td><td>'+esc(p.reason)+'</td></tr>').join('')+'</table></div></div>';
  return h;}}
async function tick(){{
  let d;try{{d=await (await fetch('/api/run/'+id)).json()}}catch(e){{setTimeout(tick,3000);return}}
  const st=document.getElementById('state');st.className='state '+({{running:'running',finished:'ok',failed:'failed'}}[d.state]);
  st.textContent=d.state==='running'?'Running...':(d.state==='finished'?'Finished: all stages completed':'Finished with failures'+(d.exit_line?' ('+d.exit_line+')':'')+(d.error?' - '+d.error:''));
  let done=0;d.stages.forEach((s,i)=>{{const el=document.getElementById('s'+i);el.className='step '+s.state;
    el.querySelector('.st').textContent=s.state==='pending'?'waiting':(s.detail||s.state);if(['ok','failed','skipped'].includes(s.state))done++;}});
  document.getElementById('bar').style.width=(d.state==='running'?Math.max(done,0.4)/6*100:100)+'%';
  document.getElementById('log').textContent=d.log_tail;document.getElementById('results').innerHTML=results(d.stats);
  if(d.files.length){{const l=document.getElementById('links');l.hidden=false;l.innerHTML='<h2>Files</h2>'+d.files.map(f=>'<a href="/files/'+id+'/'+f+'" target="_blank" rel="noopener">'+esc(f)+'</a>').join('');
    if(!shown&&d.files.includes('EXIT_CAPSULE.html')&&d.state!=='running'){{shown=true;document.getElementById('viewer').innerHTML='<div class="card"><h2>Exit Capsule</h2><iframe sandbox title="Exit Capsule" src="/files/'+id+'/EXIT_CAPSULE.html"></iframe></div>'}}}}
  if(d.state==='running')setTimeout(tick,2000);
}}
tick();</script><noscript><p>Enable JavaScript to see live progress.</p></noscript>"""
    return page("GemmaMorph run", body, script, hero=hero_html(small=True))


# ---------------------------------------------------------------------- server

def make_handler(runs_dir: Path, command_builder):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _not_found(self) -> None:
            self._send(HTTPStatus.NOT_FOUND, page("Not found", "<h1>Not found</h1><p><a href='/'>Home</a></p>"))

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                return self._send(200, index_page(runs_dir))
            parts = [p for p in path.split("/") if p]
            if len(parts) == 2 and parts[0] == "run" and parts[1] in RUNS and RUN_ID.match(parts[1]):
                return self._send(200, run_page(RUNS[parts[1]]))
            if len(parts) == 3 and parts[:2] == ["api", "run"] and parts[2] in RUNS:
                return self._send(200, json.dumps(run_state(RUNS[parts[2]])).encode("utf-8"), "application/json")
            if len(parts) == 3 and parts[0] == "files" and parts[1] in RUNS and parts[2] in SERVED_FILES:
                target = RUNS[parts[1]].out_dir / parts[2]
                if target.is_file():
                    ctype = {"html": "text/html; charset=utf-8", "json": "application/json"}.get(target.suffix[1:], "text/plain; charset=utf-8")
                    return self._send(200, target.read_bytes(), ctype)
            self._not_found()

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/run":
                return self._not_found()
            length = int(self.headers.get("Content-Length") or 0)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(min(length, 65536)).decode("utf-8", "replace")).items()}
            try:
                run = start_run(form, runs_dir, command_builder)
            except ValueError as exc:
                return self._send(400, page("Error", f"<h1>Cannot start</h1><p>{html.escape(str(exc))}</p><p><a href='/'>Back</a></p>"))
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", f"/run/{run.id}")
            self.end_headers()

    return Handler


def serve(host: str, port: int, runs_dir: Path, command_builder=build_command) -> ThreadingHTTPServer:
    runs_dir.mkdir(parents=True, exist_ok=True)
    return ThreadingHTTPServer((host, port), make_handler(runs_dir, command_builder))


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="GemmaMorph local web UI")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--runs-dir", default=str(ROOT / "ui_runs"), help="where run logs and capsules are written")
    p.add_argument("--no-browser", action="store_true")
    args = p.parse_args(argv)
    server = serve("127.0.0.1", args.port, Path(args.runs_dir))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"GemmaMorph UI at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
