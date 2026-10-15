"""
Stage 4: Gemma 4 proposals.

For every flagged dependency Gemma receives the real usage snippets and the
risk signals S2 collected, and returns one JSON object (replacement, exact
old_text -> new_text edits, requirements change, suggested tests, expected
gaps). Nothing is pre-written: the prompt carries no replacement table.

Defensive handling:
  * take the outermost {...} from the reply and validate it against the
    Proposal schema; on failure ask ONCE more, quoting the validation error
  * every edit is checked against the real file; an edit whose old_text is not
    found exactly once is dropped and logged (kept in proposal.dropped_edits)
  * if the model is unreachable the remaining dependencies are marked as not
    attempted and the stage reports failed with the real error
Every proposal is tagged AI-PROPOSED here; only a rehearsal can change that.
"""

from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path
from typing import Protocol

from depexit.edits import EditError, apply_edit, read_lf, resolve_in_repo
from depexit.llm import LLMError, extract_json_object
from depexit.log import log
from depexit.models import (
    FAILED, DiscoveryResult, Edit, Proposal, ProposalResult, ProposalSchemaError,
    RiskScanResult, RiskSignal, StageStatus, Usage, UsageResult, proposal_from_json,
)

MAX_USAGES = 25
MAX_SIGNALS = 15

SYSTEM_PROMPT = (
    "You are a careful Python dependency migration assistant. You reply with a single JSON "
    "object and nothing else. You only use the evidence you are given. If you are not sure "
    "about an edit, leave it out and describe the gap in expected_gaps."
)

SCHEMA = """{
  "replacement": "package to use instead (same name when upgrading in place, 'stdlib: <module>' for a standard-library module) or null",
  "rationale": "one or two sentences using the evidence above",
  "edits": [{"file": "path exactly as shown above", "old_text": "text copied exactly from the source lines above", "new_text": "replacement text"}],
  "requirements_change": {"remove": ["requirement strings to remove"], "add": ["requirement strings to add"]},
  "suggested_tests": ["tests that would show the change works"],
  "expected_gaps": ["behaviour differences or things this proposal does not cover"]
}"""


class LLM(Protocol):
    def ask(self, prompt: str, system: str = "") -> str: ...


def _signal_lines(signals: list[RiskSignal]) -> str:
    if not signals:
        return "- none"
    return "\n".join(f"- [{s.tag}] {s.kind}: {s.detail} ({s.source})" for s in signals[:MAX_SIGNALS])


def _usage_blocks(usages: list[Usage]) -> str:
    if not usages:
        return "(no usages were found in the repository's Python files)"
    blocks = []
    for u in usages[:MAX_USAGES]:
        where = f" in {u.function}" if u.function else ""
        blocks.append(f"### {u.file}:{u.line} [{u.kind}] {u.symbol}{where}\n{u.snippet}")
    more = len(usages) - MAX_USAGES
    return "\n\n".join(blocks) + (f"\n\n(... {more} more usages not shown)" if more > 0 else "")


def build_prompt(name: str, declared: str, signals: list[RiskSignal], usages: list[Usage]) -> str:
    return f"""Dependency: {name}
Declared in the repository: {declared}
Python: {sys.version_info.major}.{sys.version_info.minor} on Windows

Risk evidence:
{_signal_lines(signals)}

Where the repository uses it (exact source lines follow each header):
{_usage_blocks(usages)}

Task: propose how this repository should deal with the dependency '{name}' given the evidence.
Reply with ONE JSON object with exactly these keys:
{SCHEMA}

Rules:
1. old_text must be copied character for character from the source lines above, including indentation. Use "edits": [] when no source change is needed.
2. Requirement strings look like "name==1.2.3" or "name>=2". To upgrade in place, remove the old requirement and add the new one.
3. Do not invent file paths, functions or evidence that are not shown above.
4. If the evidence shows the dependency is already at its latest release, upgrading cannot fix it: propose a different package or a standard-library module, or keep it (replacement null, no edits).
5. If you remove an import, every use of it must be replaced in the same proposal, otherwise the code breaks."""


def validate_edits(repo: Path, proposal: Proposal) -> None:
    """Keep only edits that apply to the real files; log and record the rest."""
    kept: list[Edit] = []
    for edit in proposal.edits:
        try:
            path = resolve_in_repo(repo, edit.file)
            text, _ = read_lf(path)
            apply_edit(text, edit.old_text, edit.new_text)
        except EditError as exc:
            log(f"    dropped edit in {edit.file}: {exc}")
            proposal.dropped_edits.append({"edit": asdict(edit), "reason": str(exc)})
            continue
        kept.append(edit)
    proposal.edits = kept


def _declared(discovery: DiscoveryResult, name: str) -> str:
    for dep in discovery.dependencies:
        if dep.name == name:
            return "; ".join(f"{d.file}:{d.line} {name}{d.spec}" for d in dep.declared)
    return "not declared in any requirements file or pyproject.toml"


def propose_one(llm: LLM, repo: Path, name: str, declared: str, signals: list[RiskSignal],
                usages: list[Usage]) -> Proposal:
    """Ask Gemma about one dependency. Raises LLMError if the model cannot be reached."""
    prompt = build_prompt(name, declared, signals, usages)
    reply = llm.ask(prompt, SYSTEM_PROMPT)
    for attempt in (1, 2):
        try:
            proposal = proposal_from_json(name, extract_json_object(reply))
            break
        except (ValueError, ProposalSchemaError) as exc:
            if attempt == 2:
                return Proposal(dependency=name, error=f"model reply did not match the schema after one retry: {exc}")
            log(f"    reply rejected ({exc}); asking once more")
            reply = llm.ask(
                f"{prompt}\n\nYour previous reply was rejected: {exc}\n"
                f"Previous reply:\n{reply}\n\nReply again with ONE corrected JSON object.",
                SYSTEM_PROMPT,
            )
    validate_edits(repo, proposal)
    return proposal


def propose_migrations(
    llm: LLM,
    repo: Path,
    discovery: DiscoveryResult,
    risk: RiskScanResult,
    usages: UsageResult,
) -> ProposalResult:
    repo = Path(repo).resolve()
    proposals: list[Proposal] = []
    unreachable: str | None = None
    for name in risk.flagged:
        if unreachable:
            proposals.append(Proposal(dependency=name, error=f"not attempted: model unreachable ({unreachable})"))
            continue
        log(f"  proposing for {name}")
        signals = [s for s in risk.signals if s.dependency == name]
        try:
            proposals.append(propose_one(llm, repo, name, _declared(discovery, name), signals,
                                         usages.usages.get(name, [])))
        except LLMError as exc:
            unreachable = str(exc)
            proposals.append(Proposal(dependency=name, error=f"model call failed: {exc}"))

    failed = [p for p in proposals if p.error]
    status = StageStatus("propose")
    if failed:
        cause = unreachable or failed[0].error  # each proposal keeps its own full error; the stage line states the cause once
        status = StageStatus("propose", FAILED, error=(
            f"{len(failed)} of {len(proposals)} proposals failed ({', '.join(p.dependency for p in failed)}). Cause: {cause[:240]}"))
    log(f"  {len(proposals) - len(failed)} proposals, {len(failed)} failed")
    return ProposalResult(proposals=proposals, status=status)
