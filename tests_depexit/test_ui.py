import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

import ui

REAL_LOG = r"""GemmaMorph: repo=X  model=gemma4:e2b (ollama @ http://localhost:11434)
--- S1 discover
    (0.0s)
--- S2 risk_scan
--- S3 usage

[ok     ] S1 discover   14 dependencies, 1 conflicts
[ok     ] S2 risk_scan  14 signals, 10 flagged dependencies, baseline 9 passed/1 failed
[failed ] S3 usage      ModuleNotFoundError: No module named 'rich'
[skipped] S4 propose    skipped: S3 usage did not complete (failed)
output: D:\out\EXIT_CAPSULE.html
exit code 1 (failed: S3 usage)
"""


def test_parse_log_reports_real_states_and_leaves_unknown_stages_pending():
    info = ui.parse_log(REAL_LOG)
    states = {s["name"]: (s["state"], s["detail"]) for s in info["stages"]}
    assert states["S1 discover"][0] == "ok" and "14 dependencies" in states["S1 discover"][1]
    assert states["S3 usage"] == ("failed", "ModuleNotFoundError: No module named 'rich'")
    assert states["S4 propose"][0] == "skipped"
    assert states["S5 rehearse"][0] == "pending" and states["S6 capsule"][0] == "pending"
    assert info["outputs"] == [r"D:\out\EXIT_CAPSULE.html"] and info["exit_line"].startswith("exit code 1")


def test_parse_log_marks_the_running_stage():
    info = ui.parse_log("--- S1 discover\n    (0.0s)\n--- S2 risk_scan\n")
    states = {s["name"]: s["state"] for s in info["stages"]}
    assert states["S1 discover"] == "running" and states["S2 risk_scan"] == "running"
    assert info["current"] == "S2 risk_scan"


def test_build_command_uses_an_argument_list_with_only_given_options(tmp_path):
    cmd = ui.build_command("D:/repo with space", tmp_path / "o", {"backend": "gemini", "model": "", "skip_rehearsal": "1"})
    assert cmd[0] == sys.executable and "D:/repo with space" in cmd
    assert cmd[cmd.index("--backend") + 1] == "gemini" and "--model" not in cmd and "--skip-rehearsal" in cmd


@pytest.fixture
def server(tmp_path):
    def fake_command(repo, out_dir, form):
        script = (
            "import sys,pathlib;out=pathlib.Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True);"
            "print('--- S1 discover');print('[ok     ] S1 discover   3 dependencies, 0 conflicts');"
            "(out/'EXIT_CAPSULE.html').write_text('<h1>capsule</h1>');(out/'secret.txt').write_text('nope');"
            "print('output: '+str(out/'EXIT_CAPSULE.html'));print('exit code 0')")
        return [sys.executable, "-c", script, str(out_dir)]

    ui.RUNS.clear()
    httpd = ui.serve("127.0.0.1", 0, tmp_path / "runs", fake_command)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
    httpd.shutdown()
    httpd.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers


def post(url, data):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=10) as r:
            return r.status, r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.headers


def test_index_page_renders(server):
    base, _ = server
    status, body, _ = get(base + "/")
    assert status == 200 and "Run GemmaMorph" in body and "AI-PROPOSED" in body


def test_start_run_shows_progress_and_serves_only_whitelisted_files(server):
    base, tmp = server
    repo = tmp / "target"
    repo.mkdir()
    status, headers = post(base + "/run", {"repo": str(repo)})
    assert status == 303
    location = headers["Location"]
    run_id = location.rsplit("/", 1)[1]
    for _ in range(100):
        _, body, _ = get(f"{base}/api/run/{run_id}")
        data = json.loads(body)
        if data["state"] != "running":
            break
        time.sleep(0.1)
    assert data["state"] == "finished" and data["returncode"] == 0
    assert data["stages"][0]["state"] == "ok" and "EXIT_CAPSULE.html" in data["files"]
    assert get(base + location)[0] == 200
    status, body, headers = get(f"{base}/files/{run_id}/EXIT_CAPSULE.html")
    assert status == 200 and "<h1>capsule</h1>" in body and headers["X-Content-Type-Options"] == "nosniff"
    assert get(f"{base}/files/{run_id}/secret.txt")[0] == 404            # not on the whitelist
    assert get(f"{base}/files/{run_id}/..%2F..%2Frun.log")[0] == 404     # no traversal
    assert get(f"{base}/files/nonexistent/EXIT_CAPSULE.html")[0] == 404
    assert get(f"{base}/api/run/not-a-run")[0] == 404


def test_missing_repo_is_rejected_without_starting_anything(server):
    base, tmp = server
    status, _ = post(base + "/run", {"repo": str(tmp / "nope")})
    assert status == 400 and ui.RUNS == {}


def test_untrusted_text_is_escaped_on_the_run_page(server):
    base, tmp = server
    odd = tmp / "x&y'z"
    odd.mkdir()
    _, headers = post(base + "/run", {"repo": str(odd)})
    _, body, _ = get(base + headers["Location"])
    assert "x&amp;y&#x27;z" in body and "x&y'z" not in body


def test_quick_mode_adds_skip_rehearsal_and_full_mode_does_not(tmp_path):
    quick = ui.build_command("repo", tmp_path, {"mode": "quick"})
    full = ui.build_command("repo", tmp_path, {"mode": "full"})
    assert "--skip-rehearsal" in quick and "--skip-rehearsal" not in full


