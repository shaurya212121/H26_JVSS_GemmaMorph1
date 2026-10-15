"""
Stage 5: Rehearse.

Each Gemma proposal is tried in a throwaway copy of the repo with a fresh
virtual environment: baseline (from S2), each proposal alone, and all proposals
combined. The target's tests are run under coverage.py, so an edit that no test
executes is "unproven", not "verified". The combined candidate is written to
proposed.patch.

A run that cannot be set up for reasons outside the proposal (no venv) is a
stage failure; a proposal that cannot be installed or breaks a test is REFUTED.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from packaging.requirements import InvalidRequirement, Requirement

from depexit.discover import discover, normalize_name, requirements_files
from depexit.edits import AppliedEdit, EditError, apply_edit, read_lf, resolve_in_repo, write_lf
from depexit.envs import Env, copy_repo, create_env, pip_install, run_command, safe_rmtree
from depexit.log import log
from depexit.models import (
    FAILED, REFUTED, VERIFIED, Comparison, DiscoveryResult, Edit, EditCoverage, Proposal,
    RehearsalResult, RequirementsChange, RunResult, StageStatus,
)
from depexit.testrun import run_tests

PYPROJECT_LINE = re.compile(r"""^\s*(["'])(?P<req>.+?)\1\s*,?\s*(#.*)?$""")


# ------------------------------------------------------------- requirements

def _req_name(entry: str) -> Optional[str]:
    try:
        return normalize_name(Requirement(entry.strip()).name)
    except InvalidRequirement:
        return None


@dataclass
class RequirementsApplied:
    unmatched_removes: list[str] = field(default_factory=list)  # nothing declared that name
    unwritten_adds: list[str] = field(default_factory=list)  # no requirements file to put them in


def apply_requirements_change(workdir: Path, change: RequirementsChange) -> RequirementsApplied:
    """Remove and add requirements in the manifests of workdir. Removal matches on the parsed package name."""
    remove_names = {n for n in (_req_name(r) or normalize_name(r) for r in change.remove)}
    matched: set[str] = set()
    req_files = requirements_files(workdir)

    for path in req_files:
        text, crlf = read_lf(path)
        kept: list[str] = []
        for line in text.splitlines():
            candidate = re.sub(r"(^|\s)#.*$", "", line).strip()
            name = _req_name(candidate) if candidate and not candidate.startswith("-") else None
            if name in remove_names:
                matched.add(name)
                continue
            kept.append(line)
        if path == req_files[0]:
            present = {_req_name(re.sub(r"(^|\s)#.*$", "", l).strip()) for l in kept}
            kept.extend(a.strip() for a in change.add if a.strip() and _req_name(a) not in present)
        write_lf(path, "\n".join(kept) + "\n", crlf)

    pyproject = workdir / "pyproject.toml"
    if pyproject.is_file():
        text, crlf = read_lf(pyproject)
        kept = []
        for line in text.splitlines():
            m = PYPROJECT_LINE.match(line)
            name = _req_name(m.group("req")) if m else None
            if name in remove_names:
                matched.add(name)
                continue
            kept.append(line)
        write_lf(pyproject, "\n".join(kept) + "\n", crlf)

    return RequirementsApplied(
        unmatched_removes=sorted(remove_names - matched),
        unwritten_adds=[a.strip() for a in change.add if a.strip()] if not req_files else [],
    )


# ------------------------------------------------------------------- edits

def apply_proposals(workdir: Path, proposals: list[Proposal], label: str, gaps: list[str],
                    record_drops: bool) -> list[tuple[Proposal, Edit, AppliedEdit]]:
    """Apply every edit that still applies; an edit that does not is a gap (and a dropped edit)."""
    applied: list[tuple[Proposal, Edit, AppliedEdit]] = []
    for p in proposals:
        for edit in p.edits:
            try:
                path = resolve_in_repo(workdir, edit.file)
                text, crlf = read_lf(path)
                result = apply_edit(text, edit.old_text, edit.new_text)
                write_lf(path, result.text, crlf)
                applied.append((p, edit, result))
            except EditError as exc:
                gaps.append(f"[{label}] edit from {p.dependency} to {edit.file} not applied: {exc}")
                if record_drops:
                    p.dropped_edits.append({"edit": asdict(edit), "reason": str(exc)})
    return applied


def edit_executed(cov: dict, workdir: Path, rel_file: str, start: int, end: int, deletion: bool = False) -> tuple[bool, str]:
    """Did the tests execute any statement in lines start..end of rel_file? cov is coverage.py's JSON."""
    if deletion:
        return False, "a deletion has no executable lines to run"
    target = os.path.normcase(str((workdir / rel_file).resolve()))
    entry = None
    for key, data in cov.get("files", {}).items():
        p = Path(key)
        p = p if p.is_absolute() else workdir / p
        if os.path.normcase(str(p.resolve())) == target:
            entry = data
            break
    if entry is None:
        return False, "the file was never imported by the tests"
    executed, missing = set(entry.get("executed_lines", [])), set(entry.get("missing_lines", []))
    lines = range(start, end + 1)
    if any(n in executed for n in lines):
        return True, "executed"
    if any(n in missing for n in lines):
        return False, "the edited lines were never executed by the tests"
    return False, "the edited lines contain no executable statements"


def get_coverage_data(env: Env, coverage_file: Path, workdir: Path) -> dict:
    """Run `coverage json` and return its data ({} if coverage produced nothing)."""
    out = workdir / "coverage.json"
    run_command("coverage-json", [str(env.python), "-m", "coverage", "json", f"--data-file={coverage_file}",
                                  "-o", str(out)], workdir, 600)
    if not out.is_file():
        return {}
    try:
        return json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- comparison

def compare_runs(baseline: Optional[RunResult], run: RunResult) -> tuple[list[str], list[str], list[str], list[str]]:
    """(new_failures, newly_passing, warnings_added, warnings_removed) of run against baseline."""
    if baseline is None:
        return [], [], [], []
    new_failures = [t for t, s in baseline.tests.items() if s == "passed" and run.tests.get(t) in ("failed", "error", None)]
    newly_passing = [t for t, s in baseline.tests.items() if s in ("failed", "error") and run.tests.get(t) == "passed"]
    before, after = set(baseline.warnings), set(run.warnings)
    return new_failures, newly_passing, sorted(after - before), sorted(before - after)


def _error_line(output: str) -> str:
    """The last pytest 'E ' line of the output (the real error), or the last non-empty line."""
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    errors = [l for l in lines if l.startswith("E ")]
    return (errors or lines or ["no output"])[-1][:300]


def is_manifest(rel_file: str) -> bool:
    name = Path(rel_file).name
    return name == "pyproject.toml" or (name.startswith("requirements") and name.endswith(".txt"))


def decide_verdict(p: Proposal, comp: Comparison, baseline: Optional[RunResult], applied_edits: int) -> None:
    """Set the proposal's verdict from a rehearsal of that proposal alone."""
    if not comp.install_ok:
        p.verdict, p.tag = "refuted", REFUTED
        p.verdict_reasons.append(f"install failed: {comp.run.stdout_tail.strip()[-300:] if comp.run else 'unknown error'}")
    elif baseline is not None and baseline.tests and comp.run is not None and not comp.run.tests:
        p.verdict, p.tag = "refuted", REFUTED
        p.verdict_reasons.append(f"the test suite could not run (pytest exit {comp.run.exit_code}): {_error_line(comp.run.stdout_tail)}")
    elif comp.new_failures:
        p.verdict, p.tag = "refuted", REFUTED
        p.verdict_reasons.extend(f"new test failure: {n}" for n in comp.new_failures)
    elif baseline is None or not baseline.tests:
        p.verdict = "unproven"
        p.verdict_reasons.append("the baseline has no tests to compare against")
    elif p.edits and applied_edits == 0:
        p.verdict = "unproven"
        p.verdict_reasons.append("none of the proposed edits could be applied")
    elif any(not ec.executed for ec in comp.edit_coverage):
        p.verdict = "unproven"
        p.verdict_reasons.extend(f"edit not executed: {ec.file}:{ec.start_line} ({ec.reason})"
                                 for ec in comp.edit_coverage if not ec.executed)
    elif not p.edits and not (p.requirements_change.remove or p.requirements_change.add):
        p.verdict = "unproven"
        p.verdict_reasons.append("nothing to rehearse")
    else:
        p.verdict = "verified"
        parts = ["no test that passed in the baseline fails now"]
        if comp.newly_passing:
            parts.append(f"{len(comp.newly_passing)} test(s) that failed in the baseline now pass: {', '.join(comp.newly_passing[:3])}")
        if comp.edit_coverage:
            parts.append(f"{len(comp.edit_coverage)} edit(s) exercised by the tests")
        p.verdict_reasons.append("; ".join(parts))


# --------------------------------------------------------------------- patch

def make_patch(repo: Path, proposals: list[Proposal], output_dir: Path) -> Optional[str]:
    """Write output_dir/proposed.patch: the combined candidate against the original. None if nothing changes."""
    sandbox = Path(tempfile.mkdtemp(prefix="rehearse-patch-"))
    try:
        work = copy_repo(repo, sandbox / "repo")
        applied = apply_proposals(work, proposals, "patch", [], record_drops=False)
        change = RequirementsChange(
            remove=[r for p in proposals for r in p.requirements_change.remove],
            add=[a for p in proposals for a in p.requirements_change.add],
        )
        apply_requirements_change(work, change)
        touched = {e.file for _, e, _ in applied} | {p.relative_to(work).as_posix() for p in requirements_files(work)}
        if (work / "pyproject.toml").is_file():
            touched.add("pyproject.toml")
        diff: list[str] = []
        for rel in sorted(touched):
            before = (repo / rel).read_bytes().decode("utf-8", "replace").replace("\r\n", "\n").splitlines(keepends=True) \
                if (repo / rel).is_file() else []
            after_path = work / rel
            after = after_path.read_bytes().decode("utf-8", "replace").replace("\r\n", "\n").splitlines(keepends=True) \
                if after_path.is_file() else []
            diff.extend(difflib.unified_diff(before, after, f"a/{rel}", f"b/{rel}"))
        if not diff:
            return None
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "proposed.patch"
        path.write_text("".join(diff), encoding="utf-8", newline="\n")
        return str(path)
    finally:
        safe_rmtree(sandbox)


# ------------------------------------------------------------------ one run

def _install(env: Env, workdir: Path, extra: list[str], timeout: int) -> Optional[str]:
    """Install the manifests, extra requirements and test tools. Returns an error line, or None on success."""
    files = requirements_files(workdir)
    steps: list[tuple[str, list[str]]] = [(f"install-{f.name}", ["-r", str(f)]) for f in files]
    if not files:
        specs = [f"{d.name}{d.declared[0].spec}" for d in discover(workdir).dependencies]
        if specs:
            steps.append(("install-pyproject-dependencies", specs))
    if extra:
        steps.append(("install-added-requirements", extra))
    steps.append(("install-test-tools", ["pytest", "coverage"]))
    for label, args in steps:
        res, out = pip_install(env, label, args, workdir, timeout)
        if res.exit_code != 0:
            lines = [l for l in (out or res.stdout_tail).splitlines() if l.strip()]
            return f"{label}: {lines[-1] if lines else 'pip failed'}"
    return None


def _rehearse_run(label: str, repo: Path, proposals: list[Proposal], baseline: Optional[RunResult],
                  gaps: list[str], notes: list[str], keep_sandbox: bool, timeout: int,
                  record_drops: bool) -> tuple[Optional[Comparison], int, Optional[str]]:
    """Returns (comparison or None if no venv, number of applied edits, infrastructure error)."""
    log(f"  rehearsing {label}")
    sandbox = Path(tempfile.mkdtemp(prefix="rehearse-"))
    try:
        work = copy_repo(repo, sandbox / "repo")
        applied = apply_proposals(work, proposals, label, gaps, record_drops)
        change = RequirementsChange(
            remove=[r for p in proposals for r in p.requirements_change.remove],
            add=[a for p in proposals for a in p.requirements_change.add],
        )
        req = apply_requirements_change(work, change)
        for name in req.unmatched_removes:
            gaps.append(f"[{label}] requirement '{name}' to remove was not declared in any manifest")

        env, create_run = create_env(sandbox / ".venv", timeout)
        if env is None:
            return None, len(applied), f"could not create a virtual environment for '{label}': {create_run.stdout_tail[-200:]}"

        error = _install(env, work, req.unwritten_adds, int(timeout * 1.5))
        if error:
            run = RunResult(label=label, command=["pip", "install"], exit_code=1, stdout_tail=error, tag=VERIFIED)
            return Comparison(label=label, run=run, install_ok=False), len(applied), None

        cov_file = sandbox / "coverage.data"
        test_run = run_tests(env, work, label, sandbox / "scratch", timeout, coverage_data=cov_file)
        cov = get_coverage_data(env, cov_file, work)
        coverage = []
        for p, edit, ae in applied:
            if is_manifest(edit.file):  # coverage cannot measure a manifest: the install and the whole test run exercise it
                ok, reason = True, "manifest change: installed, and the whole test suite ran against it"
            else:
                ok, reason = edit_executed(cov, work, edit.file, ae.start_line, ae.end_line, deletion=not edit.new_text.strip())
            coverage.append(EditCoverage(p.dependency, edit.file, ae.start_line, ae.end_line, ok, reason))
        failures, passing, added, removed = compare_runs(baseline, test_run.result)
        return Comparison(label=label, run=test_run.result, new_failures=failures, newly_passing=passing,
                          warnings_added=added, warnings_removed=removed, edit_coverage=coverage,
                          install_ok=True), len(applied), None
    finally:
        if keep_sandbox:
            notes.append(f"sandbox for '{label}' kept at {sandbox}")
        else:
            safe_rmtree(sandbox)


def rehearse(repo: Path, discovery: DiscoveryResult, baseline: Optional[RunResult],
             proposals: list[Proposal], output_dir: Path, keep_sandbox: bool = False,
             test_timeout: int = 600) -> RehearsalResult:
    repo = Path(repo).resolve()
    output_dir = Path(output_dir)
    candidates = [p for p in proposals if p.error is None
                  and (p.edits or p.requirements_change.remove or p.requirements_change.add)]
    comparisons: list[Comparison] = []
    gaps: list[str] = []
    notes: list[str] = []
    infra_errors: list[str] = []

    if not candidates:
        notes.append("no proposal has edits or a requirements change to rehearse")
    if baseline is None or not baseline.tests:
        notes.append("the baseline has no tests: every proposal stays unproven")

    for p in candidates:
        comp, applied, infra = _rehearse_run(p.dependency, repo, [p], baseline, gaps, notes, keep_sandbox,
                                             test_timeout, record_drops=True)
        if infra:
            infra_errors.append(infra)
            p.verdict = "unproven"
            p.verdict_reasons.append("the sandbox could not be created")
            continue
        comparisons.append(comp)
        decide_verdict(p, comp, baseline, applied)

    survivors = [p for p in candidates if p.verdict != "refuted"]
    if len(survivors) != len(candidates):
        notes.append("refuted proposals are left out of the combined run and of proposed.patch: "
                     + ", ".join(p.dependency for p in candidates if p.verdict == "refuted"))
    if len(survivors) > 1:
        comp, _, infra = _rehearse_run("combined", repo, survivors, baseline, gaps, notes, keep_sandbox,
                                       test_timeout, record_drops=False)
        if infra:
            infra_errors.append(infra)
        else:
            comparisons.append(comp)
            if comp.new_failures:
                gaps.append("[combined] new failures when all proposals are applied together: " + ", ".join(comp.new_failures))
            if not comp.install_ok:
                gaps.append(f"[combined] install failed: {comp.run.stdout_tail if comp.run else ''}")
    elif candidates:
        notes.append("a combined run was skipped: fewer than two proposals survived their own rehearsal")

    for comp in comparisons:
        for ec in comp.edit_coverage:
            if not ec.executed:
                gaps.append(f"[{comp.label}] edit in {ec.file}:{ec.start_line} was not executed by the tests ({ec.reason})")
        if comp.warnings_added:
            gaps.append(f"[{comp.label}] {len(comp.warnings_added)} new warning(s) compared with the baseline")

    patch_path = make_patch(repo, survivors, output_dir) if survivors else None
    status = StageStatus("rehearse")
    if infra_errors:
        status = StageStatus("rehearse", FAILED, error="; ".join(infra_errors))
    log(f"  {len(comparisons)} runs compared, {len(gaps)} gaps")
    return RehearsalResult(baseline=baseline, comparisons=comparisons, gaps=gaps, patch_path=patch_path,
                           notes=notes, status=status)
