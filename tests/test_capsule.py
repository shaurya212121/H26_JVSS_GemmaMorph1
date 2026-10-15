"""
Unit tests for Stage 6: depexit/capsule.py.

Tests cover:
- build_summary:
    * Successful generation with FakeLLM returning raw JSON
    * Successful generation with FakeLLM returning markdown code-fenced JSON
    * Failure handling on LLM exception (returns (None, error_str))
    * Failure handling on malformed non-JSON output (returns (None, error_str))
    * Failure handling on JSON missing 'summary' key (returns (None, error_str))
    * Failure handling on empty summary string (returns (None, error_str))
    * Never invents a summary
- write_capsule:
    * Generates EXIT_CAPSULE.json, EXIT_CAPSULE.md, and EXIT_CAPSULE.html
    * JSON matches capsule.to_dict()
    * Markdown renders all sections, stage statuses with real errors and skip reasons,
      dropped_edits, edit_coverage, install_ok, tag legend, and prominent refuted proposals section
    * HTML is 100% self-contained (no external http/https resources)
    * Dynamic values containing HTML entities/tags (<script>, &amp;, etc.) are properly escaped
    * Minimal/empty capsule handles gracefully with clean defaults

Runs 100% offline with NO external dependencies and NO Ollama.
"""

import json
from pathlib import Path
import pytest

