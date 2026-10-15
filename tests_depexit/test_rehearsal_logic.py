from pathlib import Path

from depexit.models import (
    Comparison, Edit, EditCoverage, Proposal, RequirementsChange, RunResult,
)
from depexit.rehearsal import (
    apply_requirements_change, compare_runs, decide_verdict, edit_executed, make_patch,
)


def run_result(tests, warnings=()):
    return RunResult(label="x", command=[], exit_code=0, tests=dict(tests), warnings=list(warnings))


def test_remove_matches_on_name_when_given_a_full_requirement_string(tmp_path):
    (tmp_path / "requirements.txt").write_text("toml==0.10.2\nrequests>=2\n# keep me\n", encoding="utf-8")
    result = apply_requirements_change(tmp_path, RequirementsChange(remove=["Toml==0.10.2"]))
    text = (tmp_path / "requirements.txt").read_text(encoding="utf-8")
    assert "toml" not in text and "requests>=2" in text and "# keep me" in text
    assert result.unmatched_removes == []


def test_added_requirements_keep_their_version_numbers(tmp_path):
    (tmp_path / "requirements.txt").write_text("a==1\n", encoding="utf-8")
    apply_requirements_change(tmp_path, RequirementsChange(add=["pydantic>=2.7.0", "Some_Pkg==1.2.3"]))
    text = (tmp_path / "requirements.txt").read_text(encoding="utf-8")
    assert "pydantic>=2.7.0" in text and "Some_Pkg==1.2.3" in text


def test_add_is_not_duplicated_when_the_name_is_already_declared(tmp_path):
    (tmp_path / "requirements.txt").write_text("pydantic>=2\n", encoding="utf-8")
    apply_requirements_change(tmp_path, RequirementsChange(add=["pydantic>=2.7"]))
    assert (tmp_path / "requirements.txt").read_text(encoding="utf-8").count("pydantic") == 1


def test_unmatched_remove_and_unwritable_add_are_reported(tmp_path):
    result = apply_requirements_change(tmp_path, RequirementsChange(remove=["ghost"], add=["new>=1"]))
    assert result.unmatched_removes == ["ghost"] and result.unwritten_adds == ["new>=1"]


