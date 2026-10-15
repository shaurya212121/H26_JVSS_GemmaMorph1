"""
Unit tests for Stage 3: depexit/usage_finder.py.

Tests cover:
- Standard imports (import x, from x import y)
- Aliased imports (import x as y, from x import y as z)
- Outermost attribute usages (nested chains do not produce duplicate inner records)
- Private API calls and imports (_name, non-dunder) vs dunder (__version__)
- Monkeypatching (assignment, augassign, delete, setattr, mock.patch string)
- Enclosing function and class scopes
- Relative imports (must be ignored)
- Unparsable files (syntax error recorded in notes without aborting)
- Unused dependencies (must be present in usages with empty list)
- Flagged dependency missing from module_map (recorded in notes)
- Directory exclusion (.git, venvs, __pycache__, node_modules, build, dist, .tox, .eggs)
- Deterministic sorting by (file, line)
- Copy-pasteable raw snippet generation

Runs 100% offline with NO external dependencies and NO Ollama.
"""

from pathlib import Path
import pytest

from depexit.models import UsageResult, VERIFIED
from depexit.usage_finder import find_usages


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "test_repo"
    repo.mkdir()

    # 1. Main app file with imports, aliases, attributes, private api, function scopes
    app_py = repo / "app.py"
    app_py.write_text(
        """import requests as req
from requests.utils import _parse_content_type_header
import toml

def my_func():
    val = req.get("https://example.com")
    ver = req.__version__
    priv = req._internal_cache
    toml.load("config.toml")
    return val

class Handler:
    def process(self):
        req.Session.get("https://example.com")
""",
        encoding="utf-8",
    )

    # 2. Monkeypatching file
    patch_py = repo / "patching.py"
    patch_py.write_text(
        """import requests
from unittest import mock

def apply_patches():
    requests.Session.request = lambda self: None
    requests.counter += 1
    del requests.old_attr
    setattr(requests, "get", lambda *a, **k: None)
    mock.patch("requests.Session.request")
""",
        encoding="utf-8",
    )

    # 3. Relative imports in a subpackage (must be ignored)
    pkg = repo / "subpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "rel.py").write_text(
        """from . import foo
from ..bar import baz
import requests
requests.post("http://test")
""",
        encoding="utf-8",
    )

    # 4. Unparsable file (syntax error)
    bad_py = repo / "broken.py"
    bad_py.write_text("def broken(::\n    pass", encoding="utf-8")

    # 5. Ignored directories with python files that must NOT be scanned
    for skip_dir in (".git", "venv", "venvs", ".venv", "node_modules", "__pycache__", "build", "dist", ".tox", ".eggs"):
        d = repo / skip_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / "ignored.py").write_text("import requests\nrequests.get()", encoding="utf-8")

    return repo