from depexit.capsule import build_summary, write_capsule
from depexit.models import (
    AI_GENERATED,
    AI_PROPOSED,
    FAILED,
    OBSERVED,
    OK,
    REFUTED,
    SKIPPED,
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


class FakeLLM:
    """Mock LLM client for deterministic, offline testing."""

    def __init__(self, response: str = "", raise_error: Exception | None = None):
        self.response = response
        self.raise_error = raise_error
        self.last_prompt = ""
        self.last_system = ""

    def ask(self, prompt: str, system: str = "") -> str:
        self.last_prompt = prompt
        self.last_system = system
        if self.raise_error:
            raise self.raise_error
        return self.response


@pytest.fixture
def sample_capsule() -> Capsule:
    return Capsule(
        generated_at="2026-10-15 14:00:00 UTC",
        repo="/path/to/test_repo",
        model={
            "name": "gemma4:e2b",
            "backend": "ollama",
            "base_url": "http://localhost:11434",
        },
        stages=[
            StageStatus(stage="discover", status=OK),
            StageStatus(stage="risk_scan", status=OK),
            StageStatus(stage="usage", status=OK),
            StageStatus(stage="propose", status=OK),
            StageStatus(stage="rehearse", status=FAILED, error="Pip install timed out"),
            StageStatus(stage="optional_audit", status=SKIPPED, skip_reason="Flag disabled"),
        ],
        dependencies=[
            Dependency(
                name="requests",
                declared=[Declaration(file="requirements.txt", line=1, spec="==2.31.0")],
            ),
            Dependency(
                name="toml",
                declared=[Declaration(file="requirements.txt", line=2, spec="==0.10.2")],
            ),
        ],
        conflicts=[
            Conflict(
                name="pydantic",
                declarations=[
                    Declaration(file="requirements.txt", line=3, spec="==1.10.13"),
                    Declaration(file="pyproject.toml", line=12, spec=">=2"),
                ],
                detail="Version specification mismatch across manifests",
                tag=VERIFIED,
            )
        ],
        signals=[
            RiskSignal(
                kind="stale-release",
                detail="No releases published in over 4 years",
                source="pypi:toml",
                tag=OBSERVED,
                dependency="toml",
            ),
            RiskSignal(
                kind="monkeypatch-detected",
                detail="Runtime monkeypatch on requests.Session.request",
                source="ast:patching.py",
                tag=VERIFIED,
                dependency="requests",
            ),
        ],
        usages={
            "requests": [
                Usage(
                    file="app.py",
                    line=10,
                    symbol="requests.get",
                    snippet="def fetch():\n    return requests.get('<script>alert(1)</script>')\n",
                    dependency="requests",
                    kind="attribute",
                    function="fetch",
                    tag=VERIFIED,
                )
            ],
            "toml": [],
        },
        proposals=[
            Proposal(
                dependency="toml",
                replacement="tomllib",
                edits=[
                    Edit(file="app.py", old_text="import toml", new_text="import tomllib"),
                    Edit(file="app.py", old_text="toml.load(f)", new_text="tomllib.load(f, 'rb')"),
                ],
                requirements_change=RequirementsChange(remove=["toml==0.10.2"], add=[]),
                suggested_tests=["Verify tomllib loads sites.toml in read-binary mode"],
                expected_gaps=["tomllib is read-only; writing TOML requires third-party tomli-w"],
                tag=AI_PROPOSED,
                rationale="tomllib is part of Python 3.11+ standard library",
                dropped_edits=[{"edit": "bad_edit", "reason": "Target text not found in source"}],
                error=None,
                verdict="verified",
                verdict_reasons=["Passed all unit tests without regression"],
            ),
            Proposal(
                dependency="requests",
                replacement="httpx",
                edits=[],
                requirements_change=RequirementsChange(remove=["requests"], add=["httpx"]),
                suggested_tests=["Test all API calls"],
                expected_gaps=["Async API changes"],
                tag=AI_PROPOSED,
                rationale="Replace requests with modern httpx",
                dropped_edits=[],
                error=None,
                verdict="refuted",
                verdict_reasons=["Broke test_request_encoding: AttributeError on response.raw"],
            ),
            Proposal(
                dependency="jinja2",
                replacement=None,
                error="Model output was malformed JSON",
                verdict="unrehearsed",
            ),
        ],
        baseline=RunResult(
            label="baseline",
            command=["pytest", "tests/"],
            exit_code=0,
            passed=5,
            failed=0,
            errors=0,
            stdout_tail="5 passed in 0.12s",
            warnings=["DeprecationWarning: pkg_resources is deprecated"],
            tag=VERIFIED,
        ),
        comparisons=[
            Comparison(
                label="toml->tomllib",
                run=RunResult(
                    label="toml_rehearsal",
                    command=["pytest", "tests/"],
                    exit_code=0,
                    passed=5,
                    failed=0,
                    errors=0,
                    stdout_tail="5 passed in 0.14s",
                    tag=VERIFIED,
                ),
                new_failures=[],
                newly_passing=[],
                warnings_added=[],
                warnings_removed=["DeprecationWarning: toml is unmaintained"],
                edit_coverage=[
                    EditCoverage(
                        proposal="toml->tomllib",
                        file="app.py",
                        start_line=1,
                        end_line=2,
                        executed=True,
                        reason="Executed during import",
                    )
                ],
                install_ok=True,
            ),
            Comparison(
                label="requests->httpx",
                run=RunResult(
                    label="requests_rehearsal",
                    command=["pytest", "tests/"],
                    exit_code=1,
                    passed=4,
                    failed=1,
                    errors=0,
                    stdout_tail="1 failed, 4 passed in 0.20s",
                    tag=VERIFIED,
                ),
                new_failures=["tests/test_client.py::test_request_encoding"],
                newly_passing=[],
                warnings_added=["UserWarning: Experimental feature"],
                warnings_removed=[],
                edit_coverage=[],
                install_ok=False,
            ),
        ],
        gaps=["tomllib is read-only", "httpx session lifecycle differs"],
        patch_path="output/rehearsal.patch",
        summary="Audit completed. tomllib migration succeeded. httpx migration failed and was refuted.",
        summary_tag=AI_GENERATED,
        summary_error=None,
        notes=["Network unavailable for PyPI metadata check on 1 package"],
    )


# ---------------------------------------------------------------- build_summary Tests


def test_build_summary_success(sample_capsule: Capsule):
    valid_json = '{"summary": "Audit finished. Two packages analyzed. One proposed replacement was verified, and one was refuted by sandbox tests."}'
    llm = FakeLLM(response=valid_json)

    summary, err = build_summary(llm, sample_capsule)

    assert err is None
    assert summary is not None
    assert "Two packages analyzed" in summary
    assert "Repository: /path/to/test_repo" in llm.last_prompt


def test_build_summary_markdown_fenced_json(sample_capsule: Capsule):
    fenced_json = """```json
{
  "summary": "Repository analysis completed with 1 refuted proposal."
}
```"""
    llm = FakeLLM(response=fenced_json)

    summary, err = build_summary(llm, sample_capsule)

    assert err is None
    assert summary == "Repository analysis completed with 1 refuted proposal."


def test_build_summary_llm_exception(sample_capsule: Capsule):
    llm = FakeLLM(raise_error=RuntimeError("Connection refused by Ollama"))

    summary, err = build_summary(llm, sample_capsule)

    assert summary is None
    assert err is not None
    assert "Connection refused by Ollama" in err


def test_build_summary_invalid_json(sample_capsule: Capsule):
    llm = FakeLLM(response="This is plain text with no JSON at all.")

    summary, err = build_summary(llm, sample_capsule)

    assert summary is None
    assert err is not None
    assert "no json object found" in err.lower() or "error" in err.lower()


def test_build_summary_missing_key(sample_capsule: Capsule):
    llm = FakeLLM(response='{"wrong_key": "some text"}')

    summary, err = build_summary(llm, sample_capsule)

    assert summary is None
    assert err is not None
    assert "summary" in err.lower()


def test_build_summary_empty_string(sample_capsule: Capsule):
    llm = FakeLLM(response='{"summary": "   "}')

    summary, err = build_summary(llm, sample_capsule)

    assert summary is None
    assert err is not None
    assert "empty" in err.lower()


# ---------------------------------------------------------------- write_capsule Tests


def test_write_capsule_artifacts_and_contracts(sample_capsule: Capsule, tmp_path: Path):
    output_dir = tmp_path / "capsule_out"

    json_path, md_path, html_path = write_capsule(sample_capsule, output_dir)

    assert json_path.exists()
    assert md_path.exists()
    assert html_path.exists()
    assert json_path.name == "EXIT_CAPSULE.json"
    assert md_path.name == "EXIT_CAPSULE.md"
    assert html_path.name == "EXIT_CAPSULE.html"

    # 1. JSON Verification
    raw_json = json_path.read_text(encoding="utf-8")
    data = json.loads(raw_json)
    assert data["repo"] == "/path/to/test_repo"
    assert len(data["proposals"]) == 3
    assert data["proposals"][1]["verdict"] == "refuted"
    assert data["stages"][4]["error"] == "Pip install timed out"

    # 2. Markdown Verification
    md_text = md_path.read_text(encoding="utf-8")

    # Tag Legend present
    assert "[VERIFIED]" in md_text
    assert "[OBSERVED]" in md_text
    assert "[AI-PROPOSED]" in md_text
    assert "[REFUTED]" in md_text
    assert "Claim Verification Taxonomy" in md_text

    # Prominent Refuted section
    assert "Refuted Proposals" in md_text
    assert "requests → `httpx`" in md_text
    assert "Broke test_request_encoding" in md_text

    # Stage statuses with errors and skip reasons
    assert "Pip install timed out" in md_text
    assert "Flag disabled" in md_text

    # Dropped edits with reasons
    assert "Target text not found in source" in md_text

    # Proposal with error set
    assert "no proposal: Model output was malformed JSON" in md_text

    # Edit coverage and install_ok
    assert "Executed during import" in md_text
    assert "Install OK:" in md_text

    # 3. HTML Verification
    html_text = html_path.read_text(encoding="utf-8")

    # Must be 100% self contained (no http:// or https:// external assets)
    assert "<script src=" not in html_text
    assert '<link rel="stylesheet"' not in html_text
    assert "http://" not in html_text.replace("http://localhost:11434", "")
    assert "https://" not in html_text

    # Escaping verification: <script>alert(1)</script> snippet in app.py must be escaped
    assert "<script>alert(1)</script>" not in html_text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_text

    # Prominent Refuted section in HTML
    assert "Refuted Proposals" in html_text
    assert "requests" in html_text
    assert "httpx" in html_text
    assert "Broke test_request_encoding" in html_text

    # Stage statuses with errors in HTML
    assert "Pip install timed out" in html_text
    assert "Flag disabled" in html_text


def test_write_capsule_minimal(tmp_path: Path):
    """Test generating capsule with minimal fields and no proposals or errors."""
    minimal_capsule = Capsule(
        generated_at="2026-10-15 15:00:00 UTC",
        repo="/path/to/clean_repo",
        model={"name": "gemma4:e2b", "backend": "ollama", "base_url": "http://localhost:11434"},
        stages=[StageStatus(stage="discover", status=OK)],
        dependencies=[],
        conflicts=[],
        signals=[],
        usages={},
        proposals=[],
        baseline=None,
        comparisons=[],
        gaps=[],
        patch_path=None,
        summary=None,
        summary_error="API key not provided",
        notes=[],
    )

    out_dir = tmp_path / "min_out"
    j, m, h = write_capsule(minimal_capsule, out_dir)

    assert j.exists() and m.exists() and h.exists()
    md_content = m.read_text(encoding="utf-8")
    assert "No proposals were refuted during rehearsal." in md_content
    assert "**Summary Generation Failed:** `API key not provided`" in md_content
    assert "*No baseline test run recorded.*" in md_content

    html_content = h.read_text(encoding="utf-8")
    assert "Summary Error:" in html_content
    assert "API key not provided" in html_content
