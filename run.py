#!/usr/bin/env python3
"""
GemmaMorph orchestrator: runs the six stages in order, passing each stage's
typed output (depexit/models.py) to the next.

  S1 discover  -> S2 risk_scan -> S3 usage -> S4 propose (Gemma 4)
              -> S5 rehearse   -> S6 capsule

Every stage ends as ok, failed or skipped, with the real error or the reason.
A stage whose input is missing is skipped and says which stage it waited for.
The exit code is 1 if any stage failed, 2 for bad arguments, otherwise 0.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from depexit.llm import LLMClient, LLMConfig  # noqa: E402
from depexit.log import log  # noqa: E402
from depexit.models import (  # noqa: E402
    FAILED, OK, SKIPPED, Capsule, DiscoveryResult, ProposalResult, RehearsalResult,
    RiskScanResult, StageStatus, UsageResult,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

STAGES = ("S1 discover", "S2 risk_scan", "S3 usage", "S4 propose", "S5 rehearse", "S6 capsule")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="run.py", description="Find at-risk dependencies and rehearse their replacement with Gemma 4.")
    p.add_argument("repo_pos", nargs="?", metavar="REPO", help="path to the Python repo to analyse")
    p.add_argument("--repo", dest="repo_flag", help="same as the positional REPO")
    p.add_argument("--output", default="./output", help="directory for the capsule files (default ./output)")
    p.add_argument("--backend", choices=["ollama", "gemini"], help="model backend (default ollama; env GEMMA_BACKEND)")
    p.add_argument("--hosted", action="store_true", help="shortcut for --backend gemini (needs GEMINI_API_KEY)")
    p.add_argument("--model", help="Gemma 4 model name (default gemma4:e2b / gemma-4-26b-a4b-it; env GEMMA_MODEL)")
    p.add_argument("--base-url", help="backend base URL (env GEMMA_BASE_URL or OLLAMA_HOST)")
    p.add_argument("--num-ctx", type=int, help="Ollama context window in tokens (default 16384)")
    p.add_argument("--skip-rehearsal", action="store_true", help="skip S5 (no sandbox runs)")
    p.add_argument("--keep-sandbox", action="store_true", help="keep temporary sandboxes for inspection")
    p.add_argument("--stale-years", type=float, default=2.0, help="flag a latest release older than this (default 2)")
    p.add_argument("--test-timeout", type=int, default=600, help="seconds allowed per test run (default 600)")
    return p.parse_args(argv)


@dataclass
class Stage:
    name: str
    status: StageStatus
    value: Any = None


def execute(name: str, fn: Callable[[], Any]) -> Stage:
    """Run one stage. This is the only place exceptions are caught; the real error is recorded."""
    log(f"--- {name}")
    started = time.monotonic()
    try:
        value = fn()
    except Exception as exc:  # a stage failing must not stop the report from being written
        return Stage(name, StageStatus(name, FAILED, error=f"{type(exc).__name__}: {exc}"))
    status = getattr(value, "status", None)
    if isinstance(status, StageStatus):  # the stage may itself report a partial failure
        status.stage = name
    else:
        status = StageStatus(name, OK)
    log(f"    ({time.monotonic() - started:.1f}s)")
    return Stage(name, status, value)


def skipped(name: str, reason: str) -> Stage:
    return Stage(name, StageStatus(name, SKIPPED, skip_reason=reason))


def waiting_on(*deps: Stage) -> Optional[str]:
    for d in deps:
        if d.status.status != OK or d.value is None:
            return f"{d.name} did not complete ({d.status.status})"
    return None


def describe(stage: Stage) -> str:
    s = stage.status
    if s.status == SKIPPED:
        return f"skipped: {s.skip_reason}"
    v = stage.value
    if s.status == FAILED:
        return f"{s.error}"
    if isinstance(v, DiscoveryResult):
        return f"{len(v.dependencies)} dependencies, {len(v.conflicts)} conflicts"
    if isinstance(v, RiskScanResult):
        b = v.baseline
        tests = f"baseline {b.passed} passed/{b.failed} failed" if b else "no baseline run"
        return f"{len(v.signals)} signals, {len(v.flagged)} flagged dependencies, {tests}"
    if isinstance(v, UsageResult):
        return f"{sum(len(u) for u in v.usages.values())} usages across {len(v.usages)} dependencies"
    if isinstance(v, ProposalResult):
        return f"{sum(1 for p in v.proposals if not p.error)} proposals, {sum(1 for p in v.proposals if p.error)} without"
    if isinstance(v, RehearsalResult):
        return f"{len(v.comparisons)} runs compared, {len(v.gaps)} gaps"
    return "done"


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    repo_arg = args.repo_pos or args.repo_flag
    if not repo_arg or (args.repo_pos and args.repo_flag and args.repo_pos != args.repo_flag):
        print("error: give the repo once, as REPO or --repo", file=sys.stderr)
        return 2
    repo = Path(repo_arg).resolve()
    if not repo.is_dir():
        print(f"error: repo not found: {repo}", file=sys.stderr)
        return 2
    output_dir = Path(args.output).resolve()
    try:
        config = LLMConfig.from_options(backend="gemini" if args.hosted else args.backend, model=args.model,
                                        base_url=args.base_url, num_ctx=args.num_ctx)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    llm = LLMClient(config)
    log(f"GemmaMorph: repo={repo}  model={config.model} ({config.backend} @ {config.base_url})")

    def run_discover() -> DiscoveryResult:
        from depexit.discover import discover
        return discover(repo)

    s1 = execute(STAGES[0], run_discover)

    def run_risk() -> RiskScanResult:
        from depexit.risk_scan import scan_risks
        return scan_risks(repo, s1.value, stale_years=args.stale_years, test_timeout=args.test_timeout,
                          keep_sandbox=args.keep_sandbox)

    s2 = skipped(STAGES[1], waiting_on(s1)) if waiting_on(s1) else execute(STAGES[1], run_risk)

    def run_usage() -> UsageResult:
        from depexit.usage_finder import find_usages
        return find_usages(repo, s2.value.flagged, s2.value.module_map)

    s3 = skipped(STAGES[2], waiting_on(s2)) if waiting_on(s2) else execute(STAGES[2], run_usage)

    def run_propose() -> ProposalResult:
        from depexit.gemma_propose import propose_migrations
        return propose_migrations(llm, repo, s1.value, s2.value, s3.value)

    s4 = skipped(STAGES[3], waiting_on(s3)) if waiting_on(s3) else execute(STAGES[3], run_propose)

    def run_rehearse() -> RehearsalResult:
        from depexit.rehearsal import rehearse
        return rehearse(repo, s1.value, s2.value.baseline, s4.value.proposals, output_dir,
                        keep_sandbox=args.keep_sandbox, test_timeout=args.test_timeout)

    if args.skip_rehearsal:
        s5 = skipped(STAGES[4], "--skip-rehearsal was given")
    elif waiting_on(s4):
        s5 = skipped(STAGES[4], waiting_on(s4))
    else:
        s5 = execute(STAGES[4], run_rehearse)

    stages = [s1, s2, s3, s4, s5]
    paths: list[Path] = []

    def run_capsule() -> list[Path]:
        from depexit.capsule import build_summary, write_capsule
        d, r, u, p, h = (s.value for s in (s1, s2, s3, s4, s5))
        capsule = Capsule(
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            repo=str(repo),
            model={"name": config.model, "backend": config.backend, "base_url": config.base_url},
            stages=[s.status for s in stages],
            dependencies=d.dependencies if d else [],
            conflicts=d.conflicts if d else [],
            signals=r.signals if r else [],
            usages=u.usages if u else {},
            proposals=p.proposals if p else [],
            baseline=r.baseline if r else None,
            comparisons=h.comparisons if h else [],
            gaps=h.gaps if h else [],
            patch_path=h.patch_path if h else None,
            summary=None,
            notes=[n for res in (d, r, u, h) if res for n in res.notes],
        )
        capsule.summary, capsule.summary_error = build_summary(llm, capsule)
        return list(write_capsule(capsule, output_dir))

    s6 = execute(STAGES[5], run_capsule)
    if s6.status.status == OK:
        paths = s6.value
    stages.append(s6)

    print()
    for s in stages:
        print(f"[{s.status.status:7}] {s.name:13} {describe(s)}")
    for path in paths:
        print(f"output: {path}")
    failed = [s.name for s in stages if s.status.status == FAILED]
    code = 1 if failed else 0
    print(f"exit code {code}" + (f" (failed: {', '.join(failed)})" if failed else ""))
    return code


if __name__ == "__main__":
    sys.exit(main())