def test_find_usages_contract_and_detections(fixture_repo: Path):
    module_map = {
        "requests": ["requests"],
        "toml": ["toml"],
        "unused_pkg": ["unused-pkg"],
    }
    flagged = ["requests", "toml", "unused-pkg", "missing-in-map"]

    result = find_usages(fixture_repo, flagged, module_map)

    assert isinstance(result, UsageResult)

    # All flagged dependencies must be present as keys in result.usages
    assert set(result.usages.keys()) == {"requests", "toml", "unused-pkg", "missing-in-map"}

    # Unused dependency must have an empty list
    assert result.usages["unused-pkg"] == []
    assert result.usages["missing-in-map"] == []

    # missing-in-map should be noted in notes
    assert any("missing-in-map" in note for note in result.notes)

    # broken.py syntax error should be recorded in notes
    assert any("broken.py" in note and "syntax error" in note.lower() for note in result.notes)

    # Inspect requests usages
    req_usages = result.usages["requests"]
    assert len(req_usages) > 0

    # Ensure all usages have VERIFIED tag and posix relative file paths
    for u in req_usages:
        assert u.tag == VERIFIED
        assert not u.file.startswith(("\\", "/"))
        assert "\\" not in u.file
        assert u.line >= 1
        assert u.dependency == "requests"
        assert len(u.snippet) > 0

    # Verify sorting by (file, line)
    for i in range(len(req_usages) - 1):
        assert (req_usages[i].file, req_usages[i].line) <= (req_usages[i + 1].file, req_usages[i + 1].line)

    # Check specific kinds and scopes in app.py
    app_usages = [u for u in req_usages if u.file == "app.py"]
    kinds = {u.kind for u in app_usages}
    assert "import" in kinds
    assert "private-api" in kinds
    assert "attribute" in kinds

    # Import on line 1
    u_imp = [u for u in app_usages if u.line == 1][0]
    assert u_imp.kind == "import"
    assert u_imp.symbol == "requests"
    assert u_imp.function == ""

    # Private API import on line 2
    u_priv = [u for u in app_usages if u.line == 2][0]
    assert u_priv.kind == "private-api"
    assert u_priv.symbol == "requests.utils._parse_content_type_header"

    # Attribute use inside function on line 6 (req.get)
    u_attr = [u for u in app_usages if u.line == 6][0]
    assert u_attr.kind == "attribute"
    assert u_attr.symbol == "requests.get"
    assert u_attr.function == "my_func"

    # Dunder attribute on line 7 (req.__version__) is standard attribute, NOT private-api
    u_dunder = [u for u in app_usages if u.line == 7][0]
    assert u_dunder.kind == "attribute"
    assert u_dunder.symbol == "requests.__version__"

    # Private attribute on line 8 (req._internal_cache) starts with single underscore
    u_priv_attr = [u for u in app_usages if u.line == 8][0]
    assert u_priv_attr.kind == "private-api"
    assert u_priv_attr.symbol == "requests._internal_cache"

    # Attribute use inside class method on line 14 (req.Session.get)
    # Must record outermost chain requests.Session.get, not duplicate requests.Session
    u_cls = [u for u in app_usages if u.line == 14][0]
    assert u_cls.kind == "attribute"
    assert u_cls.symbol == "requests.Session.get"
    assert u_cls.function == "Handler.process"

    # Check monkeypatch detections in patching.py
    patch_usages = [u for u in req_usages if u.file == "patching.py"]
    patch_kinds = [u.kind for u in patch_usages]
    assert patch_kinds.count("monkeypatch") >= 4

    mp_symbols = [u.symbol for u in patch_usages if u.kind == "monkeypatch"]
    assert "requests.Session.request" in mp_symbols
    assert "requests.counter" in mp_symbols
    assert "requests.old_attr" in mp_symbols
    assert any("requests.get" in s for s in mp_symbols)

    # Check relative import handling in subpkg/rel.py
    rel_usages = [u for u in req_usages if u.file == "subpkg/rel.py"]
    # Only requests import and requests.post should be recorded; relative imports ignored
    assert len(rel_usages) == 2
    assert rel_usages[0].kind == "import"
    assert rel_usages[1].kind == "attribute"
    assert rel_usages[1].symbol == "requests.post"

    # Check toml usages
    toml_usages = result.usages["toml"]
    assert len(toml_usages) == 2  # import toml on line 3, toml.load on line 9
    assert toml_usages[0].kind == "import"
    assert toml_usages[0].symbol == "toml"
    assert toml_usages[1].kind == "attribute"
    assert toml_usages[1].symbol == "toml.load"
    assert toml_usages[1].function == "my_func"

    # Check that skipped directories did not produce any usages
    all_files = {u.file for us in result.usages.values() for u in us}
    for f in all_files:
        assert not any(part in f.split("/") for part in (
            ".git", "venv", "venvs", ".venv", "node_modules", "__pycache__", "build", "dist", ".tox", ".eggs"
        ))


def test_missing_repo_raises_instead_of_reporting_ok(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError, match="does not exist"):
        find_usages(tmp_path / "no-such-repo", ["requests"], {"requests": ["requests"]})
