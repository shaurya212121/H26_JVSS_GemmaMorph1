"""
Stage 5: Rehearse.
Verifies AI-proposed dependency replacements by applying edits and running tests in a sandbox.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from depexit.discover import discover, normalize_name
from depexit.edits import apply_edit, read_lf, write_lf, resolve_in_repo, AppliedEdit, EditError
from depexit.envs import copy_repo, create_env, pip_install, run_command, safe_rmtree, Env
from depexit.log import log
from depexit.models import (
    Comparison, DiscoveryResult, EditCoverage, Proposal,
    RehearsalResult, RunResult, StageStatus,
    AI_PROPOSED, REFUTED, VERIFIED
)
from depexit.testrun import run_tests

def apply_requirements_change(workdir: Path, change):
    """
    Apply a RequirementsChange to the manifest files in workdir.
    Normalizes package names before matching.
    """
    # 1. Find manifest files
    req_files = sorted(list(workdir.glob("requirements*.txt")))
    pyproject = workdir / "pyproject.toml"

    # Normalize remove list
    remove_norm = {normalize_name(n) for n in change.remove}
    add_norm = sorted(list(set(normalize_name(n) for n in change.add)))

    # 2. Handle requirements*.txt
    for rf in req_files:
        text, crlf = read_lf(rf)
        lines = text.splitlines()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "-r", "--requirement")):
                new_lines.append(line)
                continue

            try:
                from packaging.requirements import Requirement
                req = Requirement(stripped)
                if normalize_name(req.name) in remove_norm:
                    continue
            except:
                if any(normalize_name(r) in stripped.lower() for r in remove_norm):
                    continue

            new_lines.append(line)

        if rf == req_files[0]:
            for a in add_norm:
                new_lines.append(a)

        write_lf(rf, "\n".join(new_lines), crlf)

    # Note: pyproject.toml handling skipped for safety in this version,
    # focusing on requirements.txt.

def get_coverage_data(env: Env, coverage_file: Path, workdir: Path) -> dict:
    """Run coverage json and return the data."""
    cmd = [str(env.python), "-m", "coverage", "json", f"--data-file={coverage_file}", "-o", "coverage.json"]
    _, output = run_command("coverage-json", cmd, workdir, 600)

    cov_json_path = workdir / "coverage.json"
    if not cov_json_path.is_file():
        return {}

    try:
        return json.loads(cov_json_path.read_text(encoding="utf-8"))
    except:
        return {}

def rehearse(repo: Path, discovery: DiscoveryResult, baseline: Optional[RunResult],
             proposals: list[Proposal], output_dir: Path, keep_sandbox: bool = False,
             test_timeout: int = 600) -> RehearsalResult:

    log("Stage 5: Rehearsal starting")

    # 1. Candidates
    candidates = [p for p in proposals if p.error is None and (p.edits or p.requirements_change.remove or p.requirements_change.add)]

    comparisons = []
    gaps = []
    notes = []

    # Total list of runs to perform
    all_runs = []
    for p in candidates:
        all_runs.append(("individual", p))
    all_runs.append(("combined", candidates))

    for mode, target in all_runs:
        label = target.dependency if mode == "individual" else "combined"
        log(f"  Rehearsing {label}...")

        sandbox = Path(tempfile.mkdtemp(prefix="rehearse-"))
        workdir = copy_repo(repo, sandbox / "repo")

        try:
            applied_this_run = [] # list of (Proposal, AppliedEdit, file_path)

            if mode == "individual":
                p = target
                for edit in p.edits:
                    try:
                        f_path = resolve_in_repo(workdir, edit.file)
                        text, crlf = read_lf(f_path)
                        ae = apply_edit(text, edit.old_text, edit.new_text)
                        write_lf(f_path, ae.text, crlf)
                        applied_this_run.append((p, ae, edit.file))
                    except EditError as exc:
                        p.dropped_edits.append({"edit": edit, "reason": str(exc)})
                        gaps.append(f"[{label}] Edit to {edit.file} not applied: {exc}")

                apply_requirements_change(workdir, p.requirements_change)

            else: # combined
                for p in target:
                    for edit in p.edits:
                        try:
                            f_path = resolve_in_repo(workdir, edit.file)
                            text, crlf = read_lf(f_path)
                            ae = apply_edit(text, edit.old_text, edit.new_text)
                            write_lf(f_path, ae.text, crlf)
                            applied_this_run.append((p, ae, edit.file))
                        except EditError as exc:
                            gaps.append(f"[combined] Edit from {p.dependency} to {edit.file} not applied: {exc}")

                all_removes = set()
                all_adds = set()
                for p in target:
                    all_removes.update(p.requirements_change.remove)
                    all_adds.update(p.requirements_change.add)

                from depexit.models import RequirementsChange
                combined_rc = RequirementsChange(remove=list(all_removes), add=list(all_adds))
                apply_requirements_change(workdir, combined_rc)

            # Re-discover to know what to install
            _ = discover(workdir)

            # Create venv
            env_path = sandbox / ".venv"
            env, create_res = create_env(env_path, test_timeout)
            if env is None:
                res = RunResult(label=label, command=create_res.command, exit_code=create_res.exit_code,
                                stdout_tail=create_res.stdout_tail, tag=VERIFIED)
                comparisons.append(Comparison(label=label, run=res, install_ok=False))
                if mode == "individual":
                    p = target
                    p.verdict = "refuted"
                    p.tag = REFUTED
                    p.verdict_reasons.append(f"install failed: {res.stdout_tail}")
                continue

            # Install manifest files
            install_ok = True
            install_err = ""
            for req_f in sorted(list(workdir.glob("requirements*.txt"))):
                res, out = pip_install(env, f"install-{req_f.name}", [f"-r {req_f.relative_to(workdir)}"], workdir, test_timeout*1.5)
                if res.exit_code != 0:
                    install_ok = False
                    install_err = out.splitlines()[-1] if out else "Unknown error"
                    break

            if install_ok:
                res, out = pip_install(env, "install-tools", ["pytest", "coverage"], workdir, test_timeout)
                if res.exit_code != 0:
                    install_ok = False
                    install_err = out.splitlines()[-1] if out else "Unknown error"

            if not install_ok:
                res = RunResult(label=label, command=["pip", "install", "..."], exit_code=1,
                                stdout_tail=f"install failed: {install_err}", tag=VERIFIED)
                comparisons.append(Comparison(label=label, run=res, install_ok=False))
                if mode == "individual":
                    p = target
                    p.verdict = "refuted"
                    p.tag = REFUTED
                    p.verdict_reasons.append(f"install failed: {install_err}")
                continue

            # Run tests
            cov_file = sandbox / "coverage.data"
            test_run = run_tests(env, workdir, label, sandbox / "scratch", test_timeout, coverage_data=cov_file)
            run_res = test_run.result

            # Get coverage data
            cov_data = get_coverage_data(env, cov_file, workdir)

            # Calculate edit coverage
            edit_covs = []
            for p_orig, ae, rel_file in applied_this_run:
                abs_file = (workdir / rel_file).resolve()
                file_data = cov_data.get("files", {}).get(str(abs_file))
                executed = False
                reason = "file not imported or no executable lines"
                if file_data:
                    exec_lines = set(file_data.get("executed_lines", []))
                    miss_lines = set(file_data.get("missing_lines", []))
                    for l in range(ae.start_line, ae.end_line + 1):
                        if l in exec_lines or l in miss_lines:
                            if l in exec_lines:
                                executed = True
                                reason = "executed"
                                break
                            else:
                                reason = "missing"

                edit_covs.append(EditCoverage(proposal=p_orig.dependency, file=rel_file,
                                             start_line=ae.start_line, end_line=ae.end_line,
                                             executed=executed, reason=reason))

            # Compare with baseline
            new_failures = []
            newly_passing = []
            warnings_added = []
            warnings_removed = []

            if baseline:
                for nodeid, status in baseline.tests.items():
                    if status == "passed" and run_res.tests.get(nodeid) in ("failed", "error", None):
                        new_failures.append(nodeid)
                for nodeid, status in baseline.tests.items():
                    if status in ("failed", "error") and run_res.tests.get(nodeid) == "passed":
                        newly_passing.append(nodeid)
                baseline_warns = set(baseline.warnings)
                current_warns = set(run_res.warnings)
                warnings_added = list(current_warns - baseline_warns)
                warnings_removed = list(baseline_warns - current_warns)

            comp = Comparison(
                label=label,
                run=run_res,
                new_failures=new_failures,
                newly_passing=newly_passing,
                warnings_added=warnings_added,
                warnings_removed=warnings_removed,
                edit_coverage=edit_covs,
                install_ok=True
            )
            comparisons.append(comp)

            if mode == "individual":
                p = target
                if new_failures:
                    p.verdict = "refuted"
                    p.tag = REFUTED
                    p.verdict_reasons.extend([f"new failure: {n}" for n in new_failures])
                elif baseline is None or not baseline.tests:
                    p.verdict = "unproven"
                    p.verdict_reasons.append("no baseline tests to compare against")
                elif (p.edits and not edit_covs) or any(not ec.executed for ec in edit_covs):
                    p.verdict = "unproven"
                    reason = f"edit not executed: {edit_covs[0].file if edit_covs else 'no edits applied'}"
                    p.verdict_reasons.append(reason)
                elif not p.edits and not (p.requirements_change.remove or p.requirements_change.add):
                    p.verdict = "unproven"
                    p.verdict_reasons.append("nothing to rehearse")
                else:
                    p.verdict = "verified"

            if mode == "combined" and new_failures:
                gaps.append(f"[combined] introduced new failures: {', '.join(new_failures)}")

        finally:
            if not keep_sandbox:
                safe_rmtree(sandbox)
            else:
                notes.append(f"Sandbox kept at {sandbox}")

    # Patch generation
    patch_path = None
    if combined_candidates := candidates:
        patch_sandbox = Path(tempfile.mkdtemp(prefix="rehearse-patch-"))
        try:
            combined_workdir = copy_repo(repo, patch_sandbox / "repo")
            for p in combined_candidates:
                for edit in p.edits:
                    try:
                        f_path = resolve_in_repo(combined_workdir, edit.file)
                        text, crlf = read_lf(f_path)
                        ae = apply_edit(text, edit.old_text, edit.new_text)
                        write_lf(f_path, ae.text, crlf)
                    except EditError:
                        pass

            all_removes = set()
            all_adds = set()
            for p in combined_candidates:
                all_removes.update(p.requirements_change.remove)
                all_adds.update(p.requirements_change.add)

            from depexit.models import RequirementsChange
            rc = RequirementsChange(remove=list(all_removes), add=list(all_adds))
            apply_requirements_change(combined_workdir, rc)

            all_files = sorted(list(combined_workdir.rglob("*")))
            diff_lines = []
            for f in all_files:
                if f.is_file():
                    rel_f = f.relative_to(combined_workdir).as_posix()
                    original_f = repo / rel_f
                    if not original_f.is_file():
                        with open(f, "rb") as src:
                            content = src.read().decode("utf-8", "replace").splitlines(keepends=True)
                        diff_lines.extend(difflib.unified_diff([], content, f"a/{rel_f}", f"b/{rel_f}"))
                    else:
                        with open(original_f, "rb") as src1, open(f, "rb") as src2:
                            c1 = src1.read().decode("utf-8", "replace").splitlines(keepends=True)
                            c2 = src2.read().decode("utf-8", "replace").splitlines(keepends=True)
                            diff_lines.extend(difflib.unified_diff(c1, c2, f"a/{rel_f}", f"b/{rel_f}"))

            if diff_lines:
                patch_path = output_dir / "proposed.patch"
                patch_path.write_text("".join(diff_lines), encoding="utf-8")
                patch_path = str(patch_path)
        finally:
            safe_rmtree(patch_sandbox)

    return RehearsalResult(
        baseline=baseline,
        comparisons=comparisons,
        gaps=gaps,
        patch_path=patch_path,
        notes=notes,
        status=StageStatus("rehearse")
    )