def test_pyproject_dependency_strings_are_removed_by_name(tmp_path):
    (tmp_path / "requirements.txt").write_text("x\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = [\n    "toml==0.10.2",\n    "rich",\n]\n', encoding="utf-8")
    apply_requirements_change(tmp_path, RequirementsChange(remove=["toml"]))
    text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert "toml" not in text and '"rich"' in text


def test_edit_coverage_matches_relative_coverage_paths(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x", encoding="utf-8")
    cov = {"files": {str(Path("pkg") / "mod.py"): {"executed_lines": [1, 2, 5], "missing_lines": [8, 9]}}}
    assert edit_executed(cov, tmp_path, "pkg/mod.py", 2, 3) == (True, "executed")
    ok, reason = edit_executed(cov, tmp_path, "pkg/mod.py", 8, 9)
    assert not ok and "never executed" in reason
    ok, reason = edit_executed(cov, tmp_path, "pkg/mod.py", 6, 7)
    assert not ok and "no executable statements" in reason
    ok, reason = edit_executed(cov, tmp_path, "other.py", 1, 1)
    assert not ok and "never imported" in reason


def test_a_pure_deletion_is_never_counted_as_executed(tmp_path):
    cov = {"files": {"a.py": {"executed_lines": [1], "missing_lines": []}}}
    assert edit_executed(cov, tmp_path, "a.py", 1, 1, deletion=True)[0] is False


def test_compare_runs_finds_new_failures_missing_tests_and_warning_changes():
    base = run_result({"a": "passed", "b": "passed", "c": "failed", "d": "passed"}, ["w1", "w2"])
    cand = run_result({"a": "passed", "b": "failed", "c": "passed"}, ["w2", "w3"])
    failures, passing, added, removed = compare_runs(base, cand)
    assert sorted(failures) == ["b", "d"] and passing == ["c"] and added == ["w3"] and removed == ["w1"]
    assert compare_runs(None, cand) == ([], [], [], [])


def comparison(**kw):
    return Comparison(label="p", run=run_result({"a": "passed"}), **kw)


def test_verdict_rules():
    base = run_result({"a": "passed"})
    covered = [EditCoverage("p", "f.py", 1, 1, True, "executed")]
    untouched = [EditCoverage("p", "f.py", 1, 1, False, "the file was never imported by the tests")]

    p = Proposal("p", edits=[Edit("f.py", "a", "b")])
    decide_verdict(p, comparison(edit_coverage=covered), base, 1)
    assert p.verdict == "verified" and p.tag == "AI-PROPOSED"

    p = Proposal("p", edits=[Edit("f.py", "a", "b")])
    decide_verdict(p, comparison(edit_coverage=untouched), base, 1)
    assert p.verdict == "unproven" and "edit not executed" in p.verdict_reasons[0]

    p = Proposal("p", edits=[Edit("f.py", "a", "b")])
    decide_verdict(p, comparison(new_failures=["a"], edit_coverage=covered), base, 1)
    assert p.verdict == "refuted" and p.tag == "REFUTED" and "a" in p.verdict_reasons[0]

    p = Proposal("p", edits=[Edit("f.py", "a", "b")])
    decide_verdict(p, comparison(install_ok=False), base, 1)
    assert p.verdict == "refuted" and "install failed" in p.verdict_reasons[0]

    p = Proposal("p", edits=[Edit("f.py", "a", "b")])
    decide_verdict(p, comparison(edit_coverage=covered), run_result({}), 1)
    assert p.verdict == "unproven" and "no tests" in p.verdict_reasons[0]

    p = Proposal("p", edits=[Edit("f.py", "a", "b")])
    decide_verdict(p, comparison(), base, 0)
    assert p.verdict == "unproven" and "could be applied" in p.verdict_reasons[0]

    p = Proposal("p", requirements_change=RequirementsChange(remove=["x"]))
    decide_verdict(p, comparison(), base, 0)
    assert p.verdict == "verified"  # a manifest-only change whose tests still pass


def test_patch_is_written_even_when_the_output_dir_does_not_exist(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("import toml\nx = toml.load(p)\n", encoding="utf-8")
    (repo / "requirements.txt").write_text("toml==0.10.2\n", encoding="utf-8")
    p = Proposal("toml", edits=[Edit("m.py", "import toml", "import tomllib")],
                 requirements_change=RequirementsChange(remove=["toml==0.10.2"], add=["tomlkit>=0.12.0"]))
    out = tmp_path / "not" / "yet"
    path = make_patch(repo, [p], out)
    patch = Path(path).read_text(encoding="utf-8")
    assert "-import toml" in patch and "+import tomllib" in patch
    assert "-toml==0.10.2" in patch and "+tomlkit>=0.12.0" in patch
    assert (repo / "m.py").read_text(encoding="utf-8").startswith("import toml")  # original untouched


def test_patch_is_none_when_nothing_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "m.py").write_text("x = 1\n", encoding="utf-8")
    assert make_patch(repo, [Proposal("p", edits=[Edit("m.py", "nope", "x")])], tmp_path / "out") is None


def test_a_suite_that_cannot_run_is_refuted_with_the_real_error_line():
    base = run_result({"a": "passed", "b": "passed"})
    crashed = RunResult("p", [], 4, tests={}, stdout_tail="...\nE   NameError: name 'validator' is not defined\n")
    p = Proposal("p", edits=[Edit("f.py", "a", "b")])
    decide_verdict(p, Comparison(label="p", run=crashed, new_failures=["a", "b"]), base, 1)
    assert p.verdict == "refuted" and p.tag == "REFUTED"
    assert "could not run" in p.verdict_reasons[0] and "validator" in p.verdict_reasons[0]


def test_verified_reason_mentions_tests_that_the_change_fixed():
    base = run_result({"a": "passed", "b": "failed"})
    comp = Comparison(label="p", run=run_result({"a": "passed", "b": "passed"}), newly_passing=["b"])
    p = Proposal("p", requirements_change=RequirementsChange(add=["x>=2"]))
    decide_verdict(p, comp, base, 0)
    assert p.verdict == "verified" and "now pass: b" in p.verdict_reasons[0]


def test_manifest_files_are_recognised():
    from depexit.rehearsal import is_manifest
    assert is_manifest("requirements.txt") and is_manifest("sub/requirements-dev.txt") and is_manifest("pyproject.toml")
    assert not is_manifest("app/config.py") and not is_manifest("templates/report.j2")
