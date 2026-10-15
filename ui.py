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
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
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
        self.ended: Optional[float] = None
        self.stopped = False

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


FINDING_ORDER = ("test-failure", "install-failure", "undeclared-import", "private-api", "monkeypatch", "yanked", "inactive",
                 "python-requires", "version-lag", "stale-release", "test-warning", "outdated-classifiers", "pypi-unavailable")


def capsule_stats(out_dir: Path) -> Optional[dict]:
    """Numbers and findings for the results view, read from the real capsule JSON (None until it exists)."""
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
            "reason": (p.get("error") or (reasons[0] if reasons else p.get("rationale", "")))[:240],
        })
    rank = {k: i for i, k in enumerate(FINDING_ORDER)}
    findings = sorted(signals, key=lambda s: rank.get(s.get("kind", ""), 99))[:10]
    return {
        "dependencies": len(capsule.get("dependencies", [])),
        "conflicts": len(capsule.get("conflicts", [])),
        "signals": len(signals),
        "flagged": len({s.get("dependency") for s in signals if s.get("dependency")}),
        "usages": sum(len(v) for v in capsule.get("usages", {}).values()),
        "findings": [{"kind": s.get("kind", ""), "dependency": s.get("dependency") or "", "detail": (s.get("detail") or "")[:200],
                      "tag": s.get("tag", ""), "source": s.get("source", "")} for s in findings],
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
    if code is not None and run.ended is None:
        run.ended = time.time()
    if run.start_error:
        state = "failed"
    elif run.stopped:
        state = "stopped"
    elif code is None:
        state = "running"
    else:
        state = "finished" if code == 0 else "failed"
    files = [n for n in sorted(SERVED_FILES) if (run.out_dir / n).is_file()]
    return {
        "id": run.id, "repo": run.repo, "state": state, "returncode": code, "error": run.start_error,
        "elapsed": int((run.ended or time.time()) - run.started),
        "stages": info["stages"], "exit_line": info["exit_line"], "files": files,
        "stats": capsule_stats(run.out_dir),
        "log_tail": "\n".join(text.splitlines()[-60:]),
    }


def stop_run(run: Run) -> bool:
    """Stop a running pipeline (and the pip/pytest processes it started). Returns True if it was running."""
    if run.proc is None or run.proc.poll() is not None:
        return False
    run.stopped = True
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(run.proc.pid), "/T", "/F"], capture_output=True)
    else:
        run.proc.terminate()
    return True


def health(base_url: str = "http://localhost:11434") -> dict:
    """Is the local model server reachable, and does it have a Gemma 4 model?"""
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=2) as resp:
            names = [m.get("name", "") for m in json.loads(resp.read().decode("utf-8")).get("models", [])]
        return {"ollama": True, "models": names, "gemma4": [n for n in names if n.lower().startswith("gemma4")]}
    except (OSError, ValueError) as exc:
        return {"ollama": False, "models": [], "gemma4": [], "error": str(exc)[:160]}


def check_path(raw: str) -> dict:
    """Quick look at a folder the user typed: is it there, and does it look like a Python project?"""
    raw = raw.strip().strip('"')
    if not raw:
        return {"state": "empty"}
    path = Path(raw)
    if not path.is_dir():
        return {"state": "missing"}
    manifests = [n for n in sorted(p.name for p in path.iterdir() if p.is_file())
                 if (n.startswith("requirements") and n.endswith(".txt")) or n == "pyproject.toml"]
    py_files = 0
    for _root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "venv", "env", "node_modules", "__pycache__"}]
        py_files += sum(1 for f in files if f.endswith(".py"))
        if py_files >= 500:
            break
    return {"state": "ok" if manifests and py_files else "weak", "manifests": manifests, "python_files": py_files}


