import run
from depexit.models import (
    OBSERVED, VERIFIED, Dependency, Declaration, DiscoveryResult, RiskScanResult, RiskSignal, Usage, UsageResult,
)


def make_risk():
    return RiskScanResult(signals=[RiskSignal("stale-release", "old", "pypi:toml", OBSERVED, "toml")],
                          baseline=None, install_runs=[], module_map={}, flagged=["toml"])


def test_private_api_and_monkeypatch_usages_become_verified_signals_and_flag_the_dependency():
    risk = make_risk()
    usage = UsageResult({
        "lib": [Usage("a.py", 5, "lib.utils._x", "", "lib", "private-api", "parse"),
                Usage("a.py", 9, "lib.get", "", "lib", "monkeypatch"),
                Usage("a.py", 1, "lib", "", "lib", "import")],
        "toml": [],
    })
    run.add_usage_signals(risk, usage)
    new = [s for s in risk.signals if s.dependency == "lib"]
    assert {s.kind for s in new} == {"private-api", "monkeypatch"}
    assert all(s.tag == VERIFIED and s.source.startswith("ast:a.py:") for s in new)
    assert risk.flagged == ["lib", "toml"]
    run.add_usage_signals(risk, usage)  # idempotent
    assert len([s for s in risk.signals if s.dependency == "lib"]) == 2


def test_plain_imports_do_not_create_signals():
    risk = make_risk()
    run.add_usage_signals(risk, UsageResult({"lib": [Usage("a.py", 1, "lib", "", "lib", "import")]}))
    assert len(risk.signals) == 1 and risk.flagged == ["toml"]


def test_exit_code_is_nonzero_when_a_stage_fails_and_later_stages_are_skipped(tmp_path, monkeypatch, capsys):
    import depexit.discover
    from depexit.llm import LLMError

    class UnreachableLLM:
        def __init__(self, config):
            pass

        def ask(self, prompt, system=""):
            raise LLMError("fake: model unreachable")

    def boom(repo):
        raise RuntimeError("cannot read manifests")

    monkeypatch.setattr(depexit.discover, "discover", boom)
    monkeypatch.setattr(run, "LLMClient", UnreachableLLM)
    code = run.main([str(tmp_path), "--output", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert code == 1
    assert "[failed ] S1 discover" in out and "RuntimeError: cannot read manifests" in out
    assert "[skipped] S2 risk_scan" in out and "S1 discover did not complete" in out
    assert "exit code 1" in out


def test_bad_arguments_exit_with_2(tmp_path, capsys):
    assert run.main([str(tmp_path / "missing")]) == 2
    assert run.main([]) == 2
