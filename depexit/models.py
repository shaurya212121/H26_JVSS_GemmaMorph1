"""
Shared data contract for every GemmaMorph stage.

Each stage takes the previous stage's typed output and returns one of the
result classes below. Nothing is passed around as an untyped dict except the
raw JSON that Gemma returns, which is validated into a Proposal first.

Claim tags (see TAGS):
  VERIFIED     a command we ran, with its real exit code and output
  OBSERVED     seen in real run output or in PyPI metadata
  AI-PROPOSED  Gemma suggested it; not yet proven
  REFUTED      Gemma proposed it and a rehearsal showed it fails
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

VERIFIED = "VERIFIED"
OBSERVED = "OBSERVED"
AI_PROPOSED = "AI-PROPOSED"
REFUTED = "REFUTED"
AI_GENERATED = "AI-GENERATED"
TAGS = (VERIFIED, OBSERVED, AI_PROPOSED, REFUTED, AI_GENERATED)

OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass
class StageStatus:
    stage: str
    status: str = OK  # ok | failed | skipped
    error: Optional[str] = None
    skip_reason: Optional[str] = None


# ---------------------------------------------------------------- S1 discover

@dataclass
class Declaration:
    file: str  # repo-relative, posix separators
    line: int  # 1-based
    spec: str  # the specifier text as written, e.g. "==1.10.13" or ">=2" ("" if unpinned)


@dataclass
class Dependency:
    name: str  # normalized
    declared: list[Declaration] = field(default_factory=list)


@dataclass
class Conflict:
    name: str
    declarations: list[Declaration]
    detail: str
    tag: str = VERIFIED  # computed by parsing the files, no model involved


@dataclass
class DiscoveryResult:
    repo: str
    dependencies: list[Dependency]
    conflicts: list[Conflict]
    notes: list[str] = field(default_factory=list)  # unreadable files, unparsable lines
    status: StageStatus = field(default_factory=lambda: StageStatus("discover"))


# ------------------------------------------------------------- S2 risk scan

@dataclass
class RiskSignal:
    kind: str  # e.g. stale-release, yanked, inactive, python-requires, version-lag,
    #           test-warning, test-failure, install-failure, undeclared-import, pypi-unavailable
    detail: str
    source: str  # e.g. "pypi:jinja2", "pytest:tests/test_x.py::test_y", "ast:linkwatch/checker.py"
    tag: str  # OBSERVED (metadata or run output) or VERIFIED (parsed/derived by us)
    dependency: Optional[str] = None  # normalized dist name when it can be attributed


@dataclass
class RunResult:
    label: str
    command: list[str]
    exit_code: Optional[int]  # None when the process could not start
    passed: int = 0
    failed: int = 0
    errors: int = 0
    stdout_tail: str = ""
    timed_out: bool = False
    skipped: int = 0
    tests: dict[str, str] = field(default_factory=dict)  # nodeid -> passed|failed|error|skipped
    warnings: list[str] = field(default_factory=list)
    tag: str = VERIFIED


@dataclass
class RiskScanResult:
    signals: list[RiskSignal]
    baseline: Optional[RunResult]  # the target's own tests in a fresh venv
    install_runs: list[RunResult]
    module_map: dict[str, list[str]]  # top-level module -> normalized dist names (from the venv)
    flagged: list[str]  # normalized dist names with at least one signal
    notes: list[str] = field(default_factory=list)
    status: StageStatus = field(default_factory=lambda: StageStatus("risk_scan"))


# ----------------------------------------------------------------- S3 usage

@dataclass
class Usage:
    file: str
    line: int
    symbol: str  # dotted name as written, e.g. "requests.utils._parse_content_type_header"
    snippet: str  # raw source lines around the usage, copyable verbatim
    dependency: str = ""  # normalized dist name
    kind: str = "attribute"  # import | attribute | private-api | monkeypatch
    function: str = ""  # enclosing function/class, "" at module level
    tag: str = VERIFIED


@dataclass
class UsageResult:
    usages: dict[str, list[Usage]]  # dist name -> usages
    notes: list[str] = field(default_factory=list)  # files that could not be parsed
    status: StageStatus = field(default_factory=lambda: StageStatus("usage"))


# --------------------------------------------------------------- S4 propose

@dataclass
class Edit:
    file: str
    old_text: str
    new_text: str


@dataclass
class RequirementsChange:
    remove: list[str] = field(default_factory=list)
    add: list[str] = field(default_factory=list)


@dataclass
class Proposal:
    dependency: str
    replacement: Optional[str] = None
    edits: list[Edit] = field(default_factory=list)
    requirements_change: RequirementsChange = field(default_factory=RequirementsChange)
    suggested_tests: list[str] = field(default_factory=list)
    expected_gaps: list[str] = field(default_factory=list)
    tag: str = AI_PROPOSED
    rationale: str = ""
    dropped_edits: list[dict[str, Any]] = field(default_factory=list)  # {edit, reason}
    error: Optional[str] = None  # set when no proposal could be produced
    verdict: str = "unrehearsed"  # unrehearsed | verified | unproven | refuted
    verdict_reasons: list[str] = field(default_factory=list)


@dataclass
class ProposalResult:
    proposals: list[Proposal]
    status: StageStatus = field(default_factory=lambda: StageStatus("propose"))


# -------------------------------------------------------------- S5 rehearse

@dataclass
class EditCoverage:
    proposal: str
    file: str
    start_line: int
    end_line: int
    executed: bool
    reason: str


@dataclass
class Comparison:
    label: str  # proposal name or "combined"
    run: Optional[RunResult]
    new_failures: list[str] = field(default_factory=list)
    newly_passing: list[str] = field(default_factory=list)
    warnings_added: list[str] = field(default_factory=list)
    warnings_removed: list[str] = field(default_factory=list)
    edit_coverage: list[EditCoverage] = field(default_factory=list)
    install_ok: bool = True


@dataclass
class RehearsalResult:
    baseline: Optional[RunResult]
    comparisons: list[Comparison]
    gaps: list[str]
    patch_path: Optional[str]
    notes: list[str] = field(default_factory=list)
    status: StageStatus = field(default_factory=lambda: StageStatus("rehearse"))


# --------------------------------------------------------------- S6 capsule

@dataclass
class Capsule:
    generated_at: str
    repo: str
    model: dict[str, Any]
    stages: list[StageStatus]
    dependencies: list[Dependency]
    conflicts: list[Conflict]
    signals: list[RiskSignal]
    usages: dict[str, list[Usage]]
    proposals: list[Proposal]
    baseline: Optional[RunResult]
    comparisons: list[Comparison]
    gaps: list[str]
    patch_path: Optional[str]
    summary: Optional[str]  # plain-English Gemma summary
    summary_tag: str = AI_GENERATED
    summary_error: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProposalSchemaError(ValueError):
    """Gemma's JSON did not match the Proposal schema."""


