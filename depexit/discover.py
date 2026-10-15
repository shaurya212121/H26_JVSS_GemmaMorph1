"""
Stage 1: Discover. No AI.

Reads requirements*.txt (including `-r` includes) and pyproject.toml
([project.dependencies], optional-dependencies, [dependency-groups]),
normalizes package names, records the file and line of every declaration, and
flags specifiers in different files that cannot both be satisfied.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from depexit.log import log
from depexit.models import Conflict, Declaration, Dependency, DiscoveryResult, VERIFIED

SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".tox", "build", "dist", ".eggs"}


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requirements_files(root: Path) -> list[Path]:
    """requirements*.txt in the repo root and in a requirements/ directory."""
    found = set(root.glob("requirements*.txt"))
    req_dir = root / "requirements"
    if req_dir.is_dir():
        found.update(req_dir.glob("*.txt"))
    return sorted(found)


def _strip_comment(line: str) -> str:
    return re.sub(r"(^|\s)#.*$", "", line).strip()


def _parse_requirement(text: str, where: str, notes: list[str]) -> tuple[str, str] | None:
    try:
        req = Requirement(text)
    except InvalidRequirement as exc:
        notes.append(f"{where}: cannot parse requirement {text!r}: {exc}")
        return None
    return normalize_name(req.name), str(req.specifier)


def parse_requirements_file(
    path: Path, root: Path, notes: list[str], _seen: set[Path] | None = None
) -> list[tuple[str, Declaration]]:
    seen = _seen if _seen is not None else set()
    path = path.resolve()
    if path in seen:
        return []
    seen.add(path)
    rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
    try:
        raw_lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        notes.append(f"{rel}: cannot read file: {exc}")
        return []

    out: list[tuple[str, Declaration]] = []
    i = 0
    while i < len(raw_lines):
        lineno = i + 1
        text = raw_lines[i]
        while text.rstrip().endswith("\\") and i + 1 < len(raw_lines):  # line continuation
            i += 1
            text = text.rstrip()[:-1] + raw_lines[i]
        i += 1
        text = _strip_comment(text)
        if not text:
            continue
        include = re.match(r"^(?:-r|--requirement)[\s=]+(\S+)", text)
        if include:
            out.extend(parse_requirements_file((path.parent / include.group(1)), root, notes, seen))
            continue
        if text.startswith("-") or "://" in text and "@" not in text:
            continue  # pip options, editable installs, bare URLs
        parsed = _parse_requirement(text, f"{rel}:{lineno}", notes)
        if parsed:
            name, spec = parsed
            out.append((name, Declaration(file=rel, line=lineno, spec=spec)))
    return out


def _locate_line(lines: list[str], literal: str, used: set[int]) -> int:
    for idx, line in enumerate(lines):
        if idx in used:
            continue
        if f'"{literal}"' in line or f"'{literal}'" in line:
            used.add(idx)
            return idx + 1
    return 0


def parse_pyproject(path: Path, root: Path, notes: list[str]) -> list[tuple[str, Declaration]]:
    rel = path.relative_to(root).as_posix()
    try:
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        notes.append(f"{rel}: cannot parse: {exc}")
        return []

    lines = text.splitlines()
    used: set[int] = set()
    groups: list[tuple[str, list]] = []
    project = data.get("project", {})
    groups.append(("project.dependencies", project.get("dependencies", [])))
    for extra, items in project.get("optional-dependencies", {}).items():
        groups.append((f"project.optional-dependencies.{extra}", items))
    for group, items in data.get("dependency-groups", {}).items():
        groups.append((f"dependency-groups.{group}", items))
    if "poetry" in data.get("tool", {}):
        notes.append(f"{rel}: [tool.poetry] dependencies are not parsed (only PEP 621 / dependency-groups)")

    out: list[tuple[str, Declaration]] = []
    for label, items in groups:
        for item in items:
            if not isinstance(item, str):
                continue  # e.g. {include-group = "..."}
            lineno = _locate_line(lines, item, used)
            parsed = _parse_requirement(item, f"{rel}:{lineno or '?'} [{label}]", notes)
            if parsed:
                name, spec = parsed
                out.append((name, Declaration(file=rel, line=lineno, spec=spec)))
    return out


def _candidate_versions(*specs: str) -> list[Version]:
    base: set[Version] = {Version("0"), Version("9999")}
    for spec in specs:
        for part in SpecifierSet(spec):
            try:
                v = Version(part.version.replace(".*", ""))
            except InvalidVersion:
                continue
            major, minor, micro = v.major, v.minor, v.micro
            base.update({
                v, Version(str(major + 1)), Version(f"{major}.{minor + 1}"),
                Version(f"{major}.{minor}.{micro + 1}"), Version(str(max(major - 1, 0))),
            })
    return sorted(base)


def specs_compatible(a: str, b: str) -> bool:
    """True when some version satisfies both specifier sets (checked on versions the specs mention)."""
    sa, sb = SpecifierSet(a), SpecifierSet(b)
    return any(sa.contains(c, prereleases=True) and sb.contains(c, prereleases=True)
               for c in _candidate_versions(a, b))


def find_conflicts(deps: dict[str, Dependency]) -> list[Conflict]:
    conflicts: list[Conflict] = []
    for name, dep in sorted(deps.items()):
        decls = dep.declared
        for i in range(len(decls)):
            for j in range(i + 1, len(decls)):
                a, b = decls[i], decls[j]
                if a.file == b.file or not a.spec or not b.spec:
                    continue
                if not specs_compatible(a.spec, b.spec):
                    conflicts.append(Conflict(
                        name=name,
                        declarations=[a, b],
                        detail=f"{name}{a.spec} ({a.file}:{a.line}) cannot be satisfied together with "
                               f"{name}{b.spec} ({b.file}:{b.line})",
                        tag=VERIFIED,
                    ))
    return conflicts


def discover(repo: Path) -> DiscoveryResult:
    root = Path(repo).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Target repository not found: {root}")

    notes: list[str] = []
    pairs: list[tuple[str, Declaration]] = []
    for req_file in requirements_files(root):
        pairs.extend(parse_requirements_file(req_file, root, notes))
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        pairs.extend(parse_pyproject(pyproject, root, notes))

    deps: dict[str, Dependency] = {}
    for name, decl in pairs:
        dep = deps.setdefault(name, Dependency(name=name))
        if decl not in dep.declared:  # an included file can be reached twice
            dep.declared.append(decl)

    if not deps:
        notes.append("no requirements*.txt or pyproject.toml dependencies found")
    conflicts = find_conflicts(deps)
    log(f"  discovered {len(deps)} dependencies, {len(conflicts)} conflicts, {len(notes)} notes")
    return DiscoveryResult(repo=str(root), dependencies=sorted(deps.values(), key=lambda d: d.name),
                           conflicts=conflicts, notes=notes)


if __name__ == "__main__":
    import sys

    result = discover(Path(sys.argv[1]))
    for dep in result.dependencies:
        print(f"{dep.name:16} " + "  ".join(f"{d.file}:{d.line} {d.spec or '(unpinned)'}" for d in dep.declared))
    print(f"\nCONFLICTS ({len(result.conflicts)})")
    for c in result.conflicts:
        print(f"  [{c.tag}] {c.detail}")
    print(f"NOTES ({len(result.notes)})")
    for n in result.notes:
        print(f"  {n}")
