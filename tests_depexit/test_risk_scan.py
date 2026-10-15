from datetime import datetime, timezone

from depexit import risk_scan
from depexit.models import Declaration, Dependency, DiscoveryResult, OBSERVED, RunResult, VERIFIED
from depexit.risk_scan import (
    PypiError, attribute_text, metadata_signals, observed_signals, scan_risks, undeclared_signals,
)
from depexit.testrun import Failure, TestRun

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)
SBX = "W:\\sbx\\"


def dep(name, spec):
    return Dependency(name, [Declaration("requirements.txt", 1, spec)])


def pypi(version="3.1.6", released="2024-01-01T00:00:00", classifiers=(), requires_python="", releases=None):
    all_releases = {version: [{"upload_time_iso_8601": released + "Z", "yanked": False}]}
    all_releases.update(releases or {})
    return {"info": {"version": version, "classifiers": list(classifiers), "requires_python": requires_python},
            "releases": all_releases, "urls": []}


def kinds(signals):
    return {s.kind for s in signals}


def test_old_latest_release_is_stale_and_recent_one_is_not():
    old = metadata_signals(dep("toml", "==0.10.2"), pypi("0.10.2", "2020-11-01T00:00:00"), 2.0, NOW)
    assert "stale-release" in kinds(old) and all(s.tag == OBSERVED and s.source == "pypi:toml" for s in old)
    recent = metadata_signals(dep("x", ""), pypi("1.0", "2026-06-01T00:00:00"), 2.0, NOW)
    assert "stale-release" not in kinds(recent)


def test_pinned_major_version_behind_latest_is_flagged_but_minor_is_not():
    assert "version-lag" in kinds(metadata_signals(dep("pydantic", "==1.10.13"), pypi("2.14.1"), 2.0, NOW))
    assert "version-lag" not in kinds(metadata_signals(dep("requests", "==2.31.0"), pypi("2.32.3"), 2.0, NOW))
    assert "version-lag" not in kinds(metadata_signals(dep("click", ">=7"), pypi("8.1.0"), 2.0, NOW))  # not pinned


def test_yanked_pinned_version_inactive_status_and_python_requires():
    data = pypi("2.0", releases={"1.0": [{"yanked": True}]},
                classifiers=["Development Status :: 7 - Inactive"], requires_python=">=99")
    assert {"yanked", "inactive", "python-requires"} <= kinds(metadata_signals(dep("p", "==1.0"), data, 2.0, NOW))


def test_classifiers_older_than_the_running_python_are_outdated():
    data = pypi(classifiers=["Programming Language :: Python :: 3.8", "Programming Language :: Python :: 3.9"])
    assert "outdated-classifiers" in kinds(metadata_signals(dep("x", ""), data, 2.0, NOW))


def test_pypi_failure_is_a_visible_signal_not_a_silent_skip(tmp_path, monkeypatch):
    def offline(name):
        raise PypiError("cannot reach https://pypi.org: offline")

    monkeypatch.setattr(risk_scan, "create_env", lambda *a, **k: (None, RunResult("create-venv", [], 1, stdout_tail="no venv")))
    (tmp_path / "m.py").write_text("x = 1\n", encoding="utf-8")
    result = scan_risks(tmp_path, DiscoveryResult(str(tmp_path), [dep("toml", "==0.10.2")], []), fetch=offline)
    unavailable = [s for s in result.signals if s.kind == "pypi-unavailable"]
    assert unavailable and "offline" in unavailable[0].detail and unavailable[0].dependency == "toml"
    assert any(s.kind == "install-failure" for s in result.signals)  # a venv failure is visible too
    assert "toml" not in result.flagged  # an unreachable PyPI is not evidence against the package


MODULE_MAP = {"jinja2": ["jinja2"], "markupsafe": ["markupsafe"], "pkg_resources": ["setuptools"], "yaml": ["pyyaml"]}


def test_failure_is_blamed_on_the_importing_package_not_the_module_named_in_the_error():
    text = ("tests\\test_report.py:5: in test\n"
            "D:\\v\\Lib\\site-packages\\jinja2\\utils.py:642: in <module>\n"
            "E   ImportError: cannot import name 'soft_unicode' from 'markupsafe' "
            "(D:\\v\\Lib\\site-packages\\markupsafe\\__init__.py)")
    assert attribute_text(text, MODULE_MAP, {"jinja2"}, last_frame=True) == "jinja2"


def test_import_error_without_library_frames_falls_back_to_the_named_module():
    assert attribute_text("E   ModuleNotFoundError: No module named 'yaml'", MODULE_MAP, {"pyyaml"}) == "pyyaml"
    assert attribute_text("something unrelated happened", MODULE_MAP, {"jinja2"}) is None


def test_warnings_and_failures_become_observed_signals_with_sources_and_redacted_paths():
    run = TestRun(
        result=RunResult("baseline", [], 1),
        failures=[Failure("tests.test_report::test_x", "ImportError: boom",
                          SBX + "repo\\app.py:3: in f\n" + SBX + "venv\\Lib\\site-packages\\jinja2\\x.py:9: in g\nE   ImportError: boom")],
        warning_blocks=[("app\\res.py:1", SBX + "venv\\Lib\\site-packages\\pkg_resources\\__init__.py:1: "
                                         "DeprecationWarning: pkg_resources is deprecated")],
    )
    sigs = observed_signals(run, MODULE_MAP, {"jinja2", "setuptools"}, {SBX + "repo\\": "", SBX + "venv": "<venv>"})
    by_kind = {s.kind: s for s in sigs}
    assert by_kind["test-failure"].dependency == "jinja2"
    assert by_kind["test-failure"].source == "pytest:tests.test_report::test_x"
    assert by_kind["test-warning"].dependency == "setuptools"
    assert all(s.tag == OBSERVED and SBX not in s.detail + s.source for s in sigs)


def test_imports_that_no_manifest_declares_are_flagged_and_stdlib_and_local_are_not(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "net.py").write_text("import os\nimport certifi\nimport pkg.util\nimport yaml\nfrom . import x\n",
                                             encoding="utf-8")
    (tmp_path / "broken.py").write_text("def (:\n", encoding="utf-8")
    notes = []
    sigs = undeclared_signals(tmp_path, {"pyyaml"}, {"certifi": ["certifi"], "yaml": ["pyyaml"]}, notes)
    assert [(s.dependency, s.kind, s.tag) for s in sigs] == [("certifi", "undeclared-import", VERIFIED)]
    assert "pkg/net.py" in sigs[0].detail and any("broken.py" in n for n in notes)


def test_an_unmapped_undeclared_module_is_reported_with_the_reason(tmp_path):
    (tmp_path / "a.py").write_text("import mystery_pkg\n", encoding="utf-8")
    sigs = undeclared_signals(tmp_path, set(), {}, [])
    assert sigs[0].dependency == "mystery-pkg" and "not installed" in sigs[0].detail