def _str_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ProposalSchemaError(f"'{name}' must be a list of strings")
    return value


def proposal_from_json(dependency: str, data: Any) -> Proposal:
    """
    Validate Gemma's JSON object against the Proposal schema.

    Required keys: replacement (string or null), edits (list of
    {file, old_text, new_text}), requirements_change ({remove:[str], add:[str]}),
    suggested_tests (list of str), expected_gaps (list of str). "rationale" is optional.
    """
    if not isinstance(data, dict):
        raise ProposalSchemaError("top-level value must be a JSON object")
    required = ("replacement", "edits", "requirements_change", "suggested_tests", "expected_gaps")
    missing = [k for k in required if k not in data]
    if missing:
        raise ProposalSchemaError(f"missing required keys: {', '.join(missing)}")

    replacement = data["replacement"]
    if replacement is not None and not isinstance(replacement, str):
        raise ProposalSchemaError("'replacement' must be a string or null")

    raw_edits = data["edits"]
    if not isinstance(raw_edits, list):
        raise ProposalSchemaError("'edits' must be a list")
    edits: list[Edit] = []
    for i, e in enumerate(raw_edits):
        if not isinstance(e, dict):
            raise ProposalSchemaError(f"edits[{i}] must be an object")
        for key in ("file", "old_text", "new_text"):
            if not isinstance(e.get(key), str):
                raise ProposalSchemaError(f"edits[{i}].{key} must be a string")
        edits.append(Edit(file=e["file"], old_text=e["old_text"], new_text=e["new_text"]))

    rc = data["requirements_change"]
    if not isinstance(rc, dict):
        raise ProposalSchemaError("'requirements_change' must be an object with 'remove' and 'add'")
    change = RequirementsChange(
        remove=_str_list(rc.get("remove"), "requirements_change.remove"),
        add=_str_list(rc.get("add"), "requirements_change.add"),
    )

    rationale = data.get("rationale", "")
    if not isinstance(rationale, str):
        raise ProposalSchemaError("'rationale' must be a string")

    return Proposal(
        dependency=dependency,
        replacement=replacement,
        edits=edits,
        requirements_change=change,
        suggested_tests=_str_list(data["suggested_tests"], "suggested_tests"),
        expected_gaps=_str_list(data["expected_gaps"], "expected_gaps"),
        rationale=rationale,
    )
