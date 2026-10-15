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
