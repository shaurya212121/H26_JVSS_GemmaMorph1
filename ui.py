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
:root{--bg:#f6f7f9;--fg:#15181d;--muted:#5b6472;--card:#fff;--line:#d9dde3;--ok:#177a3c;--bad:#b3261e;--run:#1a5fb4;--skip:#7a6a00;--acc:#3b4cca}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e8eaee;--muted:#9aa3b2;--card:#1c1f25;--line:#2e333c;--ok:#4cc77a;--bad:#ff7b72;--run:#79b0ff;--skip:#e3c35a;--acc:#8e9bff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px 64px}h1{font-size:1.5rem;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:0 0 16px}
label{display:block;font-weight:600;margin:12px 0 4px}input[type=text],select{width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:640px){.row{grid-template-columns:1fr}}
button{margin-top:16px;padding:10px 18px;border:0;border-radius:6px;background:var(--acc);color:#fff;font:inherit;font-weight:600;cursor:pointer}
.hint{color:var(--muted);font-size:.85rem}.stage{display:grid;grid-template-columns:130px 90px 1fr;gap:10px;padding:8px 0;border-top:1px solid var(--line)}
.stage:first-child{border-top:0}.pill{font-weight:600}.ok{color:var(--ok)}.failed{color:var(--bad)}.running{color:var(--run)}.skipped{color:var(--skip)}.pending{color:var(--muted)}
pre{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:10px;overflow:auto;max-height:320px;font-size:.8rem;white-space:pre-wrap;word-break:break-word}
a{color:var(--acc)}iframe{width:100%;height:78vh;border:1px solid var(--line);border-radius:8px;background:#fff}
.tags span{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:1px 9px;margin:2px 4px 2px 0;font-size:.78rem}
"""


def page(title: str, body: str, script: str = "") -> bytes:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>{body}</main>{script}</body></html>").encode("utf-8")


def index_page(runs_dir: Path) -> bytes:
    recent = sorted(RUNS.values(), key=lambda r: r.started, reverse=True)[:8]
    items = "".join(
        f"<li><a href='/run/{r.id}'>{html.escape(r.repo)}</a> <span class='hint'>({run_state(r)['state']})</span></li>" for r in recent)
    body = f"""
<h1>GemmaMorph</h1>
<p class="sub">Find at-risk Python dependencies, ask Gemma 4 for replacements, rehearse them in a sandbox.</p>
<div class="card"><form method="post" action="/run">
<label for="repo">Repository folder</label>
<input id="repo" name="repo" type="text" placeholder="D:\\path\\to\\the\\repo" required>
<p class="hint">The folder is only read; edits are tried in throwaway copies.</p>
<div class="row"><div><label for="backend">Model backend</label>
<select id="backend" name="backend"><option value="">ollama (local, default)</option><option value="ollama">ollama</option><option value="gemini">gemini (needs GEMINI_API_KEY)</option></select></div>
<div><label for="model">Model (optional)</label><input id="model" name="model" type="text" placeholder="gemma4:e2b"></div></div>
<label for="base_url">Base URL (optional)</label><input id="base_url" name="base_url" type="text" placeholder="http://localhost:11434">
<label><input type="checkbox" name="skip_rehearsal" value="1"> Skip the sandbox rehearsal (S5)</label>
<button type="submit">Run GemmaMorph</button></form></div>
<div class="card tags"><b>Every claim is tagged:</b> <span>VERIFIED</span><span>OBSERVED</span><span>AI-PROPOSED</span><span>REFUTED</span><span>AI-GENERATED</span></div>
{('<div class="card"><b>Recent runs</b><ul>' + items + '</ul></div>') if items else ''}"""
    return page("GemmaMorph", body)


def run_page(run: Run) -> bytes:
    body = f"""
<h1>Run <span class="hint">{html.escape(run.id)}</span></h1>
<p class="sub" id="repo">{html.escape(run.repo)}</p>
<div class="card"><div id="state" class="pill running">starting...</div><div id="stages"></div></div>
<div class="card" id="links" hidden></div>
<div class="card"><b>Log</b><pre id="log"></pre></div>
<div id="viewer"></div><p><a href="/">New run</a></p>"""
    script = f"""<script>
const id={json.dumps(run.id)};let shown=false;
function esc(s){{return String(s).replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]))}}
async function tick(){{
  let d;try{{d=await (await fetch('/api/run/'+id)).json()}}catch(e){{setTimeout(tick,3000);return}}
  const st=document.getElementById('state');st.className='pill '+({{running:'running',finished:'ok',failed:'failed'}}[d.state]);
  st.textContent=d.state==='running'?'running...':(d.state==='finished'?'finished (exit 0)':'finished with failures'+(d.exit_line?' - '+d.exit_line:'')+(d.error?' - '+d.error:''));
  document.getElementById('stages').innerHTML=d.stages.map(s=>'<div class="stage"><span>'+esc(s.name)+'</span><span class="pill '+s.state+'">'+s.state+'</span><span>'+esc(s.detail)+'</span></div>').join('');
  document.getElementById('log').textContent=d.log_tail;
  if(d.files.length){{const l=document.getElementById('links');l.hidden=false;l.innerHTML='<b>Results</b> '+d.files.map(f=>'<a href="/files/'+id+'/'+f+'" target="_blank" rel="noopener">'+esc(f)+'</a>').join(' &middot; ');
    if(!shown&&d.files.includes('EXIT_CAPSULE.html')&&d.state!=='running'){{shown=true;document.getElementById('viewer').innerHTML='<iframe sandbox title="Exit Capsule" src="/files/'+id+'/EXIT_CAPSULE.html"></iframe>'}}}}
  if(d.state==='running')setTimeout(tick,2000);
}}
tick();</script><noscript><p>Enable JavaScript to see live progress.</p></noscript>"""
    return page("GemmaMorph run", body, script)


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