def test_check_path_describes_a_folder(tmp_path):
    assert ui.check_path("")["state"] == "empty"
    assert ui.check_path(str(tmp_path / "nope"))["state"] == "missing"
    assert ui.check_path(str(tmp_path))["state"] == "weak"
    (tmp_path / "requirements.txt").write_text("x\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "skip.py").write_text("x", encoding="utf-8")
    result = ui.check_path(f'"{tmp_path}"')  # quotes pasted from Explorer are tolerated
    assert result == {"state": "ok", "manifests": ["requirements.txt"], "python_files": 1}


def test_health_reports_an_unreachable_model_server_without_raising():
    result = ui.health("http://127.0.0.1:1")
    assert result["ollama"] is False and result["gemma4"] == [] and result["error"]


def test_capsule_stats_orders_findings_and_counts_verdicts(tmp_path):
    capsule = {
        "dependencies": [{}, {}], "conflicts": [], "usages": {"a": [1, 2], "b": [3]}, "model": {"name": "gemma4:e2b"},
        "signals": [
            {"kind": "stale-release", "dependency": "toml", "detail": "old", "tag": "OBSERVED", "source": "pypi:toml"},
            {"kind": "test-failure", "dependency": "jinja2", "detail": "boom", "tag": "OBSERVED", "source": "pytest:t"},
        ],
        "proposals": [{"dependency": "x", "verdict": "refuted", "tag": "REFUTED", "verdict_reasons": ["why"]},
                      {"dependency": "y", "error": "no model", "tag": "AI-PROPOSED"}],
    }
    (tmp_path / "EXIT_CAPSULE.json").write_text(json.dumps(capsule), encoding="utf-8")
    stats = ui.capsule_stats(tmp_path)
    assert [f["kind"] for f in stats["findings"]] == ["test-failure", "stale-release"]
    assert stats["counts"] == {"refuted": 1, "no proposal": 1} and stats["usages"] == 3 and stats["flagged"] == 2
    assert ui.capsule_stats(tmp_path / "missing") is None


def test_api_routes_health_check_and_stop(server):
    base, tmp = server
    assert "ollama" in json.loads(get(base + "/api/health")[1])
    (tmp / "requirements.txt").write_text("x\n", encoding="utf-8")
    assert json.loads(get(base + "/api/check?path=" + urllib.parse.quote(str(tmp)))[1])["manifests"] == ["requirements.txt"]
    assert get(base + "/api/stop/unknown")[0] == 404
    status, _ = post(base + "/api/stop/unknown", {})
    assert status == 404


def test_a_running_pipeline_can_be_stopped(tmp_path):
    ui.RUNS.clear()
    run = ui.start_run({"repo": str(tmp_path)}, tmp_path / "runs",
                       lambda repo, out, form: [sys.executable, "-c", "import time;print('--- S1 discover',flush=True);time.sleep(60)"])
    time.sleep(0.5)
    assert ui.run_state(run)["state"] == "running"
    assert ui.stop_run(run) is True
    for _ in range(50):
        if run.returncode is not None:
            break
        time.sleep(0.1)
    state = ui.run_state(run)
    assert state["state"] == "stopped" and state["elapsed"] < 30
    assert ui.stop_run(run) is False


def test_open_report_shows_a_finished_capsule_with_its_stage_statuses(tmp_path):
    ui.RUNS.clear()
    capsule = {"repo": "D:/proj", "dependencies": [], "conflicts": [], "usages": {}, "signals": [], "model": {},
               "stages": [{"stage": "discover", "status": "ok"}, {"stage": "propose", "status": "failed", "error": "model down"}],
               "proposals": [{"dependency": "x", "verdict": "verified", "tag": "AI-PROPOSED",
                              "edits": [{"file": "a.py", "old_text": "import a", "new_text": "import b"}],
                              "requirements_change": {"remove": ["a==1"], "add": ["b>=2"]}}]}
    (tmp_path / "EXIT_CAPSULE.json").write_text(json.dumps(capsule), encoding="utf-8")
    run = ui.open_report(f'"{tmp_path}"')
    state = ui.run_state(run)
    stages = {s["name"]: s for s in state["stages"]}
    assert run.repo == "D:/proj" and state["state"] == "failed"
    assert stages["S1 discover"]["state"] == "ok" and stages["S4 propose"]["detail"] == "model down"
    p = state["stats"]["proposals"][0]
    assert p["edits"][0]["new"] == "import b" and p["remove"] == ["a==1"] and p["add"] == ["b>=2"]
    with pytest.raises(ValueError):
        ui.open_report(str(tmp_path / "empty"))


def test_runs_from_an_earlier_session_are_restored(tmp_path):
    ui.RUNS.clear()
    d = tmp_path / "abcdef012345"
    d.mkdir()
    (d / "run.log").write_text("GemmaMorph: repo=D:/proj  model=gemma4:e2b (ollama @ x)\n[ok     ] S1 discover   1 dependencies, 0 conflicts\nexit code 0\n", encoding="utf-8")
    (tmp_path / "not-a-run").mkdir()
    ui.restore_runs(tmp_path)
    assert list(ui.RUNS) == ["abcdef012345"]
    state = ui.run_state(ui.RUNS["abcdef012345"])
    assert state["repo"] == "D:/proj" and state["state"] == "finished"


def test_page_scripts_are_valid_javascript(tmp_path):
    import re
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    run = ui.Run("abcdef012345", "D:/x", tmp_path, [])
    for name, page_bytes in (("index", ui.index_page(tmp_path)), ("run", ui.run_page(run))):
        scripts = re.findall(r"<script>(.*?)</script>", page_bytes.decode("utf-8"), re.S)
        assert scripts
        js = tmp_path / f"{name}.js"
        js.write_text("\n;\n".join(scripts), encoding="utf-8")
        result = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
        assert result.returncode == 0, f"{name} page: {result.stderr}"
