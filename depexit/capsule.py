"""
Stage 6: Dependency Exit Capsule Generator.

Generates machine-readable EXIT_CAPSULE.json, human-readable EXIT_CAPSULE.md,
and self-contained EXIT_CAPSULE.html from the shared typed Capsule contract.

Enforces strict claim verification tagging:
  - [VERIFIED]    : Confirmed by code execution or deterministic AST analysis
  - [OBSERVED]    : Observed in test execution output or PyPI metadata
  - [AI-PROPOSED] : Synthesized by Gemma AI
  - [REFUTED]     : Proposed by Gemma AI but failed during rehearsal
  - [AI-GENERATED]: Synthesized text (e.g. executive summary)
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Optional, Tuple

from depexit.llm import extract_json_object
from depexit.log import log
from depexit.models import (
    AI_GENERATED,
    AI_PROPOSED,
    FAILED,
    OBSERVED,
    OK,
    REFUTED,
    SKIPPED,
    TAGS,
    VERIFIED,
    Capsule,
    Comparison,
    Conflict,
    Declaration,
    Dependency,
    Edit,
    EditCoverage,
    Proposal,
    RequirementsChange,
    RiskSignal,
    RunResult,
    StageStatus,
    Usage,
)


def build_summary(llm: Any, capsule: Capsule) -> tuple[Optional[str], Optional[str]]:
    """
    Query Gemma for a concise, factual executive summary of the Capsule.

    Parameters:
        llm: An object with an `ask(prompt: str, system: str = "") -> str` method.
        capsule: The completed Capsule dataclass instance.

    Returns:
        tuple[Optional[str], Optional[str]]: (summary_text, error_message).
        On error, returns (None, "<real error text>"). Never invents a summary.
    """
    total_deps = len(capsule.dependencies)
    conflict_count = len(capsule.conflicts)
    signal_count = len(capsule.signals)
    flagged_deps = sorted(list(capsule.usages.keys()))
    proposal_count = len(capsule.proposals)

    verdicts_summary = [f"{p.dependency}: {p.verdict}" for p in capsule.proposals]
    refuted_proposals = [p.dependency for p in capsule.proposals if p.verdict == "refuted"]

    prompt = (
        "Generate a factual, 3-6 sentence plain English executive summary of the following dependency resilience audit.\n\n"
        f"Repository: {capsule.repo}\n"
        f"Dependencies Evaluated: {total_deps}\n"
        f"Manifest Conflicts: {conflict_count}\n"
        f"Risk Signals Detected: {signal_count}\n"
        f"Flagged At-Risk Dependencies: {', '.join(flagged_deps) if flagged_deps else 'none'}\n"
        f"Migration Proposals Generated: {proposal_count}\n"
        f"Proposal Verdicts: {', '.join(verdicts_summary) if verdicts_summary else 'none'}\n"
        f"Refuted Proposals: {', '.join(refuted_proposals) if refuted_proposals else 'none'}\n"
        f"Compatibility Gaps: {', '.join(capsule.gaps) if capsule.gaps else 'none'}\n\n"
        "Requirements:\n"
        "- Respond ONLY with a valid JSON object: {\"summary\": \"<3-6 sentences>\"}\n"
        "- State purely factual findings based strictly on the provided data.\n"
        "- Do not extrapolate, invent numbers, or present unproven claims as verified.\n"
    )

    system_prompt = "You are an autonomous software dependency safety auditor. Respond strictly in valid JSON."

    try:
        raw_response = llm.ask(prompt, system=system_prompt)
    except Exception as exc:
        log(f"Stage 6: LLM summary query failed: {exc}")
        return None, str(exc)

    try:
        data = extract_json_object(raw_response)
        if not isinstance(data, dict):
            return None, f"Expected JSON object, got {type(data).__name__}"
        if "summary" not in data or not isinstance(data["summary"], str):
            return None, "JSON missing required 'summary' string key"
        summary_text = data["summary"].strip()
        if not summary_text:
            return None, "Model returned empty summary"
        return summary_text, None
    except Exception as exc:
        log(f"Stage 6: Failed to extract summary JSON: {exc}")
        return None, str(exc)


def write_capsule(capsule: Capsule, output_dir: Path) -> tuple[Path, Path, Path]:
    """
    Write EXIT_CAPSULE.json, EXIT_CAPSULE.md, and EXIT_CAPSULE.html to output_dir.

    Parameters:
        capsule: The completed Capsule dataclass instance.
        output_dir: Directory where the capsule files will be saved.

    Returns:
        tuple[Path, Path, Path]: (json_path, md_path, html_path).
    """
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    log(f"Stage 6: Writing Dependency Exit Capsule to {output_dir}")

    # 1. JSON (Machine-readable)
    json_path = output_dir / "EXIT_CAPSULE.json"
    json_content = json.dumps(capsule.to_dict(), indent=2)
    json_path.write_text(json_content, encoding="utf-8")

    # 2. Markdown (Human-readable GitHub flavored)
    md_path = output_dir / "EXIT_CAPSULE.md"
    md_content = _render_markdown(capsule)
    md_path.write_text(md_content, encoding="utf-8")

    # 3. HTML (Self-contained, offline-compatible)
    html_path = output_dir / "EXIT_CAPSULE.html"
    html_content = _render_html(capsule)
    html_path.write_text(html_content, encoding="utf-8")

    log(f"Stage 6 complete: Capsule written to {output_dir}")
    return json_path, md_path, html_path


# ---------------------------------------------------------------- Markdown Renderer


def _render_markdown(capsule: Capsule) -> str:
    lines: list[str] = []

    lines.append("# 🛡️ Dependency Exit Capsule")
    lines.append("")
    lines.append("> **GemmaMorph: Autonomous Dependency Resilience & Exit Rehearsal**")
    lines.append("")

    # Tag Legend
    lines.append("## 🏷️ Claim Verification Taxonomy")
    lines.append("")
    lines.append("| Tag | Meaning | Verification Basis |")
    lines.append("| :--- | :--- | :--- |")
    lines.append("| `[VERIFIED]` | Ground truth | AST syntax parsing, deterministic commands with exit codes |")
    lines.append("| `[OBSERVED]` | Runtime or metadata observation | Pytest warning captures, PyPI warehouse metadata |")
    lines.append("| `[AI-PROPOSED]` | Gemma AI migration proposal | Synthesized by Gemma model; unrehearsed hypothesis |")
    lines.append("| `[REFUTED]` | Invalidated AI hypothesis | Sandbox rehearsal executed and broke tests or syntax |")
    lines.append("| `[AI-GENERATED]` | Synthesized prose | Natural language summary generated by Gemma |")
    lines.append("")

    # Provenance Header
    lines.append("## 📋 Execution Header")
    lines.append("")
    lines.append(f"- **Target Repository:** `{capsule.repo}`")
    lines.append(f"- **Generated At:** `{capsule.generated_at}`")
    m_name = capsule.model.get("name") or capsule.model.get("model_name") or "unknown"
    m_backend = capsule.model.get("backend") or "unknown"
    m_base = capsule.model.get("base_url") or "unknown"
    lines.append(f"- **Model:** `{m_name}` (backend: `{m_backend}`, base URL: `{m_base}`)")
    lines.append("")

    # Stage Statuses
    lines.append("### Pipeline Stages")
    lines.append("")
    lines.append("| Stage | Status | Details |")
    lines.append("| :--- | :---: | :--- |")
    for s in capsule.stages:
        status_badge = f"`{s.status.upper()}`"
        detail_parts = []
        if s.error:
            detail_parts.append(f"Error: {s.error}")
        if s.skip_reason:
            detail_parts.append(f"Skip Reason: {s.skip_reason}")
        details = " | ".join(detail_parts) if detail_parts else "—"
        lines.append(f"| **`{s.stage}`** | {status_badge} | {details} |")
    lines.append("")

    # Executive Summary
    lines.append("## 📝 Executive Summary")
    lines.append("")
    if capsule.summary:
        lines.append(f"> `[{capsule.summary_tag}]` {capsule.summary}")
    elif capsule.summary_error:
        lines.append(f"> **Summary Generation Failed:** `{capsule.summary_error}`")
    else:
        lines.append("> No summary generated.")
    lines.append("")

    # Refuted Section (Key Safety Deliverable)
    refuted = [p for p in capsule.proposals if p.verdict == "refuted"]
    lines.append("## ⚠️ Refuted Proposals (Safety Deliverables)")
    lines.append("")
    if refuted:
        lines.append(
            "> The following proposals generated by Gemma were **REFUTED** by sandbox rehearsal. "
            "Rehearsal proved that applying these changes introduces test regressions or installation failures. "
            "GemmaMorph blocked these proposals from being applied."
        )
        lines.append("")
        for p in refuted:
            rep = p.replacement or "None"
            lines.append(f"### `[REFUTED]` {p.dependency} → `{rep}`")
            lines.append("")
            lines.append(f"- **Rationale:** {p.rationale or 'None'}")
            lines.append("- **Refutation Reasons:**")
            if p.verdict_reasons:
                for r in p.verdict_reasons:
                    lines.append(f"  - ❌ {r}")
            else:
                lines.append("  - ❌ Sandbox rehearsal failed.")
            if p.dropped_edits:
                lines.append("- **Dropped Edits:**")
                for de in p.dropped_edits:
                    lines.append(f"  - Reason: {de.get('reason', 'unknown')}")
            lines.append("")
    else:
        lines.append("No proposals were refuted during rehearsal.")
        lines.append("")

    # Risk Table & Conflicts
    lines.append("## 🔍 Dependency Risk Signals & Conflicts")
    lines.append("")
    if capsule.conflicts:
        lines.append("### Manifest Conflicts")
        lines.append("")
        lines.append("| Dependency | Tag | Detail | Declarations |")
        lines.append("| :--- | :---: | :--- | :--- |")
        for c in capsule.conflicts:
            decls = ", ".join(f"{d.file}:{d.line} (`{d.spec}`)" for d in c.declarations)
            lines.append(f"| **`{c.name}`** | `[{c.tag}]` | {c.detail} | {decls} |")
        lines.append("")

    lines.append("### Risk Signals")
    lines.append("")
    if capsule.signals:
        lines.append("| Dependency | Tag | Signal Kind | Source | Detail |")
        lines.append("| :--- | :---: | :--- | :--- | :--- |")
        for sig in capsule.signals:
            dep_str = f"`{sig.dependency}`" if sig.dependency else "*unattributed*"
            lines.append(f"| {dep_str} | `[{sig.tag}]` | `{sig.kind}` | `{sig.source}` | {sig.detail} |")
    else:
        lines.append("No risk signals recorded.")
    lines.append("")

    # Usages Section
    lines.append("## 📍 Codebase Usages (AST Findings)")
    lines.append("")
    for dep, usages in capsule.usages.items():
        lines.append(f"### Dependency: `{dep}` ({len(usages)} usages)")
        lines.append("")
        if not usages:
            lines.append("*No callsites detected in repository.*")
            lines.append("")
            continue

        lines.append("| File | Line | Kind | Symbol | Function | Tag |")
        lines.append("| :--- | :---: | :---: | :--- | :--- | :---: |")
        for u in usages:
            fn = f"`{u.function}`" if u.function else "*<module>*"
            lines.append(f"| `{u.file}` | `{u.line}` | `{u.kind}` | `{u.symbol}` | {fn} | `[{u.tag}]` |")
        lines.append("")

        # Include snippets
        for u in usages:
            if u.snippet.strip():
                lines.append(f"**Snippet for `{u.file}:{u.line}` ({u.symbol}):**")
                lines.append("```python")
                lines.append(u.snippet.rstrip())
                lines.append("```")
                lines.append("")

    # Proposals Section
    lines.append("## 💡 Migration Proposals")
    lines.append("")
    for p in capsule.proposals:
        if p.error:
            lines.append(f"### Proposal: `{p.dependency}` — no proposal: {p.error}")
            lines.append("")
            continue

        rep = f"`{p.replacement}`" if p.replacement else "*None*"
        lines.append(f"### `{p.dependency}` → {rep}")
        lines.append("")
        lines.append(f"- **Tag:** `[{p.tag}]`")
        lines.append(f"- **Verdict:** `{p.verdict}`")
        if p.verdict_reasons:
            lines.append(f"- **Verdict Reasons:** {', '.join(p.verdict_reasons)}")
        if p.rationale:
            lines.append(f"- **Rationale:** {p.rationale}")

        if p.requirements_change.add or p.requirements_change.remove:
            lines.append("- **Requirements Changes:**")
            if p.requirements_change.remove:
                lines.append(f"  - Remove: `{', '.join(p.requirements_change.remove)}`")
            if p.requirements_change.add:
                lines.append(f"  - Add: `{', '.join(p.requirements_change.add)}`")

        if p.edits:
            lines.append("- **Proposed Code Edits:**")
            for idx, e in enumerate(p.edits, 1):
                lines.append(f"  **Edit {idx} in `{e.file}`:**")
                lines.append("  ```python")
                lines.append(f"  # <<< OLD TEXT\n{e.old_text}")
                lines.append(f"  # >>> NEW TEXT\n{e.new_text}")
                lines.append("  ```")

        if p.dropped_edits:
            lines.append("- **Dropped Edits:**")
            for de in p.dropped_edits:
                lines.append(f"  - Reason: {de.get('reason', 'unknown')}")

        if p.suggested_tests:
            lines.append("- **Suggested Verification Tests:**")
            for st in p.suggested_tests:
                lines.append(f"  - {st}")

        if p.expected_gaps:
            lines.append("- **Expected Compatibility Gaps:**")
            for eg in p.expected_gaps:
                lines.append(f"  - {eg}")
        lines.append("")

    # Rehearsal Section
    lines.append("## 🧪 Sandbox Exit Rehearsal Results")
    lines.append("")
    if capsule.baseline:
        b = capsule.baseline
        lines.append("### Baseline Test Suite Run")
        lines.append("")
        lines.append(f"- **Exit Code:** `{b.exit_code}`")
        lines.append(f"- **Passed:** `{b.passed}` | **Failed:** `{b.failed}` | **Errors:** `{b.errors}` | **Skipped:** `{b.skipped}`")
        if b.warnings:
            lines.append(f"- **Warnings ({len(b.warnings)}):**")
            for w in b.warnings[:5]:
                lines.append(f"  - ⚠️ `{w}`")
            if len(b.warnings) > 5:
                lines.append(f"  - *...and {len(b.warnings) - 5} more*")
        if b.stdout_tail:
            lines.append("```")
            lines.append(b.stdout_tail.rstrip())
            lines.append("```")
        lines.append("")
    else:
        lines.append("*No baseline test run recorded.*")
        lines.append("")

    if capsule.comparisons:
        lines.append("### Proposal Comparison Rehearsals")
        lines.append("")
        for c in capsule.comparisons:
            lines.append(f"#### Comparison: `{c.label}`")
            lines.append("")
            lines.append(f"- **Install OK:** `{'Yes' if c.install_ok else 'No'}`")
            if c.run:
                r = c.run
                lines.append(f"- **Tests:** `{r.passed}` passed, `{r.failed}` failed, exit code `{r.exit_code}`")
                if r.stdout_tail:
                    lines.append("```")
                    lines.append(r.stdout_tail.rstrip())
                    lines.append("```")
            lines.append(f"- **New Failures:** `{', '.join(c.new_failures) if c.new_failures else 'None'}`")
            lines.append(f"- **Newly Passing:** `{', '.join(c.newly_passing) if c.newly_passing else 'None'}`")
            lines.append(f"- **Warnings Added:** `{', '.join(c.warnings_added) if c.warnings_added else 'None'}`")
            lines.append(f"- **Warnings Removed:** `{', '.join(c.warnings_removed) if c.warnings_removed else 'None'}`")

            if c.edit_coverage:
                lines.append("- **Edit Coverage:**")
                lines.append("  | Proposal | File | Lines | Executed | Reason |")
                lines.append("  | :--- | :--- | :---: | :---: | :--- |")
                for ec in c.edit_coverage:
                    lines.append(
                        f"  | `{ec.proposal}` | `{ec.file}` | `{ec.start_line}-{ec.end_line}` | "
                        f"`{'Yes' if ec.executed else 'No'}` | {ec.reason} |"
                    )
            lines.append("")

    # Gaps & Patch
    lines.append("## ⚡ Compatibility Gaps & Proposed Patch")
    lines.append("")
    if capsule.gaps:
        lines.append("### Compatibility Gaps")
        for g in capsule.gaps:
            lines.append(f"- ⚠️ {g}")
        lines.append("")
    else:
        lines.append("No compatibility gaps reported.")
        lines.append("")

    if capsule.patch_path:
        lines.append(f"**Unified Rehearsed Patch Path:** `{capsule.patch_path}`")
    else:
        lines.append("No unified patch artifact produced.")
    lines.append("")

    # Notes
    if capsule.notes:
        lines.append("## 📌 Stage Notes")
        lines.append("")
        for n in capsule.notes:
            lines.append(f"- {n}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------- HTML Renderer


def _h(val: Any) -> str:
    """Safely escape text for HTML attributes and body."""
    if val is None:
        return ""
    return html.escape(str(val))


def _html_badge(tag: str) -> str:
    t = str(tag).upper().strip("[] ")
    cls = "tag-default"
    if "VERIFIED" in t:
        cls = "tag-verified"
    elif "OBSERVED" in t:
        cls = "tag-observed"
    elif "REFUTED" in t:
        cls = "tag-refuted"
    elif "PROPOSED" in t:
        cls = "tag-proposed"
    elif "GENERATED" in t:
        cls = "tag-generated"
    return f'<span class="badge {cls}">[{_h(t)}]</span>'


def _render_html(capsule: Capsule) -> str:
    m_name = capsule.model.get("name") or capsule.model.get("model_name") or "unknown"
    m_backend = capsule.model.get("backend") or "unknown"
    m_base = capsule.model.get("base_url") or "unknown"

    refuted = [p for p in capsule.proposals if p.verdict == "refuted"]

    html_parts: list[str] = []
    html_parts.append(
        """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dependency Exit Capsule</title>