def build_command(repo: str, out_dir: Path, form: dict[str, str]) -> list[str]:
    cmd = [sys.executable, str(ROOT / "run.py"), repo, "--output", str(out_dir)]
    for flag in ("backend", "model", "base-url"):
        value = form.get(flag.replace("-", "_"), "").strip()
        if value:
            cmd += [f"--{flag}", value]
    if form.get("skip_rehearsal") or form.get("mode") == "quick":
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
:root{--bg:#f2f3fc;--card:#ffffff;--fg:#14172b;--muted:#626a8a;--line:#e1e4f4;--soft:#f6f7fe;--acc:#5b5bf0;--acc2:#a855f7;--pink:#ec4899;
--ok:#0f9d58;--bad:#e5484d;--run:#2f7bff;--skip:#c68a00;--shadow:0 10px 30px rgba(60,60,140,.10);
--t-verified:#0f9d58;--t-observed:#2f7bff;--t-proposed:#e08a00;--t-refuted:#e5484d;--t-generated:#a855f7}
:root[data-theme=dark]{--bg:#0b0d19;--card:#141830;--fg:#eceefb;--muted:#9ba2c8;--line:#262b50;--soft:#101429;--acc:#8b8bff;--acc2:#c58bff;--pink:#ff7ab8;
--ok:#3ddc84;--bad:#ff7b80;--run:#6aa6ff;--skip:#f0c04a;--shadow:0 10px 30px rgba(0,0,0,.4);
--t-verified:#3ddc84;--t-observed:#6aa6ff;--t-proposed:#f5b73f;--t-refuted:#ff7b80;--t-generated:#c58bff}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 "Segoe UI",system-ui,-apple-system,sans-serif;transition:background .25s}
.hero{background:radial-gradient(1200px 380px at 85% -10%,rgba(236,72,153,.55),transparent 60%),linear-gradient(135deg,#3730a3 0%,#6d28d9 55%,#a21caf 100%);color:#fff;padding:30px 16px 78px}
.hero .in{max-width:1080px;margin:0 auto}.top{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:12px;font-weight:800;font-size:1.85rem;letter-spacing:-.02em}
.logo{width:44px;height:44px;border-radius:13px;background:rgba(255,255,255,.2);display:grid;place-items:center;font-size:1.45rem}
.tools{display:flex;gap:10px;align-items:center}.pill{display:inline-flex;align-items:center;gap:8px;padding:6px 13px;border-radius:999px;font-size:.8rem;font-weight:700;background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.3)}
.dot{width:10px;height:10px;border-radius:50%;background:#bbb;display:inline-block}.dot.on{background:#4ade80;box-shadow:0 0 0 4px rgba(74,222,128,.25)}.dot.off{background:#fb7185;box-shadow:0 0 0 4px rgba(251,113,133,.25)}
.theme{cursor:pointer;border:1px solid rgba(255,255,255,.3);background:rgba(255,255,255,.16);color:#fff;border-radius:999px;padding:6px 12px;font:inherit;font-weight:700;font-size:.8rem}
.tagline{font-size:1.5rem;margin:22px 0 6px;font-weight:800;letter-spacing:-.01em}.lead{margin:0;opacity:.9;max-width:660px;font-size:1.02rem}
.chips{margin-top:20px;display:flex;flex-wrap:wrap;gap:8px}
.chip{display:inline-flex;align-items:center;gap:7px;padding:5px 12px;border-radius:999px;font-size:.78rem;font-weight:800;background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.3)}
.chip i{width:9px;height:9px;border-radius:50%;display:inline-block}
main{max-width:1080px;margin:-44px auto 0;padding:0 16px 64px;position:relative}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:24px;margin:0 0 18px;box-shadow:var(--shadow)}
h2{margin:0 0 12px;font-size:1.08rem;display:flex;align-items:center;gap:8px}.sub{color:var(--muted);margin:0 0 14px}.hint{color:var(--muted);font-size:.84rem;margin:6px 0 0}
label{display:block;font-weight:700;margin:16px 0 6px;font-size:.88rem}
input[type=text],select{width:100%;padding:12px 14px;border:1.5px solid var(--line);border-radius:12px;background:var(--soft);color:var(--fg);font:inherit;transition:border .15s,box-shadow .15s}
input:focus,select:focus{outline:0;border-color:var(--acc);box-shadow:0 0 0 4px rgba(91,91,240,.18)}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:720px){.row{grid-template-columns:1fr}}
.check-msg{margin-top:8px;font-size:.86rem;font-weight:600;min-height:1.3em}.check-msg.ok{color:var(--ok)}.check-msg.weak{color:var(--skip)}.check-msg.missing{color:var(--bad)}
.recent{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}.recent button{border:1px dashed var(--line);background:transparent;color:var(--acc);border-radius:999px;padding:4px 12px;cursor:pointer;font:inherit;font-size:.8rem;font-weight:600}
.modes{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:6px}@media(max-width:720px){.modes{grid-template-columns:1fr}}
.mode{position:relative;border:1.5px solid var(--line);border-radius:14px;padding:14px 16px;cursor:pointer;background:var(--soft);transition:all .15s}
.mode input{position:absolute;opacity:0}.mode b{display:block}.mode span{color:var(--muted);font-size:.84rem;font-weight:400}
.mode:has(input:checked){border-color:var(--acc);background:linear-gradient(135deg,rgba(91,91,240,.12),rgba(168,85,247,.12));box-shadow:0 0 0 3px rgba(91,91,240,.15)}
details.adv{margin-top:16px}details.adv summary{cursor:pointer;font-weight:700;color:var(--muted)}
button.go{margin-top:22px;padding:14px 30px;border:0;border-radius:14px;color:#fff;font:inherit;font-weight:800;font-size:1.02rem;cursor:pointer;
background:linear-gradient(135deg,var(--acc),var(--acc2) 60%,var(--pink));box-shadow:0 10px 24px rgba(124,58,237,.38);transition:transform .12s,box-shadow .12s}
button.go:hover{transform:translateY(-2px);box-shadow:0 14px 28px rgba(124,58,237,.45)}
button.stop{padding:8px 16px;border-radius:10px;border:1.5px solid var(--bad);background:transparent;color:var(--bad);font:inherit;font-weight:700;cursor:pointer}
.steps{display:grid;grid-template-columns:repeat(6,1fr);gap:12px}@media(max-width:900px){.steps{grid-template-columns:repeat(2,1fr)}}
.step{border:1.5px solid var(--line);border-radius:16px;padding:14px 12px;background:var(--soft);min-height:122px;transition:all .25s}
.step .ic{font-size:1.5rem;line-height:1}.step .nm{font-weight:800;margin-top:8px;font-size:.92rem}.step .st{font-size:.76rem;color:var(--muted);margin-top:3px;word-break:break-word}
.step.ok{border-color:var(--ok);background:linear-gradient(180deg,rgba(15,157,88,.10),transparent)}.step.failed{border-color:var(--bad);background:linear-gradient(180deg,rgba(229,72,77,.10),transparent)}
.step.skipped{border-color:var(--skip);background:linear-gradient(180deg,rgba(198,138,0,.10),transparent)}
.step.running{border-color:var(--run);background:linear-gradient(180deg,rgba(47,123,255,.12),transparent);animation:pulse 1.4s infinite}
.step .badge2{float:right;font-size:.7rem;font-weight:800;border-radius:99px;padding:1px 8px;color:#fff;background:var(--muted)}
.step.ok .badge2{background:var(--ok)}.step.failed .badge2{background:var(--bad)}.step.skipped .badge2{background:var(--skip)}.step.running .badge2{background:var(--run)}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(47,123,255,.4)}50%{box-shadow:0 0 0 8px rgba(47,123,255,0)}}
.bar{height:10px;border-radius:99px;background:var(--line);overflow:hidden;margin:16px 0 6px}.bar div{height:100%;width:0;background:linear-gradient(90deg,var(--acc),var(--acc2),var(--pink));transition:width .6s}
.head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.state{font-weight:800;font-size:1.12rem}.state.ok{color:var(--ok)}.state.failed,.state.stopped{color:var(--bad)}.state.running{color:var(--run)}
.callout{margin-top:14px;border-radius:14px;padding:14px 16px;border:1.5px solid var(--bad);background:rgba(229,72,77,.08)}.callout b{color:var(--bad)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(122px,1fr));gap:12px}
.stat{border-radius:16px;padding:16px;border:1px solid var(--line);background:var(--soft)}.stat b{display:block;font-size:2rem;line-height:1.1;letter-spacing:-.02em}.stat span{color:var(--muted);font-size:.8rem}
.stat.v{background:linear-gradient(135deg,rgba(15,157,88,.14),transparent)}.stat.v b{color:var(--t-verified)}
.stat.u{background:linear-gradient(135deg,rgba(224,138,0,.16),transparent)}.stat.u b{color:var(--t-proposed)}
.stat.r{background:linear-gradient(135deg,rgba(229,72,77,.14),transparent)}.stat.r b{color:var(--t-refuted)}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:.72rem;font-weight:800;color:#fff;white-space:nowrap;cursor:help}
.b-verified{background:var(--t-verified)}.b-unproven{background:var(--t-proposed)}.b-refuted{background:var(--t-refuted)}.b-other{background:var(--muted)}
.b-ai-proposed{background:var(--t-proposed)}.b-observed{background:var(--t-observed)}.b-ai-generated{background:var(--t-generated)}.b-verified-tag{background:var(--t-verified)}
.summary{border-left:4px solid var(--t-generated);padding:6px 0 6px 16px;font-size:1.02rem}
.finds{display:grid;gap:10px}.find{display:grid;grid-template-columns:34px 1fr;gap:12px;border:1px solid var(--line);border-radius:14px;padding:12px 14px;background:var(--soft)}
.find .fi{font-size:1.35rem}.find b{font-size:.92rem}.find p{margin:2px 0 0;font-size:.84rem;color:var(--muted);word-break:break-word}
.props{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:14px}
.prop{border:1px solid var(--line);border-left:6px solid var(--muted);border-radius:14px;padding:14px 16px;background:var(--soft)}
.prop.verified{border-left-color:var(--t-verified)}.prop.unproven{border-left-color:var(--t-proposed)}.prop.refuted{border-left-color:var(--t-refuted)}
.prop h3{margin:0 0 4px;font-size:1rem}.prop .arrow{color:var(--muted);font-weight:600}.prop p{margin:8px 0 0;font-size:.84rem;color:var(--muted)}
.links a{display:inline-block;margin:0 10px 10px 0;padding:10px 16px;border-radius:12px;border:1.5px solid var(--line);color:var(--acc);text-decoration:none;font-weight:700;background:var(--soft)}
.links a:hover{border-color:var(--acc)}
pre{background:#0d1020;color:#d9dcf5;border-radius:12px;padding:14px;overflow:auto;max-height:300px;font-size:.78rem;white-space:pre-wrap;word-break:break-word}
details summary{cursor:pointer;font-weight:700}
iframe{width:100%;height:80vh;border:1px solid var(--line);border-radius:12px;background:#fff}
.legend{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}.legend div{border:1px solid var(--line);border-radius:14px;padding:12px 14px;background:var(--soft);font-size:.84rem}
.legend b{display:block;margin-bottom:2px;font-size:.9rem}.foot{text-align:center;color:var(--muted);font-size:.82rem;margin-top:10px}a{color:var(--acc)}
"""

TAGS = (("VERIFIED", "--t-verified", "a command we ran"), ("OBSERVED", "--t-observed", "real output or PyPI data"),
        ("AI-PROPOSED", "--t-proposed", "Gemma's suggestion, not yet proven"), ("REFUTED", "--t-refuted", "proposed, and the sandbox showed it fails"),
        ("AI-GENERATED", "--t-generated", "prose written by Gemma"))
STEP_INFO = (("&#128269;", "Discover", "Reads your dependency files"), ("&#128225;", "Risk scan", "PyPI data plus your tests in a fresh venv"),
             ("&#129517;", "Usage", "Finds every place the code uses them"), ("&#129302;", "Gemma 4", "Proposes replacements and exact edits"),
             ("&#129514;", "Rehearse", "Tests each proposal in a sandbox"), ("&#128230;", "Capsule", "Writes the report"))

THEME_JS = """<script>
(function(){var t=null;try{t=localStorage.getItem('gm-theme')}catch(e){}
if(!t&&window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches)t='dark';
if(t)document.documentElement.setAttribute('data-theme',t);
window.toggleTheme=function(){var n=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';
document.documentElement.setAttribute('data-theme',n);try{localStorage.setItem('gm-theme',n)}catch(e){}}})();</script>"""


def page(title: str, body: str, script: str = "", hero: str = "") -> bytes:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title>{THEME_JS}<style>{CSS}</style></head><body>{hero}<main>{body}</main>{script}</body></html>").encode("utf-8")


def hero_html(small: bool = False) -> str:
    chips = "".join(f"<span class='chip'><i style='background:var({v})'></i>{t}</span>" for t, v, _ in TAGS)
    lead = "" if small else ("<p class='lead'>Find at-risk Python dependencies, let Gemma 4 propose replacements, then watch each proposal "
                             "get tested in a sandbox. Every claim carries a tag, so you always know what is fact and what is AI.</p>")
    return (f"<div class='hero'><div class='in'><div class='top'><div class='brand'><span class='logo'>&#129516;</span>GemmaMorph</div>"
            f"<div class='tools'><span class='pill' id='health'><i class='dot'></i>checking model...</span>"
            f"<button class='theme' type='button' onclick='toggleTheme()'>&#9728;&#65039; / &#127769;</button></div></div>"
            f"<div class='tagline'>Gemma 4 proposes. Your test suite decides.</div>{lead}<div class='chips'>{chips}</div></div></div>")


HEALTH_JS = """<script>
fetch('/api/health').then(r=>r.json()).then(h=>{const el=document.getElementById('health');if(!el)return;
 if(h.ollama&&h.gemma4.length)el.innerHTML='<i class="dot on"></i>Ollama ready &middot; '+h.gemma4[0].replace(/[<>&]/g,'');
 else if(h.ollama)el.innerHTML='<i class="dot off"></i>Ollama is up, but no Gemma 4 model &middot; run: ollama pull gemma4:e2b';
 else el.innerHTML='<i class="dot off"></i>Ollama not reachable &middot; start it, or use the Gemini backend'}).catch(()=>{});
</script>"""


def steps_html() -> str:
    return "".join(f"<div class='step' id='s{i}' title='{html.escape(d)}'><span class='badge2'>{i + 1}</span><div class='ic'>{ic}</div>"
                   f"<div class='nm'>{n}</div><div class='st'>{html.escape(d)}</div></div>" for i, (ic, n, d) in enumerate(STEP_INFO))


def index_page(runs_dir: Path) -> bytes:
    recent = sorted(RUNS.values(), key=lambda r: r.started, reverse=True)[:5]
    seen, chips = set(), ""
    for r in recent:
        if r.repo not in seen:
            seen.add(r.repo)
            chips += f"<button type='button' data-path='{html.escape(r.repo, quote=True)}'>{html.escape(r.repo)}</button>"
    recent_html = f"<div class='recent'>{chips}</div>" if chips else ""
    legend = "".join(f"<div><b style='color:var({v})'>{t}</b>{html.escape(d)}</div>" for t, v, d in TAGS)
    body = f"""
<div class="card"><h2>&#128640; Start a run</h2>
<p class="sub">Point GemmaMorph at a Python project. Your folder is only read: edits are tried in throwaway copies.</p>
<form method="post" action="/run">
<label for="repo">Project folder</label>
<input id="repo" name="repo" type="text" placeholder="D:\\path\\to\\your\\project" required autofocus autocomplete="off">
<div class="check-msg" id="check"></div>{recent_html}
<label>What do you want?</label>
<div class="modes">
<label class="mode"><input type="radio" name="mode" value="quick" checked><b>&#9889; Quick scan</b><span>Finds risks, traces usage and gets Gemma 4's proposals. Skips the sandbox, so proposals stay unproven.</span></label>
<label class="mode"><input type="radio" name="mode" value="full"><b>&#129514; Full rehearsal</b><span>Also tests every proposal in a fresh environment. Takes much longer, and is what proves or refutes a proposal.</span></label></div>
<details class="adv"><summary>Advanced: model and backend</summary>
<div class="row"><div><label for="backend">Backend</label>
<select id="backend" name="backend"><option value="">Ollama, local (default)</option><option value="gemini">Gemini API (needs GEMINI_API_KEY)</option></select></div>
<div><label for="model">Gemma 4 model</label><input id="model" name="model" type="text" placeholder="gemma4:e2b"></div></div>
<label for="base_url">Base URL</label><input id="base_url" name="base_url" type="text" placeholder="http://localhost:11434"></details>
<button class="go" type="submit">Run GemmaMorph &rarr;</button></form></div>
<div class="card"><h2>&#128260; What happens</h2><div class="steps">{steps_html()}</div></div>
<div class="card"><h2>&#127991;&#65039; Every claim is tagged</h2><div class="legend">{legend}</div></div>
<div class="foot">Gemma 4 &middot; runs locally &middot; nothing leaves your machine in the default mode</div>"""
    script = HEALTH_JS + """<script>
const repo=document.getElementById('repo'),msg=document.getElementById('check');let t=null;
function check(){clearTimeout(t);t=setTimeout(async()=>{const v=repo.value.trim();if(!v){msg.textContent='';msg.className='check-msg';return}
 try{const r=await (await fetch('/api/check?path='+encodeURIComponent(v))).json();
  if(r.state==='ok'){msg.className='check-msg ok';msg.textContent='\\u2713 Python project found: '+r.manifests.join(', ')+' \\u00b7 '+(r.python_files>=500?'500+':r.python_files)+' Python files'}
  else if(r.state==='weak'){msg.className='check-msg weak';msg.textContent='Folder found, but no requirements*.txt / pyproject.toml or no .py files. Results may be thin.'}
  else if(r.state==='missing'){msg.className='check-msg missing';msg.textContent='That folder does not exist.'}
 }catch(e){}},250)}
repo.addEventListener('input',check);document.querySelectorAll('.recent button').forEach(b=>b.onclick=()=>{repo.value=b.dataset.path;check()});
</script>"""
    return page("GemmaMorph", body, script, hero=hero_html())


def run_page(run: Run) -> bytes:
    body = f"""
<div class="card"><div class="head"><div><div class="state running" id="state">Starting...</div><p class="sub" id="repo" style="margin:2px 0 0">{html.escape(run.repo)}</p></div>
<div style="display:flex;gap:12px;align-items:center"><span class="hint" id="clock"></span><button class="stop" id="stop" type="button">Stop</button></div></div>
<div class="bar"><div id="bar"></div></div>
<div class="steps" style="margin-top:14px">{steps_html()}</div>
<div id="help"></div></div>
<div id="results"></div>
<div class="card links" id="links" hidden></div>
<div class="card"><details><summary>Live log</summary><pre id="log" style="margin-top:10px"></pre></details></div>
<div id="viewer"></div><p class="foot"><a href="/">&larr; New run</a></p>"""
    script = HEALTH_JS + f"""<script>
const id={json.dumps(run.id)};let shown=false;
const esc=s=>String(s).replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const cls=v=>({{verified:'b-verified',unproven:'b-unproven',refuted:'b-refuted'}}[v]||'b-other');
const tip={{verified:'Tests that passed before still pass, and the change was exercised',unproven:'No test proved it: the edited lines were not exercised, or there was nothing to compare',refuted:'The sandbox showed this proposal breaks the tests or the install','no proposal':'Gemma 4 did not return a usable proposal',unrehearsed:'The sandbox rehearsal was skipped'}};
const KIND={{'test-failure':['\\u274c','A test fails in a clean environment'],'install-failure':['\\ud83d\\udce6','An install step failed'],'undeclared-import':['\\ud83d\\udccc','Imported but never declared'],'private-api':['\\ud83d\\udd12','Uses a private API'],'monkeypatch':['\\ud83d\\udc12','Monkeypatches the library at runtime'],'yanked':['\\u26d4','Release was yanked on PyPI'],'inactive':['\\ud83d\\udca4','Marked inactive on PyPI'],'python-requires':['\\ud83d\\udc0d','Does not support this Python'],'version-lag':['\\u2b06\\ufe0f','A major version behind'],'stale-release':['\\u23f3','No release for a long time'],'test-warning':['\\u26a0\\ufe0f','Warning seen while testing'],'outdated-classifiers':['\\ud83c\\udff7\\ufe0f','Classifiers look out of date'],'pypi-unavailable':['\\ud83d\\udce1','PyPI could not be reached']}};
function hint(t){{t=t||'';
 if(/Cannot reach ollama|actively refused|Connection refused/i.test(t))return'The model server is not running. Start Ollama, make sure it has the model (ollama pull gemma4:e2b), then run again.';
 if(/GEMINI_API_KEY/.test(t))return'Set the GEMINI_API_KEY environment variable, or switch the backend to Ollama.';
 if(/allocate|out of memory|not enough memory/i.test(t))return'The model could not get enough free memory. Close heavy apps and run again.';
 if(/No module named/i.test(t))return'A Python module is missing. Install the project requirements (pip install -r requirements-gemmamorph.txt).';
 return'';}}
const fmt=s=>Math.floor(s/60)+'m '+String(s%60).padStart(2,'0')+'s';
function results(s){{
  if(!s)return'';const c=s.counts||{{}};
  const stat=(n,l,k)=>'<div class="stat '+(k||'')+'"><b>'+n+'</b><span>'+l+'</span></div>';
  let h='<div class="card"><h2>&#128202; Results'+(s.model&&s.model.name?' <span class="hint">&middot; '+esc(s.model.name)+'</span>':'')+'</h2><div class="stats">'+
    stat(s.dependencies,'dependencies found')+stat(s.signals,'risk signals')+stat(s.flagged,'dependencies flagged')+stat(s.usages,'code usages traced')+
    stat(c.verified||0,'proposals verified','v')+stat(c.unproven||0,'unproven','u')+stat(c.refuted||0,'refuted by the sandbox','r')+'</div></div>';
  if(s.summary)h+='<div class="card"><h2>&#9997;&#65039; Summary <span class="badge b-ai-generated">AI-GENERATED</span></h2><div class="summary">'+esc(s.summary)+'</div></div>';
  else if(s.summary_error)h+='<div class="card"><h2>Summary</h2><p class="hint">unavailable: '+esc(s.summary_error)+'</p></div>';
  if(s.findings.length)h+='<div class="card"><h2>&#128161; Key findings</h2><div class="finds">'+s.findings.map(f=>{{const k=KIND[f.kind]||['\\u2022',f.kind];
    return'<div class="find"><div class="fi">'+k[0]+'</div><div><b>'+esc(k[1])+(f.dependency?' &middot; '+esc(f.dependency):'')+'</b> <span class="badge b-'+(f.tag==='VERIFIED'?'verified-tag':'observed')+'">'+esc(f.tag)+'</span><p>'+esc(f.detail)+'</p></div></div>'}}).join('')+'</div></div>';
  if(s.proposals.length)h+='<div class="card"><h2>&#129302; Gemma 4 proposals and their rehearsal</h2><div class="props">'+
    s.proposals.map(p=>'<div class="prop '+esc(p.verdict)+'"><h3>'+esc(p.dependency)+(p.replacement?' <span class="arrow">&rarr;</span> '+esc(p.replacement):'')+'</h3>'+
    '<span class="badge '+cls(p.verdict)+'" title="'+esc(tip[p.verdict]||'')+'">'+esc(p.verdict)+'</span> <span class="badge b-'+esc((p.tag||'').toLowerCase())+'" title="Claim tag">'+esc(p.tag)+'</span><p>'+esc(p.reason)+'</p></div>').join('')+'</div></div>';
  return h;}}
document.getElementById('stop').onclick=async()=>{{await fetch('/api/stop/'+id,{{method:'POST'}})}};
async function tick(){{
  let d;try{{d=await (await fetch('/api/run/'+id)).json()}}catch(e){{setTimeout(tick,3000);return}}
  const st=document.getElementById('state');st.className='state '+({{running:'running',finished:'ok',failed:'failed',stopped:'stopped'}}[d.state]);
  st.textContent={{running:'Running...',finished:'\\u2705 Finished: all stages completed',stopped:'\\u23f9 Stopped',failed:'\\u26a0\\ufe0f Finished with problems'}}[d.state]+((d.state==='failed'&&d.exit_line)?' ('+d.exit_line+')':'');
  document.getElementById('clock').textContent=fmt(d.elapsed);document.getElementById('stop').style.display=d.state==='running'?'':'none';
  let done=0,problem='';d.stages.forEach((s,i)=>{{const el=document.getElementById('s'+i);el.className='step '+s.state;
    if(s.state!=='pending')el.querySelector('.st').textContent=s.state==='running'?'working...':(s.detail||s.state);if(['ok','failed','skipped'].includes(s.state))done++;
    if(s.state==='failed'&&!problem)problem=s.detail}});
  document.getElementById('bar').style.width=(d.state==='running'?Math.max(done,0.4)/6*100:100)+'%';
  const hm=d.state==='failed'?(hint(problem+' '+d.log_tail)||hint(d.error||'')):'';
  document.getElementById('help').innerHTML=(d.state==='failed'||d.error)?'<div class="callout"><b>What went wrong</b><p style="margin:6px 0 0">'+esc(problem||d.error||'See the log below.')+'</p>'+(hm?'<p style="margin:8px 0 0">&#128161; '+esc(hm)+'</p>':'')+'</div>':'';
  document.getElementById('log').textContent=d.log_tail;document.getElementById('results').innerHTML=results(d.stats);
  if(d.files.length){{const l=document.getElementById('links');l.hidden=false;l.innerHTML='<h2>&#128193; Files</h2>'+d.files.map(f=>'<a href="/files/'+id+'/'+f+'" target="_blank" rel="noopener">'+esc(f)+'</a>').join('');
    if(!shown&&d.files.includes('EXIT_CAPSULE.html')&&d.state!=='running'){{shown=true;document.getElementById('viewer').innerHTML='<div class="card"><h2>&#128214; Exit Capsule</h2><iframe sandbox title="Exit Capsule" src="/files/'+id+'/EXIT_CAPSULE.html"></iframe></div>'}}}}
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
            if path == "/api/health":
                return self._send(200, json.dumps(health()).encode("utf-8"), "application/json")
            if path == "/api/check":
                raw = parse_qs(urlparse(self.path).query).get("path", [""])[0]
                return self._send(200, json.dumps(check_path(raw)).encode("utf-8"), "application/json")
            if len(parts) == 3 and parts[:2] == ["api", "run"] and parts[2] in RUNS:
                return self._send(200, json.dumps(run_state(RUNS[parts[2]])).encode("utf-8"), "application/json")
            if len(parts) == 3 and parts[0] == "files" and parts[1] in RUNS and parts[2] in SERVED_FILES:
                target = RUNS[parts[1]].out_dir / parts[2]
                if target.is_file():
                    ctype = {"html": "text/html; charset=utf-8", "json": "application/json"}.get(target.suffix[1:], "text/plain; charset=utf-8")
                    return self._send(200, target.read_bytes(), ctype)
            self._not_found()

        def do_POST(self) -> None:
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            if len(parts) == 3 and parts[:2] == ["api", "stop"] and parts[2] in RUNS:
                return self._send(200, json.dumps({"stopped": stop_run(RUNS[parts[2]])}).encode("utf-8"), "application/json")
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
