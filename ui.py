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
from urllib.parse import parse_qs, quote, urlparse

ROOT = Path(__file__).resolve().parent
STAGE_NAMES = ("S1 discover", "S2 risk_scan", "S3 usage", "S4 propose", "S5 rehearse", "S6 capsule")
SERVED_FILES = {"EXIT_CAPSULE.html", "EXIT_CAPSULE.md", "EXIT_CAPSULE.json", "proposed.patch"}
RUN_ID = re.compile(r"^[0-9a-f]{12}$")
STATUS_LINE = re.compile(r"^\[(ok|failed|skipped)\s*\]\s+(S\d [a-z_]+)\s+(.*)$")
STAGE_MARK = re.compile(r"^--- (S\d [a-z_]+)")
DURATION = re.compile(r"^\s+\((\d+(?:\.\d+)?)s\)\s*$")

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
        self.imported = False  # a finished report opened from disk, or a run from an earlier session

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
    stages = {name: {"name": name, "state": "pending", "detail": "", "seconds": None} for name in STAGE_NAMES}
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
            stages[m.group(2)].update(state=m.group(1), detail=m.group(3))
            continue
        m = DURATION.match(line)
        if m and current:
            stages[current]["seconds"] = float(m.group(1))
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
        rc = p.get("requirements_change") or {}
        rows.append({
            "dependency": p.get("dependency", ""),
            "replacement": p.get("replacement") or "",
            "verdict": verdict,
            "tag": p.get("tag", ""),
            "reason": (p.get("error") or (reasons[0] if reasons else p.get("rationale", "")))[:240],
            "rationale": (p.get("rationale") or "")[:400],
            "edits": [{"file": e.get("file", ""), "old": e.get("old_text", "")[:700], "new": e.get("new_text", "")[:700]}
                      for e in (p.get("edits") or [])[:4]],
            "remove": rc.get("remove") or [], "add": rc.get("add") or [],
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
        "repo": capsule.get("repo", ""),
        "generated_at": capsule.get("generated_at", ""),
        "stages": [{"stage": s.get("stage", ""), "status": s.get("status", ""),
                    "detail": s.get("error") or s.get("skip_reason") or ""} for s in capsule.get("stages", [])],
    }


def run_state(run: Run) -> dict:
    text = run.log_path.read_text(encoding="utf-8", errors="replace") if run.log_path.is_file() else ""
    info = parse_log(text)
    stats = capsule_stats(run.out_dir)
    code = run.returncode
    if run.imported:
        m = re.search(r"exit code (\d+)", info["exit_line"] or "")
        code = int(m.group(1)) if m else (0 if stats and all(s["status"] != "failed" for s in stats["stages"]) else 1 if stats else None)
        if stats and all(s["state"] == "pending" for s in info["stages"]):  # no log: use the stage list stored in the capsule
            by_suffix = {s["stage"].split(" ", 1)[-1]: s for s in stats["stages"]}
            for st in info["stages"]:
                src = by_suffix.get(st["name"].split(" ", 1)[1])
                if src:
                    st["state"], st["detail"] = src["status"], src["detail"]
            if not by_suffix.get("capsule"):  # the capsule cannot record its own stage; it exists, so S6 completed
                info["stages"][-1]["state"], info["stages"][-1]["detail"] = "ok", "report written"
        run.ended = run.ended or run.started
    if code is not None and run.ended is None:
        run.ended = time.time()
    if run.imported and code is None:
        state = "interrupted"
    elif run.start_error:
        state = "failed"
    elif run.stopped:
        state = "stopped"
    elif code is None:
        state = "running"
    else:
        state = "finished" if code == 0 else "failed"
    files = [n for n in sorted(SERVED_FILES) if (run.out_dir / n).is_file()]
    return {
        "id": run.id, "repo": run.repo, "state": state, "imported": run.imported, "returncode": code, "error": run.start_error,
        "elapsed": int((run.ended or time.time()) - run.started),
        "stages": info["stages"], "exit_line": info["exit_line"], "files": files,
        "stats": stats,
        "log_tail": "\n".join(text.splitlines()[-150:]),
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
        return {"ollama": True, "models": names, "gemma4": [n for n in names if n.lower().startswith("gemma4")],
                "gemini_key": bool(os.environ.get("GEMINI_API_KEY"))}
    except (OSError, ValueError) as exc:
        return {"ollama": False, "models": [], "gemma4": [], "error": str(exc)[:160],
                "gemini_key": bool(os.environ.get("GEMINI_API_KEY"))}


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

STATIC_DIR = ROOT / "ui_static"
STATIC_FILES = {"gemmamorph.css": "text/css; charset=utf-8", "gemmamorph.js": "application/javascript; charset=utf-8"}

ICONS = {
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2"/>',
    "radar": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><path d="M12 12 18.4 5.6"/><circle cx="12" cy="12" r="1.3" fill="currentColor"/>',
    "route": '<circle cx="6" cy="6" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M8.5 6H15a3.5 3.5 0 0 1 0 7H9a3.5 3.5 0 0 0 0 7h6.5"/>',
    "spark": '<path d="M12 3c.6 4.2 2.8 6.4 7 7-4.2.6-6.4 2.8-7 7-.6-4.2-2.8-6.4-7-7 4.2-.6 6.4-2.8 7-7z"/><path d="M19 3v4M21 5h-4"/>',
    "flask": '<path d="M9 3h6M10 3v6L4.8 18.2A2 2 0 0 0 6.5 21h11a2 2 0 0 0 1.7-2.8L14 9V3"/><path d="M7.4 15h9.2"/>',
    "box": '<path d="M21 8 12 3 3 8v8l9 5 9-5z"/><path d="m3 8 9 5 9-5M12 13v8"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "play": '<path d="M7 4.5v15l12-7.5z"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "bolt": '<path d="M13 2 4 14h7l-1 8 9-12h-7z"/>',
    "shield": '<path d="M12 3 4 6v6c0 4.6 3.4 8.3 8 9 4.6-.7 8-4.4 8-9V6z"/><path d="m9 12 2 2 4-4"/>',
    "terminal": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3M13 15h4"/>',
    "chart": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "tag": '<path d="M3 12V4h8l10 10-8 8z"/><circle cx="7.5" cy="7.5" r="1.5"/>',
    "doc": '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/>',
    "cpu": '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
    "grid": '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    "list": '<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1"/><circle cx="4.5" cy="12" r="1"/><circle cx="4.5" cy="18" r="1"/>',
    "menu": '<path d="M4 7h16M4 12h16M4 17h16"/>',
    "wave": '<path d="M2 12c2.5-4 5-4 7.5 0s5 4 7.5 0 3.5-3 5-2"/>',
    "replay": '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/>',
    "status": '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
}
LOGO = ('<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
        '<defs><linearGradient id="gmg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#38bdf8"/>'
        '<stop offset=".55" stop-color="#a78bfa"/><stop offset="1" stop-color="#f472b6"/></linearGradient></defs>'
        '<path d="M12 2.5 20.5 7.3v9.4L12 21.5 3.5 16.7V7.3z" stroke="url(#gmg)"/>'
        '<path d="M7.5 13.6c1.5-3.2 3-3.2 4.5 0s3 3.2 4.5 0" stroke="url(#gmg)"/></svg>')
FAVICON = "data:image/svg+xml," + quote(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="#38bdf8" stroke-width="2" '
    'stroke-linejoin="round"><path d="M12 2.5 20.5 7.3v9.4L12 21.5 3.5 16.7V7.3z"/><path d="M7.5 13.6c1.5-3.2 3-3.2 4.5 0s3 3.2 4.5 0" stroke="#f472b6"/></svg>')
HEAD_JS = ('<script>document.documentElement.classList.add("js");try{if(localStorage.getItem("gm-motion")==="off")'
           'document.documentElement.classList.add("motion-off")}catch(e){}</script>')

TAGS = (("VERIFIED", "t-verified", "A command we ran, with its real exit code and output."),
        ("OBSERVED", "t-observed", "Seen in real run output or in PyPI metadata."),
        ("AI-PROPOSED", "t-proposed", "Gemma 4 suggested it. Not proven yet."),
        ("REFUTED", "t-refuted", "Gemma 4 proposed it and the sandbox showed it fails."),
        ("AI-GENERATED", "t-generated", "Prose written by Gemma 4, such as the summary."))
STAGE_UI = (("search", "Discover", "Reads requirements*.txt and pyproject.toml, flags conflicting pins.", False),
            ("radar", "Risk scan", "PyPI metadata plus your own tests in a fresh virtual environment.", False),
            ("route", "Usage", "Traces imports, calls, private APIs and monkeypatches with ast.", False),
            ("spark", "Gemma 4", "Proposes a replacement and exact edits for each flagged dependency.", True),
            ("flask", "Rehearse", "Tests each proposal alone and combined, with coverage on edited lines.", False),
            ("box", "Capsule", "Writes JSON, Markdown and HTML, with a summary by Gemma 4.", True))
INDEX_NAV = (("#launch", "bolt", "Launch"), ("#open", "replay", "Open a report"), ("#pipeline", "grid", "Pipeline"), ("#tags", "tag", "Claim tags"))
RUN_NAV = (("#status", "status", "Status"), ("#pipeline", "grid", "Pipeline"), ("#terminal", "terminal", "Terminal"),
           ("#results", "chart", "Results"), ("#findings", "list", "Findings"), ("#proposals", "spark", "Proposals"), ("#capsule", "box", "Exit Capsule"))
STATE_DOT = {"running": "running", "finished": "ok", "failed": "failed", "stopped": "stopped", "interrupted": "stopped"}


def icon(name: str) -> str:
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{ICONS[name]}</svg>')


def sidebar(nav: tuple, current_id: Optional[str] = None) -> str:
    links = "".join(f'<a href="{href}">{icon(ic)}<span>{label}</span></a>' for href, ic, label in nav)
    runs = ""
    for r in sorted(RUNS.values(), key=lambda r: r.started, reverse=True)[:6]:
        dot = STATE_DOT.get(run_state(r)["state"], "")
        cur = " cur" if r.id == current_id else ""
        runs += (f'<a class="{cur.strip()}" href="/run/{r.id}" title="{html.escape(r.repo, quote=True)}"><i class="dot {dot}"></i>'
                 f'<span>{html.escape(Path(r.repo).name or r.repo)}</span></a>')
    runs = runs or '<p class="none">No operations yet</p>'
    return f"""<aside class="side" id="side">
<a class="brand" href="/"><span class="mark">{LOGO}</span><span><b>GemmaMorph</b><small>Exit rehearsal engine</small></span></a>
<div><div class="nav-label">Navigate</div><nav class="nav"><a href="/">{icon("grid")}<span>Mission control</span></a>{links}</nav></div>
<div><div class="nav-label">Operations</div><div class="runs">{runs}</div></div>
<div class="sys"><div class="sys-h"><span>SYSTEM</span><span class="clock" id="clock">--:--:--</span></div>
<div class="row"><span>MODEL LINK</span><b data-health><i class="dot"></i>checking</b></div>
<div class="row"><span>ENGINE</span><b>Gemma 4</b></div>
<div class="row"><span>SANDBOX</span><b>fresh venv</b></div>
<div class="row"><span>SOURCE</span><b>read-only</b></div>
<button class="motion" id="motion" type="button" aria-pressed="true">{icon("wave")}<span>Motion on</span></button></div>
</aside>"""


def shell(title: str, page_kind: str, crumb: str, heading: str, main: str, nav: tuple,
          config: Optional[dict] = None, current_id: Optional[str] = None) -> bytes:
    cfg = json.dumps({"page": page_kind, **(config or {})}).replace("<", "\\u003c")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark"><title>{html.escape(title)}</title><link rel="icon" href="{FAVICON}">
{HEAD_JS}<link rel="stylesheet" href="/static/gemmamorph.css"></head>
<body data-page="{page_kind}">
<canvas id="bg" aria-hidden="true"></canvas><div class="mesh" aria-hidden="true"></div><div class="scan-veil" aria-hidden="true"></div>
{sidebar(nav, current_id)}
<div class="shell">
<header class="topbar"><button class="menu" id="menu" type="button" aria-label="Open navigation">{icon("menu")}</button>
<div class="t"><div class="crumb">GemmaMorph <b>//</b> {html.escape(crumb)}</div><div class="tb-title">{html.escape(heading)}</div></div>
<div class="chips"><span class="chip keep" data-health><i class="dot"></i>Model link</span><span class="chip">{icon("shield")}Local-first</span>
<span class="chip">{icon("spark")}Gemma 4</span></div></header>
<main id="main">{main}</main>
<footer class="foot"><b>GemmaMorph</b> · Gemma 4 dependency resilience · Gemma 4 proposes, your test suite decides</footer>
</div>
<div class="toasts" id="toasts" aria-live="polite"></div>
<script>window.GM={cfg};</script><script src="/static/gemmamorph.js"></script>
</body></html>""".encode("utf-8")


def section_head(num: str, kicker: str, title: str, lead: str = "", extra: str = "") -> str:
    lead_html = f'<p class="lead">{lead}</p>' if lead else ""
    return (f'<div class="sec-head"><div><div class="kicker">{num} <i>//</i> {kicker}</div><h2>{title}</h2>{lead_html}</div>{extra}</div>')


def index_page(runs_dir: Path) -> bytes:
    seen, chips = set(), ""
    for r in sorted(RUNS.values(), key=lambda r: r.started, reverse=True):
        if r.repo not in seen and not r.imported and len(seen) < 4:
            seen.add(r.repo)
            chips += f'<button type="button" data-path="{html.escape(r.repo, quote=True)}">{html.escape(r.repo)}</button>'
    recent = f'<div class="recent">{chips}</div>' if chips else ""
    nodes = "".join(
        f'<div class="node{" ai" if ai else ""}"><div class="orb">{icon(ic)}<em>S{i + 1}</em></div><b>{name}</b><p>{html.escape(desc)}</p>'
        f'<span class="kind">{"Gemma 4" if ai else "Deterministic"}</span></div>' for i, (ic, name, desc, ai) in enumerate(STAGE_UI))
    legend = "".join(f'<div class="lg"><span class="tag {cls}">{t}</span><p>{html.escape(d)}</p></div>' for t, cls, d in TAGS)
    main = f"""
<section class="panel hud hero reveal" id="top"><i class="corner tl"></i><i class="corner br"></i>
<div class="over">Gemma 4 · Dependency resilience</div>
<h1>Gemma 4 proposes.<br><span class="grad">Your test suite decides.</span></h1>
<p class="intro">GemmaMorph finds the dependencies that put a Python project at risk, traces every place the code relies on them,
asks Gemma 4 for a replacement with exact edits, and rehearses each proposal in a sandbox against your own tests.</p>
<div class="features"><span class="feature">{icon("spark")}Gemma 4 on your machine</span><span class="feature">{icon("flask")}Sandbox rehearsal</span>
<span class="feature">{icon("shield")}Coverage-checked edits</span><span class="feature">{icon("tag")}Every claim tagged</span></div>
<div class="metrics"><div class="metric"><b>6</b><span>pipeline stages</span></div><div class="metric"><b>4</b><span>stages run without AI</span></div>
<div class="metric"><b>5</b><span>claim tags on every finding</span></div><div class="metric"><b>0</b><span>lines of your code leave this machine (Ollama mode)</span></div></div>
</section>

<section class="panel hud reveal" id="launch"><i class="corner tl"></i><i class="corner br"></i>
{section_head("01", "Launch", "Start an operation", "Point GemmaMorph at a Python project. Your folder is only read: every edit happens in a throwaway copy.",
              '<span class="hint"><span class="kbd">/</span> focus &nbsp;·&nbsp; <span class="kbd">Ctrl</span> + <span class="kbd">Enter</span> launch</span>')}
<div class="launch">
<form id="launch-form" method="post" action="/run">
<div class="field"><label for="repo">Project folder</label><div class="input">{icon("folder")}<input id="repo" name="repo" type="text"
placeholder="D:\\path\\to\\your\\project" required autocomplete="off" spellcheck="false"></div>
<div class="status-line" id="check">Awaiting a folder path</div>{recent}</div>
<div class="field"><span class="field-label">Operation mode</span><div class="modes">
<label class="mode"><input type="radio" name="mode" value="quick" checked><span class="t">{icon("bolt")}Quick scan</span>
<p>Risk scan, usage trace and Gemma 4's proposals. Skips the sandbox, so every proposal stays unrehearsed.</p><span class="eta">Fastest · minutes</span></label>
<label class="mode"><input type="radio" name="mode" value="full"><span class="t">{icon("flask")}Full rehearsal</span>
<p>Adds the sandbox: each proposal is installed in a fresh environment and run against your own tests.</p><span class="eta">Much longer · proves or refutes</span></label>
</div></div>
<details class="adv"><summary>Advanced · model and backend</summary><div class="adv-body plain">
<div><label for="backend">Backend</label><select id="backend" name="backend"><option value="">Ollama, local (default)</option>
<option value="gemini">Gemini API (needs GEMINI_API_KEY)</option></select></div>
<div><label for="model">Gemma 4 model</label><input id="model" name="model" type="text" placeholder="gemma4:e2b" autocomplete="off"></div>
<div class="full"><label for="base_url">Base URL</label><input id="base_url" name="base_url" type="text" placeholder="http://localhost:11434" autocomplete="off"></div>
</div></details>
<div class="actions"><button class="btn" type="submit" id="launch-btn">{icon("play")}<span>Run GemmaMorph</span></button>
<span class="hint">The pipeline runs locally. This page follows it live.</span></div>
</form>
<aside class="preflight" aria-label="Preflight checks"><div class="kicker">Preflight</div>
<div class="check" id="pf-model"><div class="ic">{icon("cpu")}</div><div><small>Model link</small><b>Checking the local model server…</b></div></div>
<div class="check" id="pf-target"><div class="ic">{icon("folder")}</div><div><small>Target</small><b>Awaiting a folder</b></div></div>
<div class="check ok" id="pf-mode"><div class="ic">{icon("bolt")}</div><div><small>Mode</small><b>Quick scan</b></div></div>
<div class="check ok"><div class="ic">{icon("shield")}</div><div><small>Sandbox</small><b>Throwaway copies · your folder is never modified</b></div></div>
<div class="go" id="pf-go"><i class="dot"></i>Checks pending</div></aside>
</div></section>

<section class="panel reveal" id="open">
{section_head("02", "Replay", "Open a finished report", "Already ran GemmaMorph? Load its output folder (the one with EXIT_CAPSULE.json) to see the results instantly. Nothing is re-run.")}
<form class="openrow" method="post" action="/open"><div class="input">{icon("folder")}<input name="folder" type="text" placeholder="D:\\path\\to\\output"
required autocomplete="off" spellcheck="false" aria-label="Output folder"></div><button class="btn ghost" type="submit">{icon("doc")}<span>Open report</span></button></form>
</section>

<section class="panel reveal" id="pipeline">
{section_head("03", "Pipeline", "Six stages. Gemma 4 reasons, code verifies.", "Deterministic code gathers the evidence. Gemma 4 reasons over it. Only a real test run can call a change verified.")}
<div class="circuit"><span class="pulse"></span>{nodes}</div>
</section>

<section class="panel reveal" id="tags">
{section_head("04", "Claim tags", "Every claim says where it came from", "The report never presents an AI suggestion as a fact.")}
<div class="legend">{legend}</div>
</section>"""
    return shell("GemmaMorph · Mission control", "index", "Mission control", "Dependency exit rehearsal", main, INDEX_NAV)


def run_page(run: Run) -> bytes:
    stages = "".join(
        f'<div class="stage" id="s{i}" style="--i:{i}" data-desc="{html.escape(desc, quote=True)}"><div class="ic">{icon(ic)}</div>'
        f'<div class="bd"><b>{name}<em>S{i + 1}</em>{"<span class=ai>GEMMA 4</span>" if ai else ""}</b><p class="detail">{html.escape(desc)}</p></div>'
        f'<div class="right"><span class="st">Pending</span><span class="secs"></span></div></div>' for i, (ic, name, desc, ai) in enumerate(STAGE_UI))
    segs = "".join(f'<span class="seg" id="seg{i}"></span>' for i in range(len(STAGE_UI)))
    main = f"""
<section class="panel hud" id="status"><i class="corner tl"></i><i class="corner br"></i>
<div class="op"><div><span class="live running" id="live"><i class="dot running"></i><span id="live-text">LIVE</span></span>
<h2 id="state-text">Starting the pipeline…</h2><div class="repo">{icon("folder")}<span>{html.escape(run.repo)}</span></div></div>
<div class="op-right"><div class="timer"><small id="timer-label">ELAPSED</small><span id="clock-run">0m 00s</span></div>
<button class="btn danger" id="stop" type="button">{icon("stop")}<span>Stop</span></button></div></div>
<div class="seg-wrap"><div class="seg-meta"><span>Pipeline progress</span><span id="progress-text">0 / 6 stages complete</span></div><div class="segbar">{segs}</div></div>
<div id="alert"></div>
</section>
<div class="ops-grid">
<section class="panel" id="pipeline">{section_head("01", "Pipeline", "Stage telemetry")}<div class="stages">{stages}</div></section>
<section class="panel term-panel" id="terminal"><div class="term-bar"><i style="background:#ef4444"></i><i style="background:#f59e0b"></i><i style="background:#10b981"></i>
<span class="title">gemmamorph · run.py · live output</span><span class="right"><span id="log-count">0 lines</span>
<label><input type="checkbox" id="follow" checked> follow</label></span></div>
<pre class="term-body" id="log"><span class="l-dim">Waiting for output…</span></pre></section>
</div>
<div id="results-area"><section class="panel" id="results">{section_head("03", "Results", "Mission results", "Results appear here as soon as the Exit Capsule is written.")}
<div class="skeleton"><i></i><i></i><i></i></div></section></div>
<div id="files-area"></div>"""
    return shell("GemmaMorph · Operation", "run", f"Operation {run.id}", "Live operation", main, RUN_NAV, {"id": run.id}, run.id)


def message_page(heading: str, text: str) -> bytes:
    main = (f'<section class="panel hud"><i class="corner tl"></i><i class="corner br"></i>{section_head("!!", "Notice", html.escape(heading))}'
            f'<div class="alert" style="margin-top:0"><div class="alert-h">{icon("status")}Details</div><p>{html.escape(text)}</p></div>'
            f'<p style="margin-top:22px"><a class="btn ghost" href="/">{icon("grid")}<span>Back to mission control</span></a></p></section>')
    return shell(f"GemmaMorph · {heading}", "message", "Notice", heading, main, INDEX_NAV)


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
            self._send(HTTPStatus.NOT_FOUND, message_page("Not found", "There is nothing at this address."))

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                return self._send(200, index_page(runs_dir))
            parts = [p for p in path.split("/") if p]
            if len(parts) == 2 and parts[0] == "run" and parts[1] in RUNS and RUN_ID.match(parts[1]):
                return self._send(200, run_page(RUNS[parts[1]]))
            if len(parts) == 2 and parts[0] == "static" and parts[1] in STATIC_FILES:
                return self._send(200, (STATIC_DIR / parts[1]).read_bytes(), STATIC_FILES[parts[1]])
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
            if urlparse(self.path).path == "/open":
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(min(length, 65536)).decode("utf-8", "replace")).items()}
                try:
                    run = open_report(form.get("folder", ""))
                except ValueError as exc:
                    return self._send(400, message_page("Cannot open that report", str(exc)))
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", f"/run/{run.id}")
                self.end_headers()
                return
            if urlparse(self.path).path != "/run":
                return self._not_found()
            length = int(self.headers.get("Content-Length") or 0)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(min(length, 65536)).decode("utf-8", "replace")).items()}
            try:
                run = start_run(form, runs_dir, command_builder)
            except ValueError as exc:
                return self._send(400, message_page("Cannot start the run", str(exc)))
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", f"/run/{run.id}")
            self.end_headers()

    return Handler


def open_report(raw: str) -> Run:
    """Show a finished report from disk (a folder containing EXIT_CAPSULE.json) without running anything."""
    folder = Path(raw.strip().strip('"'))
    if not (folder / "EXIT_CAPSULE.json").is_file():
        raise ValueError(f"no EXIT_CAPSULE.json in: {raw or '(empty)'}")
    stats = capsule_stats(folder)
    run = Run(uuid.uuid4().hex[:12], (stats or {}).get("repo") or str(folder), folder, [])
    run.imported = True
    with LOCK:
        RUNS[run.id] = run
    return run


def restore_runs(runs_dir: Path) -> None:
    """Bring back runs from earlier sessions so their results stay one click away."""
    for d in sorted(runs_dir.iterdir()) if runs_dir.is_dir() else []:
        log = d / "run.log"
        if RUN_ID.match(d.name) and log.is_file() and d.name not in RUNS:
            first = log.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
            m = re.search(r"repo=(.+?)\s+model=", first)
            run = Run(d.name, m.group(1) if m else str(d), d, [])
            run.imported, run.started = True, log.stat().st_mtime
            RUNS[d.name] = run


def serve(host: str, port: int, runs_dir: Path, command_builder=build_command) -> ThreadingHTTPServer:
    runs_dir.mkdir(parents=True, exist_ok=True)
    restore_runs(runs_dir)
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