<style>
  :root {
    --bg: #0b0f19;
    --card: #151d2f;
    --border: #23314d;
    --text: #f1f5f9;
    --text-muted: #94a3b8;
    --accent: #38bdf8;
    --verified-bg: #064e3b;
    --verified-text: #6ee7b7;
    --verified-border: #059669;
    --observed-bg: #78350f;
    --observed-text: #fcd34d;
    --observed-border: #d97706;
    --proposed-bg: #1e3a8a;
    --proposed-text: #93c5fd;
    --proposed-border: #2563eb;
    --refuted-bg: #7f1d1d;
    --refuted-text: #fca5a5;
    --refuted-border: #dc2626;
    --generated-bg: #581c87;
    --generated-text: #d8b4fe;
    --generated-border: #9333ea;
  }
  body {
    background-color: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.6;
    margin: 0;
    padding: 24px;
  }
  .container {
    max-width: 1200px;
    margin: 0 auto;
  }
  h1, h2, h3, h4 {
    color: #ffffff;
    margin-top: 1.5em;
    margin-bottom: 0.5em;
  }
  h1 { font-size: 2rem; border-bottom: 2px solid var(--border); padding-bottom: 12px; }
  h2 { font-size: 1.4rem; border-bottom: 1px solid var(--border); padding-bottom: 8px; }
  h3 { font-size: 1.15rem; }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 24px;
    box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.25);
  }
  .alert-refuted {
    background: #3b0d0d;
    border: 2px solid var(--refuted-border);
    border-radius: 8px;
    padding: 20px;
    margin-bottom: 24px;
  }
  .badge {
    display: inline-block;
    padding: 2px 8px;
    font-size: 0.75rem;
    font-weight: 700;
    border-radius: 4px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    border: 1px solid transparent;
    vertical-align: middle;
  }
  .tag-verified { background: var(--verified-bg); color: var(--verified-text); border-color: var(--verified-border); }
  .tag-observed { background: var(--observed-bg); color: var(--observed-text); border-color: var(--observed-border); }
  .tag-proposed { background: var(--proposed-bg); color: var(--proposed-text); border-color: var(--proposed-border); }
  .tag-refuted { background: var(--refuted-bg); color: var(--refuted-text); border-color: var(--refuted-border); }
  .tag-generated { background: var(--generated-bg); color: var(--generated-text); border-color: var(--generated-border); }
  .tag-default { background: #374151; color: #e5e7eb; border-color: #4b5563; }
  table {
    width: 100%;
    border-collapse: collapse;
    margin: 16px 0;
    font-size: 0.92rem;
  }
  th, td {
    padding: 10px 14px;
    text-align: left;
    border: 1px solid var(--border);
  }
  th {
    background: #0d1322;
    font-weight: 600;
  }
  tr:nth-child(even) {
    background: rgba(255, 255, 255, 0.02);
  }
  pre, code {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    font-size: 0.88rem;
  }
  code {
    background: rgba(0, 0, 0, 0.4);
    padding: 2px 6px;
    border-radius: 4px;
  }
  pre {
    background: #080c14;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 12px;
    overflow-x: auto;
    white-space: pre-wrap;
    word-break: break-all;
  }
  pre code {
    background: none;
    padding: 0;
  }
  .grid-2 {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 16px;
  }
  .summary-box {
    background: rgba(0, 0, 0, 0.25);
    border-left: 4px solid var(--accent);
    padding: 14px 18px;
    border-radius: 0 6px 6px 0;
    margin: 16px 0;
  }
</style>
</head>
<body>
<div class="container">
"""
    )

    # Title & Metadata
    html_parts.append("<h1>🛡️ Dependency Exit Capsule</h1>")
    html_parts.append("<div class=\"card\">")
    html_parts.append("<div class=\"grid-2\">")
    html_parts.append(f"<div><strong>Target Repository:</strong> <code>{_h(capsule.repo)}</code></div>")
    html_parts.append(f"<div><strong>Generated At:</strong> <code>{_h(capsule.generated_at)}</code></div>")
    html_parts.append(f"<div><strong>Model:</strong> <code>{_h(m_name)}</code> ({_h(m_backend)})</div>")
    html_parts.append(f"<div><strong>Base URL:</strong> <code>{_h(m_base)}</code></div>")
    html_parts.append("</div></div>")

    # Tag Legend Card
    html_parts.append("<div class=\"card\">")
    html_parts.append("<h3>🏷️ Claim Verification Taxonomy</h3>")
    html_parts.append("<table>")
    html_parts.append("<tr><th>Tag</th><th>Definition</th><th>Verification Method</th></tr>")
    html_parts.append(f"<tr><td>{_html_badge('VERIFIED')}</td><td>Ground Truth</td><td>AST syntax parsing, deterministic commands with exit codes</td></tr>")
    html_parts.append(f"<tr><td>{_html_badge('OBSERVED')}</td><td>Observed Data</td><td>Pytest warning capture, PyPI warehouse metadata</td></tr>")
    html_parts.append(f"<tr><td>{_html_badge('AI-PROPOSED')}</td><td>AI Hypothesis</td><td>Synthesized by Gemma model; unrehearsed</td></tr>")
    html_parts.append(f"<tr><td>{_html_badge('REFUTED')}</td><td>Invalidated Hypothesis</td><td>Failed sandbox rehearsal (broke tests or introduced errors)</td></tr>")
    html_parts.append(f"<tr><td>{_html_badge('AI-GENERATED')}</td><td>AI Prose</td><td>Executive summary generated by Gemma</td></tr>")
    html_parts.append("</table></div>")

    # Pipeline Stages
    html_parts.append("<div class=\"card\">")
    html_parts.append("<h3>Pipeline Stages</h3>")
    html_parts.append("<table>")
    html_parts.append("<tr><th>Stage</th><th>Status</th><th>Details</th></tr>")
    for s in capsule.stages:
        stat_color = "#10b981" if s.status == OK else "#ef4444" if s.status == FAILED else "#f59e0b"
        detail_items = []
        if s.error:
            detail_items.append(f"<strong>Error:</strong> {_h(s.error)}")
        if s.skip_reason:
            detail_items.append(f"<strong>Skip Reason:</strong> {_h(s.skip_reason)}")
        det_str = " | ".join(detail_items) if detail_items else "—"
        html_parts.append(
            f"<tr><td><code>{_h(s.stage)}</code></td>"
            f"<td style=\"color:{stat_color}; font-weight:700;\">{_h(s.status.upper())}</td>"
            f"<td>{det_str}</td></tr>"
        )
    html_parts.append("</table></div>")

    # Executive Summary
    html_parts.append("<div class=\"card\">")
    html_parts.append("<h2>📝 Executive Summary</h2>")
    if capsule.summary:
        html_parts.append(f"<div class=\"summary-box\">{_html_badge(capsule.summary_tag)} {_h(capsule.summary)}</div>")
    elif capsule.summary_error:
        html_parts.append(f"<div class=\"summary-box\" style=\"border-color: #ef4444;\"><strong>Summary Error:</strong> {_h(capsule.summary_error)}</div>")
    else:
        html_parts.append("<p>No summary generated.</p>")
    html_parts.append("</div>")

    # Refuted Section
    if refuted:
        html_parts.append("<div class=\"alert-refuted\">")
        html_parts.append("<h2>⚠️ Refuted Proposals (Key Safety Deliverables)</h2>")
        html_parts.append(
            "<p>The following proposals generated by Gemma were <strong>REFUTED</strong> by sandbox rehearsal. "
            "Rehearsal proved that applying these changes breaks tests or causes install failures. "
            "GemmaMorph prevented these faulty changes from reaching production.</p>"
        )
        for p in refuted:
            rep = p.replacement or "None"
            html_parts.append(f"<h3>{_html_badge('REFUTED')} <code>{_h(p.dependency)}</code> → <code>{_h(rep)}</code></h3>")
            html_parts.append(f"<p><strong>Rationale:</strong> {_h(p.rationale or 'None')}</p>")
            html_parts.append("<p><strong>Refutation Reasons:</strong></p><ul>")
            for r in p.verdict_reasons:
                html_parts.append(f"<li>❌ {_h(r)}</li>")
            html_parts.append("</ul>")
            if p.dropped_edits:
                html_parts.append("<p><strong>Dropped Edits:</strong></p><ul>")
                for de in p.dropped_edits:
                    html_parts.append(f"<li>Reason: {_h(de.get('reason', 'unknown'))}</li>")
                html_parts.append("</ul>")
        html_parts.append("</div>")

    # Risk Signals & Conflicts
    html_parts.append("<div class=\"card\">")
    html_parts.append("<h2>🔍 Dependency Risk Signals & Conflicts</h2>")
    if capsule.conflicts:
        html_parts.append("<h3>Manifest Conflicts</h3>")
        html_parts.append("<table><tr><th>Dependency</th><th>Tag</th><th>Detail</th><th>Declarations</th></tr>")
        for c in capsule.conflicts:
            decls = ", ".join(f"{_h(d.file)}:{d.line} (<code>{_h(d.spec)}</code>)" for d in c.declarations)
            html_parts.append(f"<tr><td><code>{_h(c.name)}</code></td><td>{_html_badge(c.tag)}</td><td>{_h(c.detail)}</td><td>{decls}</td></tr>")
        html_parts.append("</table>")

    html_parts.append("<h3>Risk Signals</h3>")
    if capsule.signals:
        html_parts.append("<table><tr><th>Dependency</th><th>Tag</th><th>Kind</th><th>Source</th><th>Detail</th></tr>")
        for sig in capsule.signals:
            dep_str = f"<code>{_h(sig.dependency)}</code>" if sig.dependency else "<em>unattributed</em>"
            html_parts.append(
                f"<tr><td>{dep_str}</td><td>{_html_badge(sig.tag)}</td>"
                f"<td><code>{_h(sig.kind)}</code></td><td><code>{_h(sig.source)}</code></td><td>{_h(sig.detail)}</td></tr>"
            )
        html_parts.append("</table>")
    else:
        html_parts.append("<p>No risk signals recorded.</p>")
    html_parts.append("</div>")

    # Usages Section
    html_parts.append("<div class=\"card\">")
    html_parts.append("<h2>📍 Codebase Usages (AST Findings)</h2>")
    for dep, usages in capsule.usages.items():
        html_parts.append(f"<h3><code>{_h(dep)}</code> ({len(usages)} usages)</h3>")
        if not usages:
            html_parts.append("<p><em>No callsites detected in repository.</em></p>")
            continue

        html_parts.append("<table><tr><th>File</th><th>Line</th><th>Kind</th><th>Symbol</th><th>Function</th><th>Tag</th></tr>")
        for u in usages:
            fn = f"<code>{_h(u.function)}</code>" if u.function else "<em>&lt;module&gt;</em>"
            html_parts.append(
                f"<tr><td><code>{_h(u.file)}</code></td><td>{u.line}</td><td><code>{_h(u.kind)}</code></td>"
                f"<td><code>{_h(u.symbol)}</code></td><td>{fn}</td><td>{_html_badge(u.tag)}</td></tr>"
            )
        html_parts.append("</table>")

        for u in usages:
            if u.snippet.strip():
                html_parts.append(f"<details style=\"margin: 8px 0;\"><summary>Snippet for <code>{_h(u.file)}:{u.line}</code> (<code>{_h(u.symbol)}</code>)</summary>")
                html_parts.append(f"<pre><code>{_h(u.snippet.rstrip())}</code></pre></details>")
    html_parts.append("</div>")

    # Proposals Section
    html_parts.append("<div class=\"card\">")
    html_parts.append("<h2>💡 Migration Proposals</h2>")
    for p in capsule.proposals:
        if p.error:
            html_parts.append(f"<h3>Proposal: <code>{_h(p.dependency)}</code> — no proposal: {_h(p.error)}</h3>")
            continue

        rep = f"<code>{_h(p.replacement)}</code>" if p.replacement else "<em>None</em>"
        html_parts.append(f"<h3><code>{_h(p.dependency)}</code> → {rep}</h3>")
        html_parts.append(f"<p><strong>Tag:</strong> {_html_badge(p.tag)} | <strong>Verdict:</strong> <code>{_h(p.verdict)}</code></p>")
        if p.verdict_reasons:
            html_parts.append(f"<p><strong>Verdict Reasons:</strong> {_h(', '.join(p.verdict_reasons))}</p>")
        if p.rationale:
            html_parts.append(f"<p><strong>Rationale:</strong> {_h(p.rationale)}</p>")

        if p.requirements_change.add or p.requirements_change.remove:
            html_parts.append("<p><strong>Requirements Changes:</strong></p><ul>")
            if p.requirements_change.remove:
                html_parts.append(f"<li>Remove: <code>{_h(', '.join(p.requirements_change.remove))}</code></li>")
            if p.requirements_change.add:
                html_parts.append(f"<li>Add: <code>{_h(', '.join(p.requirements_change.add))}</code></li>")
            html_parts.append("</ul>")

        if p.edits:
            html_parts.append("<p><strong>Proposed Code Edits:</strong></p>")
            for idx, e in enumerate(p.edits, 1):
                html_parts.append(f"<details><summary>Edit {idx} in <code>{_h(e.file)}</code></summary>")
                html_parts.append(f"<pre><code># &lt;&lt;&lt; OLD TEXT\n{_h(e.old_text)}\n\n# &gt;&gt;&gt; NEW TEXT\n{_h(e.new_text)}</code></pre></details>")

        if p.dropped_edits:
            html_parts.append("<p><strong>Dropped Edits:</strong></p><ul>")
            for de in p.dropped_edits:
                html_parts.append(f"<li>Reason: {_h(de.get('reason', 'unknown'))}</li>")
            html_parts.append("</ul>")

        if p.suggested_tests:
            html_parts.append("<p><strong>Suggested Verification Tests:</strong></p><ul>")
            for st in p.suggested_tests:
                html_parts.append(f"<li>{_h(st)}</li>")
            html_parts.append("</ul>")

        if p.expected_gaps:
            html_parts.append("<p><strong>Expected Gaps:</strong></p><ul>")
            for eg in p.expected_gaps:
                html_parts.append(f"<li>{_h(eg)}</li>")
            html_parts.append("</ul>")
    html_parts.append("</div>")

    # Rehearsal Section
    html_parts.append("<div class=\"card\">")
    html_parts.append("<h2>🧪 Sandbox Exit Rehearsal Results</h2>")
    if capsule.baseline:
        b = capsule.baseline
        html_parts.append("<h3>Baseline Test Suite Run</h3>")
        html_parts.append(f"<p><strong>Exit Code:</strong> <code>{b.exit_code}</code> | <strong>Passed:</strong> {b.passed} | <strong>Failed:</strong> {b.failed} | <strong>Errors:</strong> {b.errors} | <strong>Skipped:</strong> {b.skipped}</p>")
        if b.warnings:
            html_parts.append(f"<p><strong>Warnings ({len(b.warnings)}):</strong></p><ul>")
            for w in b.warnings[:5]:
                html_parts.append(f"<li>⚠️ <code>{_h(w)}</code></li>")
            if len(b.warnings) > 5:
                html_parts.append(f"<li><em>...and {len(b.warnings) - 5} more</em></li>")
            html_parts.append("</ul>")
        if b.stdout_tail:
            html_parts.append(f"<pre><code>{_h(b.stdout_tail.rstrip())}</code></pre>")

    if capsule.comparisons:
        html_parts.append("<h3>Proposal Comparison Rehearsals</h3>")
        for c in capsule.comparisons:
            html_parts.append(f"<h4>Comparison: <code>{_h(c.label)}</code></h4>")
            html_parts.append(f"<p><strong>Install OK:</strong> {'Yes' if c.install_ok else 'No'}</p>")
            if c.run:
                r = c.run
                html_parts.append(f"<p><strong>Tests:</strong> {r.passed} passed, {r.failed} failed, exit code <code>{r.exit_code}</code></p>")
                if r.stdout_tail:
                    html_parts.append(f"<pre><code>{_h(r.stdout_tail.rstrip())}</code></pre>")
            html_parts.append(f"<p><strong>New Failures:</strong> <code>{_h(', '.join(c.new_failures) if c.new_failures else 'None')}</code></p>")
            html_parts.append(f"<p><strong>Newly Passing:</strong> <code>{_h(', '.join(c.newly_passing) if c.newly_passing else 'None')}</code></p>")
            html_parts.append(f"<p><strong>Warnings Added:</strong> <code>{_h(', '.join(c.warnings_added) if c.warnings_added else 'None')}</code></p>")
            html_parts.append(f"<p><strong>Warnings Removed:</strong> <code>{_h(', '.join(c.warnings_removed) if c.warnings_removed else 'None')}</code></p>")

            if c.edit_coverage:
                html_parts.append("<p><strong>Edit Coverage:</strong></p>")
                html_parts.append("<table><tr><th>Proposal</th><th>File</th><th>Lines</th><th>Executed</th><th>Reason</th></tr>")
                for ec in c.edit_coverage:
                    html_parts.append(
                        f"<tr><td><code>{_h(ec.proposal)}</code></td><td><code>{_h(ec.file)}</code></td>"
                        f"<td>{ec.start_line}-{ec.end_line}</td><td>{'Yes' if ec.executed else 'No'}</td><td>{_h(ec.reason)}</td></tr>"
                    )
                html_parts.append("</table>")
    html_parts.append("</div>")

    # Gaps & Patch
    html_parts.append("<div class=\"card\">")
    html_parts.append("<h2>⚡ Compatibility Gaps & Patch</h2>")
    if capsule.gaps:
        html_parts.append("<ul>")
        for g in capsule.gaps:
            html_parts.append(f"<li>⚠️ {_h(g)}</li>")
        html_parts.append("</ul>")
    else:
        html_parts.append("<p>No compatibility gaps reported.</p>")

    if capsule.patch_path:
        html_parts.append(f"<p><strong>Unified Rehearsed Patch Path:</strong> <code>{_h(capsule.patch_path)}</code></p>")
    else:
        html_parts.append("<p>No unified patch artifact produced.</p>")
    html_parts.append("</div>")

    # Notes
    if capsule.notes:
        html_parts.append("<div class=\"card\">")
        html_parts.append("<h2>📌 Stage Notes</h2><ul>")
        for n in capsule.notes:
            html_parts.append(f"<li>{_h(n)}</li>")
        html_parts.append("</ul></div>")

    html_parts.append("</div>\n</body>\n</html>")
    return "\n".join(html_parts)
