import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from depexit.models import (
    Proposal, Edit, RequirementsChange, RunResult,
    DiscoveryResult
)
from depexit.rehearsal import apply_requirements_change, rehearse

def test_apply_requirements_change(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    req_file = workdir / "requirements.txt"
    req_file.write_text("requests==2.25.1\nflask==2.0.1\n# a comment\n-r other.txt\n", encoding="utf-8")

    change = RequirementsChange(remove=["requests", "Flask"], add=["httpx"])
    apply_requirements_change(workdir, change)

    content = req_file.read_text(encoding="utf-8")
    assert "flask==2.0.1" not in content
    assert "requests==2.25.1" not in content
    assert "# a comment" in content
    assert "-r other.txt" in content
    assert "httpx" in content

def test_rehearse_verdicts_refuted(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "file.py").write_text("print('hello')")
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    baseline = RunResult(label="baseline", command=[], exit_code=0, tests={"t1": "passed"})
    p = Proposal(dependency="dep1", edits=[Edit("file.py", "hello", "world")])
    mock_run = RunResult(label="dep1", command=[], exit_code=1, tests={"t1": "failed"})

    with patch("depexit.rehearsal.create_env", return_value=(MagicMock(), RunResult(label="venv", command=[], exit_code=0))), \
         patch("depexit.rehearsal.pip_install", return_value=(RunResult(label="pip", command=[], exit_code=0), "")), \
         patch("depexit.rehearsal.run_tests") as mock_tests, \
         patch("depexit.rehearsal.get_coverage_data", return_value={}), \
         patch("depexit.rehearsal.discover", return_value=DiscoveryResult(repo=str(repo), dependencies=[], conflicts=[])):

        mock_tests.return_value.result = mock_run
        res = rehearse(repo, DiscoveryResult(repo=str(repo), dependencies=[], conflicts=[]), baseline, [p], output_dir)

        assert p.verdict == "refuted"
        assert "REFUTED" in p.tag

def test_rehearse_verdicts_unproven_no_coverage(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "file.py").write_text("print('hello')")
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    baseline = RunResult(label="baseline", command=[], exit_code=0, tests={"t1": "passed"})
    p = Proposal(dependency="dep1", edits=[Edit("file.py", "hello", "world")])
    mock_run = RunResult(label="dep1", command=[], exit_code=0, tests={"t1": "passed"})

    with patch("depexit.rehearsal.create_env", return_value=(MagicMock(), RunResult(label="venv", command=[], exit_code=0))), \
         patch("depexit.rehearsal.pip_install", return_value=(RunResult(label="pip", command=[], exit_code=0), "")), \
         patch("depexit.rehearsal.run_tests") as mock_tests, \
         patch("depexit.rehearsal.get_coverage_data", return_value={}), \
         patch("depexit.rehearsal.discover", return_value=DiscoveryResult(repo=str(repo), dependencies=[], conflicts=[])):

        mock_tests.return_value.result = mock_run
        res = rehearse(repo, DiscoveryResult(repo=str(repo), dependencies=[], conflicts=[]), baseline, [p], output_dir)

        assert p.verdict == "unproven"
        assert any("edit not executed" in r for r in p.verdict_reasons)

def test_rehearse_verdicts_verified(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "file.py").write_text("print('hello')")
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    baseline = RunResult(label="baseline", command=[], exit_code=0, tests={"t1": "passed"})
    p = Proposal(dependency="dep1", edits=[Edit("file.py", "hello", "world")])
    mock_run = RunResult(label="dep1", command=[], exit_code=0, tests={"t1": "passed"})

    with patch("depexit.rehearsal.create_env", return_value=(MagicMock(), RunResult(label="venv", command=[], exit_code=0))), \
         patch("depexit.rehearsal.pip_install", return_value=(RunResult(label="pip", command=[], exit_code=0), "")), \
         patch("depexit.rehearsal.run_tests") as mock_tests, \
         patch("depexit.rehearsal.get_coverage_data") as mock_cov, \
         patch("depexit.rehearsal.discover", return_value=DiscoveryResult(repo=str(repo), dependencies=[], conflicts=[])):

        mock_tests.return_value.result = mock_run
        # We need a real path for coverage to match. Since we don't mock copy_repo,
        # the actual sandbox path is used. We'll just return a dict that matches any path.
        def side_effect(env, cov_file, workdir):
            return {
                "files": {
                    str((workdir / "file.py").resolve()): {"executed_lines": [1], "missing_lines": []}
                }
            }
        mock_cov.side_effect = side_effect

        res = rehearse(repo, DiscoveryResult(repo=str(repo), dependencies=[], conflicts=[]), baseline, [p], output_dir)

        assert p.verdict == "verified"

def test_rehearse_combined_patch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "file1.py").write_text("line1\nline2\n")
    (repo / "file2.py").write_text("start\nend\n")
    output_dir = tmp_path / "output"
    output_dir.mkdir()

    p1 = Proposal(dependency="dep1", edits=[Edit("file1.py", "line1", "changed1")])
    p2 = Proposal(dependency="dep2", edits=[Edit("file2.py", "start", "changed2")])

    with patch("depexit.rehearsal.create_env", return_value=(MagicMock(), RunResult(label="venv", command=[], exit_code=0))), \
         patch("depexit.rehearsal.pip_install", return_value=(RunResult(label="pip", command=[], exit_code=0), "")), \
         patch("depexit.rehearsal.run_tests") as mock_tests, \
         patch("depexit.rehearsal.get_coverage_data", return_value={}), \
         patch("depexit.rehearsal.discover", return_value=DiscoveryResult(repo=str(repo), dependencies=[], conflicts=[])):

        mock_tests.return_value.result = RunResult(label="combined", command=[], exit_code=0, tests={})
        res = rehearse(repo, DiscoveryResult(repo=str(repo), dependencies=[], conflicts=[]), None, [p1, p2], output_dir)

        assert res.patch_path is not None
        patch_content = Path(res.patch_path).read_text()
        assert "changed1" in patch_content
        assert "changed2" in patch_content
