import pytest

from depexit.edits import EditError, apply_edit, read_lf, resolve_in_repo, write_lf


def test_apply_edit_replaces_unique_text_and_reports_lines():
    text = "import a\nimport toml\nx = 1\n"
    result = apply_edit(text, "import toml", "import tomllib")
    assert result.text == "import a\nimport tomllib\nx = 1\n"
    assert (result.start_line, result.end_line) == (2, 2)


def test_multiline_new_text_line_range():
    result = apply_edit("a\nb\nc\n", "b\n", "b1\nb2\nb3\n")
    assert (result.start_line, result.end_line) == (2, 4)


def test_missing_old_text_is_rejected():
    with pytest.raises(EditError, match="does not occur"):
        apply_edit("import a\n", "import b", "import c")


def test_ambiguous_old_text_is_rejected():
    with pytest.raises(EditError, match="occurs 2 times"):
        apply_edit("x = 1\nx = 1\n", "x = 1", "x = 2")


def test_empty_and_noop_edits_are_rejected():
    with pytest.raises(EditError, match="empty"):
        apply_edit("abc", "", "x")
    with pytest.raises(EditError, match="identical"):
        apply_edit("abc", "abc", "abc")


def test_crlf_files_match_lf_old_text_and_keep_crlf(tmp_path):
    f = tmp_path / "m.py"
    f.write_bytes(b"import toml\r\nx = 1\r\n")
    text, crlf = read_lf(f)
    assert crlf and text == "import toml\nx = 1\n"
    write_lf(f, apply_edit(text, "import toml\nx = 1", "import tomllib\nx = 1").text, crlf)
    assert f.read_bytes() == b"import tomllib\r\nx = 1\r\n"


def test_resolve_in_repo_blocks_escape_and_missing(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.py").write_text("x", encoding="utf-8")
    (tmp_path / "outside.py").write_text("x", encoding="utf-8")
    assert resolve_in_repo(repo, "ok.py").name == "ok.py"
    with pytest.raises(EditError, match="outside"):
        resolve_in_repo(repo, "../outside.py")
    with pytest.raises(EditError, match="does not exist"):
        resolve_in_repo(repo, "nope.py")
