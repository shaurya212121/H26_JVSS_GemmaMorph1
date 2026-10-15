import json

import pytest

from depexit.gemma_propose import propose_migrations
from depexit.llm import LLMError
from depexit.models import (
    FAILED, OBSERVED, Dependency, Declaration, DiscoveryResult, RiskScanResult, RiskSignal,
    Usage, UsageResult,
)

SOURCE = "import toml\n\n\ndef load(path):\n    return toml.load(path)\n"


class FakeLLM:
    """Returns canned replies in order; an Exception instance is raised instead."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def ask(self, prompt, system=""):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def good(**over):
    data = {
        "replacement": "stdlib: tomllib",
        "rationale": "toml is unmaintained",
        "edits": [{"file": "cfg.py", "old_text": "import toml", "new_text": "import tomllib"}],
        "requirements_change": {"remove": ["toml==0.10.2"], "add": []},
        "suggested_tests": ["load a sample file"],
        "expected_gaps": ["tomllib is read-only"],
    }
    data.update(over)
    return json.dumps(data)


@pytest.fixture
def world(tmp_path):
    (tmp_path / "cfg.py").write_text(SOURCE, encoding="utf-8")
    discovery = DiscoveryResult(str(tmp_path), [Dependency("toml", [Declaration("requirements.txt", 1, "==0.10.2")])], [])
    risk = RiskScanResult(
        signals=[RiskSignal("stale-release", "last release 6 years ago", "pypi:toml", OBSERVED, "toml")],
        baseline=None, install_runs=[], module_map={"toml": ["toml"]}, flagged=["toml"])
    usages = UsageResult({"toml": [Usage("cfg.py", 1, "toml", SOURCE, "toml", "import")]})
    return tmp_path, discovery, risk, usages


def run(world, llm):
    repo, discovery, risk, usages = world
    return propose_migrations(llm, repo, discovery, risk, usages)


def test_good_reply_becomes_a_tagged_proposal(world):
    result = run(world, FakeLLM(good()))
    p = result.proposals[0]
    assert result.status.status == "ok" and p.error is None
    assert p.tag == "AI-PROPOSED" and p.verdict == "unrehearsed"
    assert [e.new_text for e in p.edits] == ["import tomllib"]
    assert p.requirements_change.remove == ["toml==0.10.2"]


def test_prompt_contains_real_evidence_and_no_replacement_table(world):
    llm = FakeLLM(good())
    run(world, llm)
    prompt = llm.prompts[0]
    assert "cfg.py:1" in prompt and "toml.load(path)" in prompt and "requirements.txt:1 toml==0.10.2" in prompt
    assert "last release 6 years ago" in prompt


def test_reply_with_text_around_json_and_fence_is_accepted(world):
    result = run(world, FakeLLM("Sure!\n```json\n" + good() + "\n```\nDone."))
    assert result.proposals[0].error is None


def test_malformed_reply_gets_exactly_one_retry_with_the_error(world):
    llm = FakeLLM("not json at all", good())
    result = run(world, llm)
    assert result.proposals[0].error is None
    assert len(llm.prompts) == 2 and "rejected" in llm.prompts[1] and "no JSON object" in llm.prompts[1]


def test_two_bad_replies_record_an_error_not_canned_steps(world):
    llm = FakeLLM("nope", json.dumps({"replacement": None}))
    result = run(world, llm)
    p = result.proposals[0]
    assert len(llm.prompts) == 2
    assert p.error and "missing required keys" in p.error
    assert p.edits == [] and p.suggested_tests == [] and p.replacement is None
    assert result.status.status == FAILED and "toml" in result.status.error


def test_hallucinated_edit_is_dropped_and_logged(world):
    bad = {"file": "cfg.py", "old_text": "import tomli_w", "new_text": "import x"}
    ghost = {"file": "missing.py", "old_text": "a", "new_text": "b"}
    keep = {"file": "cfg.py", "old_text": "import toml", "new_text": "import tomllib"}
    p = run(world, FakeLLM(good(edits=[bad, keep, ghost]))).proposals[0]
    assert [e.old_text for e in p.edits] == ["import toml"]
    reasons = {d["edit"]["file"]: d["reason"] for d in p.dropped_edits}
    assert "does not occur" in reasons["cfg.py"] and "does not exist" in reasons["missing.py"]


def test_ambiguous_and_escaping_edits_are_dropped(world):
    ambiguous = {"file": "cfg.py", "old_text": "toml", "new_text": "tomllib"}
    escaping = {"file": "../x.py", "old_text": "a", "new_text": "b"}
    p = run(world, FakeLLM(good(edits=[ambiguous, escaping]))).proposals[0]
    assert p.edits == [] and len(p.dropped_edits) == 2


def test_unreachable_model_fails_stage_and_stops_calling(world):
    repo, discovery, risk, usages = world
    risk.flagged = ["toml", "idna"]
    llm = FakeLLM(LLMError("Cannot reach ollama at http://localhost:11434/api/chat"))
    result = propose_migrations(llm, repo, discovery, risk, usages)
    assert len(llm.prompts) == 1
    assert result.status.status == FAILED and "Cannot reach ollama" in result.status.error
    assert [p.dependency for p in result.proposals] == ["toml", "idna"]
    assert "not attempted" in result.proposals[1].error


def test_no_flagged_dependencies_is_ok_with_no_proposals(world):
    repo, discovery, risk, usages = world
    risk.flagged = []
    result = propose_migrations(FakeLLM(), repo, discovery, risk, usages)
    assert result.proposals == [] and result.status.status == "ok"
