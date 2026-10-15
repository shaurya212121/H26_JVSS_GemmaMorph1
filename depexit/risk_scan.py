"""
Stage 2: Risk scan. No AI.

Two kinds of evidence, each signal carries its source and tag:
  * PyPI metadata (live JSON API): stale last release, yanked releases,
    "Inactive" status, outdated classifiers, requires_python that excludes the
    running interpreter, pinned version a major version behind.   -> [OBSERVED]
  * Observed evidence: the target's own tests are run in a fresh venv built from
    its requirements; warnings, import errors and failures are mapped to the
    package that caused them.                                      -> [OBSERVED]
Also flags third-party modules the code imports but never declares. -> [VERIFIED]
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from depexit.discover import normalize_name, requirements_files, SKIP_DIRS
from depexit.envs import Env, copy_repo, create_env, pip_install, run_command, safe_rmtree
from depexit.log import log
from depexit.models import (
    OBSERVED, VERIFIED, Dependency, DiscoveryResult, RiskScanResult, RiskSignal, RunResult,
)
from depexit.testrun import TestRun, run_tests

FetchFn = Callable[[str], dict]
SITE_PACKAGES = re.compile(r"site-packages[\\/]+([A-Za-z0-9_]+)")
MISSING_MODULE = re.compile(r"(?:No module named|cannot import name .+? from) ['\"]([\w.]+)['\"]")


class PypiError(RuntimeError):
    pass


def fetch_pypi(name: str) -> dict:
    url = f"https://pypi.org/pypi/{name}/json"
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": "GemmaMorph"})
    except requests.RequestException as exc:
        raise PypiError(f"cannot reach {url}: {exc}") from exc
    if resp.status_code != 200:
        raise PypiError(f"{url} returned HTTP {resp.status_code}")
    return resp.json()


# ---------------------------------------------------------------- PyPI signals

def _pinned_version(dep: Dependency) -> str | None:
    for decl in dep.declared:
        for part in decl.spec.split(","):
            part = part.strip()
            if part.startswith("==") and "*" not in part:
                return part[2:].strip()
    return None


def _upload_time(files: list[dict]) -> datetime | None:
    times = []
    for f in files:
        raw = f.get("upload_time_iso_8601") or f.get("upload_time")
        if raw:
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                times.append(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
            except ValueError:
                continue
    return max(times) if times else None


def _all_yanked(files: list[dict]) -> bool:
    return bool(files) and all(f.get("yanked") for f in files)


def _requires_python_excludes(spec: str, running: Version) -> bool:
    try:
        return running not in SpecifierSet(spec)
    except InvalidSpecifier:
        return False


def metadata_signals(dep: Dependency, data: dict, stale_years: float, now: datetime) -> list[RiskSignal]:
    name = dep.name
    src = f"pypi:{name}"
    info, releases = data.get("info", {}), data.get("releases", {})
    latest = info.get("version", "")
    pinned = _pinned_version(dep)
    running = Version(f"{sys.version_info.major}.{sys.version_info.minor}")
    sigs: list[RiskSignal] = []

    def add(kind: str, detail: str) -> None:
        sigs.append(RiskSignal(kind=kind, detail=detail, source=src, tag=OBSERVED, dependency=name))

    released = _upload_time(releases.get(latest) or data.get("urls", []))
    if released:
        age = (now - released).days / 365.25
        if age > stale_years:
            add("stale-release", f"latest release {latest} was uploaded {released.date()} ({age:.1f} years ago)")
    if _all_yanked(releases.get(latest, [])):
        add("yanked", f"latest release {latest} is yanked on PyPI")
    if pinned and _all_yanked(releases.get(pinned, [])):
        add("yanked", f"pinned version {pinned} is yanked on PyPI")

    for c in info.get("classifiers", []):
        if c.startswith("Development Status") and any(w in c for w in ("Inactive", "Planning", "Pre-Alpha")):
            add("inactive", f"PyPI classifier: {c}")
    py3_minors = [int(m.group(1)) for c in info.get("classifiers", [])
                  if (m := re.fullmatch(r"Programming Language :: Python :: 3\.(\d+)", c))]
    if py3_minors and max(py3_minors) < running.minor:
        add("outdated-classifiers", f"classifiers list Python 3.{max(py3_minors)} at most; running 3.{running.minor}")

    rp_latest = info.get("requires_python") or ""
    if rp_latest and _requires_python_excludes(rp_latest, running):
        add("python-requires", f"latest {latest} requires Python {rp_latest}, excludes {running}")
    if pinned:
        pinned_files = releases.get(pinned) or []
        rp_pinned = next((f.get("requires_python") for f in pinned_files if f.get("requires_python")), "")
        if rp_pinned and _requires_python_excludes(rp_pinned, running):
            add("python-requires", f"pinned {pinned} requires Python {rp_pinned}, excludes {running}")
        try:
            if latest and Version(latest).major - Version(pinned).major >= 1:
                add("version-lag", f"pinned {pinned}; latest on PyPI is {latest} (major version behind)")
        except InvalidVersion:
            pass
    return sigs


# ------------------------------------------------------------ observed evidence

def _dists_for_module(top: str, module_map: dict[str, list[str]]) -> list[str]:
    top = top.removesuffix(".py")
    return module_map.get(top) or module_map.get(top.lower()) or []


def attribute_text(text: str, module_map: dict[str, list[str]], known: set[str],
                   last_frame: bool = False) -> str | None:
    """Map a traceback/warning text to a distribution via site-packages paths, then named modules."""
    frame_text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("E "))
    frames = SITE_PACKAGES.findall(frame_text)
    for top in (reversed(frames) if last_frame else frames):
        dists = _dists_for_module(top, module_map)
        if dists:
            return dists[0]
    m = MISSING_MODULE.search(text)
    if m:
        top = m.group(1).split(".")[0]
        dists = _dists_for_module(top, module_map)
        return dists[0] if dists else normalize_name(top)
    # last resort: a declared package's import name mentioned in the text
    lowered = text.lower()
    for mod, dists in sorted(module_map.items(), key=lambda kv: -len(kv[0])):
        if any(d in known for d in dists) and re.search(rf"\b{re.escape(mod.lower())}\b", lowered):
            return next(d for d in dists if d in known)
    return None


def observed_signals(run: TestRun, module_map: dict[str, list[str]], known: set[str],
                     redact: dict[str, str] | None = None) -> list[RiskSignal]:
    """redact maps sandbox path prefixes to labels, so temp paths do not leak into the report."""
    sigs: list[RiskSignal] = []
    seen: dict[tuple, int] = {}

    def clean(text: str) -> str:
        for prefix, label in (redact or {}).items():
            text = text.replace(prefix, label)
        return text

    def add(kind: str, detail: str, source: str, dep: str | None) -> None:
        detail, source = clean(detail), clean(source)
        key = (kind, dep, detail)
        if key in seen:
            return
        seen[key] = 1
        sigs.append(RiskSignal(kind=kind, detail=detail, source=source, tag=OBSERVED, dependency=dep))

    for location, text in run.warning_blocks:
        dep = attribute_text(f"{location} {text}", module_map, known)
        add("test-warning", text[:300], f"pytest:warnings:{location}", dep)
    for f in run.failures:
        body = f"{f.message}\n{f.text}"
        dep = attribute_text(body, module_map, known, last_frame=True)
        lines = f.text.strip().splitlines()
        summary = (f.message or (lines[-1] if lines else "failure"))[:300]
        add("test-failure", summary, f"pytest:{f.test}", dep)
    return sigs


# ------------------------------------------------------------ undeclared imports

def local_module_names(root: Path) -> set[str]:
    names: set[str] = set()
    for base in (root, root / "src"):
        if not base.is_dir():
            continue
        for item in base.iterdir():
            if item.name in SKIP_DIRS or item.name.startswith("."):
                continue
            if item.is_file() and item.suffix == ".py":
                names.add(item.stem)
            elif item.is_dir() and any(item.rglob("*.py")):
                names.add(item.name)
    return names


def third_party_imports(root: Path, notes: list[str]) -> dict[str, list[str]]:
    """top-level module -> repo files importing it (stdlib and local modules excluded)."""
    local = local_module_names(root)
    stdlib = set(sys.stdlib_module_names)
    found: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError) as exc:
            notes.append(f"{rel.as_posix()}: skipped, cannot parse: {exc}")
            continue
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            for mod in mods:
                if mod in stdlib or mod in local:
                    continue
                files = found.setdefault(mod, [])
                if rel.as_posix() not in files:
                    files.append(rel.as_posix())
    return found


def undeclared_signals(root: Path, declared: set[str], module_map: dict[str, list[str]],
                       notes: list[str]) -> list[RiskSignal]:
    sigs: list[RiskSignal] = []
    for mod, files in sorted(third_party_imports(root, notes).items()):
        dists = _dists_for_module(mod, module_map)
        if dists and any(d in declared for d in dists):
            continue
        dep = dists[0] if dists else normalize_name(mod)
        how = "" if dists else " (module not installed in the test venv, so its distribution is unknown)"
        sigs.append(RiskSignal(
            kind="undeclared-import",
            detail=f"imported as '{mod}' in {', '.join(files[:5])} but '{dep}' is not declared{how}",
            source=f"ast:{files[0]}", tag=VERIFIED, dependency=dep,
        ))
    return sigs


# ------------------------------------------------------------------- the stage

def _module_map(env: Env, cwd: Path) -> dict[str, list[str]]:
    code = "import json, importlib.metadata as m; print(json.dumps(m.packages_distributions()))"
    result, output = run_command("packages-distributions", [str(env.python), "-c", code], cwd, 120)
    if result.exit_code != 0:
        return {}
    try:
        raw = json.loads(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}
    return {mod: [normalize_name(d) for d in dists] for mod, dists in raw.items()}


def install_signals(runs: list[RunResult], outputs: list[str]) -> list[RiskSignal]:
    sigs = []
    for run, out in zip(runs, outputs):
        if run.exit_code == 0 or run.label == "create-venv":
            continue
        m = re.search(r"(?:requirement|building wheel for|Failed building wheel for)\s+([A-Za-z0-9_.\-]+)", out, re.I)
        dep = normalize_name(m.group(1)) if m else None
        last = next((l for l in reversed(out.splitlines()) if "error" in l.lower()), run.stdout_tail[-200:])
        sigs.append(RiskSignal(kind="install-failure", detail=f"{run.label} exited {run.exit_code}: {last.strip()[:300]}",
                               source=f"pip:{run.label}", tag=VERIFIED, dependency=dep))
    return sigs


def scan_risks(
    repo: Path,
    discovery: DiscoveryResult,
    stale_years: float = 2.0,
    test_timeout: int = 600,
    install_timeout: int = 900,
    fetch: FetchFn = fetch_pypi,
    keep_sandbox: bool = False,
) -> RiskScanResult:
    root = Path(repo).resolve()
    now = datetime.now(timezone.utc)
    known = {d.name for d in discovery.dependencies}
    signals: list[RiskSignal] = []
    notes: list[str] = []

    # --- PyPI metadata
    for dep in discovery.dependencies:
        try:
            data = fetch(dep.name)
        except PypiError as exc:
            signals.append(RiskSignal("pypi-unavailable", str(exc), f"pypi:{dep.name}", OBSERVED, dep.name))
            continue
        signals.extend(metadata_signals(dep, data, stale_years, now))

    # --- Observed evidence: fresh venv + the target's tests
    sandbox = Path(tempfile.mkdtemp(prefix="gemmamorph-s2-"))
    install_runs: list[RunResult] = []
    outputs: list[str] = []
    baseline: RunResult | None = None
    module_map: dict[str, list[str]] = {}
    try:
        work = copy_repo(root, sandbox / "repo")
        env, create_run = create_env(sandbox / "venv", timeout=install_timeout)
        install_runs.append(create_run)
        outputs.append(create_run.stdout_tail)
        if env is None:
            signals.append(RiskSignal("install-failure", f"could not create a virtual environment: {create_run.stdout_tail[-300:]}",
                                      "venv", VERIFIED))
            notes.append("observed test evidence unavailable: venv creation failed")
        else:
            req_files = requirements_files(work)
            if req_files:
                for rf in req_files:
                    run, out = pip_install(env, f"pip install -r {rf.name}", ["-r", str(rf)], work, install_timeout)
                    install_runs.append(run); outputs.append(out)
            else:
                specs = [f"{d.name}{d.declared[0].spec}" for d in discovery.dependencies]
                if specs:
                    run, out = pip_install(env, "pip install (pyproject dependencies)", specs, work, install_timeout)
                    install_runs.append(run); outputs.append(out)
            run, out = pip_install(env, "pip install pytest", ["pytest"], work, install_timeout)
            install_runs.append(run); outputs.append(out)

            module_map = _module_map(env, work)
            test_run = run_tests(env, work, "baseline", sandbox / "scratch", test_timeout)
            baseline = test_run.result
            log(f"  baseline tests: {baseline.passed} passed, {baseline.failed} failed, "
                f"{baseline.errors} errors (exit {baseline.exit_code})")
            if baseline.exit_code == 5 or not baseline.tests:
                notes.append("no tests were collected; there is no observed test evidence")
            signals.extend(observed_signals(test_run, module_map, known,
                                              {str(work) + os.sep: "", str(env.root): "<venv>"}))
        signals.extend(install_signals(install_runs, outputs))
        signals.extend(undeclared_signals(root, known, module_map, notes))
    finally:
        if keep_sandbox:
            notes.append(f"sandbox kept at {sandbox}")
        else:
            safe_rmtree(sandbox)

    flagged = sorted({s.dependency for s in signals if s.dependency and s.kind != "pypi-unavailable"})
    log(f"  {len(signals)} risk signals; {len(flagged)} flagged dependencies")
    return RiskScanResult(signals=signals, baseline=baseline, install_runs=install_runs,
                          module_map=module_map, flagged=flagged, notes=notes)


if __name__ == "__main__":
    from depexit.discover import discover

    target = Path(sys.argv[1])
    found = discover(target)
    result = scan_risks(target, found)
    print(f"\nSIGNALS ({len(result.signals)})")
    for s in result.signals:
        print(f"  [{s.tag}] {s.dependency or '-':16} {s.kind:20} {s.detail[:150]}  <{s.source[:60]}>")
    print(f"FLAGGED: {', '.join(result.flagged)}")
    for n in result.notes:
        print(f"NOTE: {n}")
    for r in result.install_runs:
        print(f"RUN {r.label:40} exit={r.exit_code}")
    if result.baseline:
        b = result.baseline
        print(f"BASELINE passed={b.passed} failed={b.failed} errors={b.errors} exit={b.exit_code}")
