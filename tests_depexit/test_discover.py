from depexit.discover import discover, find_conflicts, normalize_name, specs_compatible
from depexit.models import Declaration, Dependency


def test_names_that_differ_only_in_case_and_separators_are_one_package():
    assert {normalize_name(n) for n in ["Jinja2", "jinja2", "JINJA2"]} == {"jinja2"}
    assert normalize_name("Python_Dateutil") == normalize_name("python.dateutil") == "python-dateutil"
    assert normalize_name("zope--interface") == "zope-interface"


def write(tmp_path, name, text):
    (tmp_path / name).write_text(text, encoding="utf-8")


def test_requirements_are_parsed_with_file_and_line(tmp_path):
    write(tmp_path, "requirements.txt",
          "# header\nJinja2==2.11.3  # pinned\n\n-r more.txt\n--index-url https://x/simple\nrequests>=2,<3\nsix\n")
    write(tmp_path, "more.txt", "Python_Dateutil>=2.8\n")
    result = discover(tmp_path)
    deps = {d.name: d for d in result.dependencies}
    assert set(deps) == {"jinja2", "requests", "six", "python-dateutil"}
    first = deps["jinja2"].declared[0]
    assert (first.file, first.line, first.spec) == ("requirements.txt", 2, "==2.11.3")
    assert deps["requests"].declared[0].line == 6
    assert deps["six"].declared[0].spec == ""
    assert deps["python-dateutil"].declared[0].file == "more.txt"


def test_unparsable_lines_become_notes_not_crashes(tmp_path):
    write(tmp_path, "requirements.txt", "good==1\nthis is not a requirement !!\n")
    result = discover(tmp_path)
    assert [d.name for d in result.dependencies] == ["good"]
    assert len(result.notes) == 1 and "requirements.txt:2" in result.notes[0]


def test_pyproject_dependencies_optional_dependencies_and_groups_get_lines(tmp_path):
    write(tmp_path, "pyproject.toml",
          '[project]\nname = "x"\ndependencies = [\n    "toml==0.10.2",\n    "Rich",\n]\n\n'
          '[project.optional-dependencies]\nweb = ["flask>=2"]\n\n[dependency-groups]\ndev = ["pytest"]\n')
    deps = {d.name: d.declared[0] for d in discover(tmp_path).dependencies}
    assert set(deps) == {"toml", "rich", "flask", "pytest"}
    assert (deps["toml"].file, deps["toml"].line, deps["toml"].spec) == ("pyproject.toml", 4, "==0.10.2")
    assert deps["flask"].line == 9 and deps["pytest"].line == 12


def test_conflicting_specs_in_different_files_are_flagged(tmp_path):
    write(tmp_path, "requirements.txt", "pydantic==1.10.13\nclick>=7\nrich\n")
    write(tmp_path, "pyproject.toml", '[project]\nname = "x"\ndependencies = ["pydantic>=2", "click>=7", "rich"]\n')
    result = discover(tmp_path)
    assert [c.name for c in result.conflicts] == ["pydantic"]
    c = result.conflicts[0]
    assert c.tag == "VERIFIED" and "requirements.txt:1" in c.detail and "pyproject.toml" in c.detail


def test_compatible_or_same_file_specs_are_not_conflicts():
    assert specs_compatible(">=7", "==8.1") and specs_compatible("==1.4.4", "==1.4.4")
    assert not specs_compatible("==1.10.13", ">=2") and not specs_compatible("==1.0", "==2.0")
    a, b = Declaration("r.txt", 1, "==1"), Declaration("r.txt", 2, "==2")
    assert find_conflicts({"x": Dependency("x", [a, b])}) == []  # same file: not a cross-file conflict


def test_repo_without_manifests_says_so(tmp_path):
    result = discover(tmp_path)
    assert result.dependencies == [] and any("no requirements" in n for n in result.notes)
