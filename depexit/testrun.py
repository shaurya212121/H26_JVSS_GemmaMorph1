"""
Run a target repo's pytest suite inside an Env and parse the real results:
per-test outcomes from a JUnit XML file, plus the warnings summary and failure
tracebacks from the output. Optionally under coverage.py.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from depexit.envs import Env, run_command
from depexit.models import RunResult

NO_TESTS_COLLECTED = 5  # pytest exit code


@dataclass
class Failure:
    test: str
    message: str
    text: str  # traceback / error body


@dataclass
class TestRun:
    __test__ = False  # not a pytest test class

    result: RunResult
    failures: list[Failure] = field(default_factory=list)
    warning_blocks: list[tuple[str, str]] = field(default_factory=list)  # (location, text)
    output: str = ""


def parse_junit(path: Path) -> tuple[dict[str, str], list[Failure]]:
    tests: dict[str, str] = {}
    failures: list[Failure] = []
    if not path.is_file():
        return tests, failures
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return tests, failures
    for case in root.iter("testcase"):
        nodeid = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
        failure, error, skipped = case.find("failure"), case.find("error"), case.find("skipped")
        if failure is not None:
            tests[nodeid] = "failed"
            failures.append(Failure(nodeid, failure.get("message", ""), failure.text or ""))
        elif error is not None:
            tests[nodeid] = "error"
            failures.append(Failure(nodeid, error.get("message", ""), error.text or ""))
        elif skipped is not None:
            tests[nodeid] = "skipped"
        else:
            tests[nodeid] = "passed"
    return tests, failures


def parse_warning_blocks(output: str) -> list[tuple[str, str]]:
    """Blocks of pytest's 'warnings summary': a non-indented header line followed by indented text."""
    lines = output.splitlines()
    start = next((i for i, l in enumerate(lines) if "warnings summary" in l and l.startswith("=")), None)
    if start is None:
        return []
    blocks: list[tuple[str, str]] = []
    location, body = "", []
    for line in lines[start + 1:]:
        if line.startswith("=") or line.startswith("-- Docs"):
            break
        if line and not line[0].isspace():
            if location and body:
                blocks.append((location, " ".join(body)))
            location, body = line.strip(), []
        elif line.strip():
            body.append(line.strip())
    if location and body:
        blocks.append((location, " ".join(body)))
    return blocks


def run_tests(
    env: Env,
    workdir: Path,
    label: str,
    scratch: Path,
    timeout: int = 600,
    coverage_data: Path | None = None,
) -> TestRun:
    """Run pytest in workdir with env's python. scratch is a dir outside workdir for the junit file."""
    scratch.mkdir(parents=True, exist_ok=True)
    junit = scratch / f"{label}-junit.xml"
    pytest_args = ["-m", "pytest", "-p", "no:cacheprovider", "-W", "default", "--tb=short", "-q",
                   f"--junitxml={junit}"]
    if coverage_data is not None:
        cmd = [str(env.python), "-m", "coverage", "run", f"--data-file={coverage_data}", *pytest_args]
    else:
        cmd = [str(env.python), *pytest_args]

    result, output = run_command(label, cmd, workdir, timeout)
    tests, failures = parse_junit(junit)
    result.tests = tests
    result.passed = sum(1 for v in tests.values() if v == "passed")
    result.failed = sum(1 for v in tests.values() if v == "failed")
    result.errors = sum(1 for v in tests.values() if v == "error")
    result.skipped = sum(1 for v in tests.values() if v == "skipped")
    blocks = parse_warning_blocks(output)
    result.warnings = [f"{loc}: {text}" for loc, text in blocks]
    return TestRun(result=result, failures=failures, warning_blocks=blocks, output=output)
