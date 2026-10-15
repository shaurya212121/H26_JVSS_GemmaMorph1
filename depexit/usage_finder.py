"""
Stage 3: AST-based dependency usage finder.

Scans every Python file in a target repository using Python's standard `ast`
module, locating every import, attribute access, private API call, and monkeypatch
associated with flagged dependencies.

Complies strictly with the shared data contract in depexit.models.
No external dependencies (uses standard library only). No hard-coded mappings.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from depexit.log import log
from depexit.models import OK, StageStatus, Usage, UsageResult, VERIFIED

# Directories to unconditionally skip during repository traversal
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "venvs",
    "env",
    "node_modules",
    "__pycache__",
    ".tox",
    ".eggs",
    "build",
    "dist",
}


def _normalize_name(name: str) -> str:
    """Normalize a package distribution name to lowercase-hyphen format."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _is_private_symbol(name: str) -> bool:
    """True if name starts with a single underscore and is not a dunder."""
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__")) and len(name) > 1


def _extract_snippet(lines: list[str], target_line: int, context: int = 2) -> str:
    """
    Extract raw source lines around target_line (1-based), preserving exact whitespace.
    Returns target_line plus `context` lines each side, copy-pasteable verbatim.
    """
    if not lines:
        return ""
    idx = max(0, target_line - 1)
    start = max(0, idx - context)
    end = min(len(lines), idx + context + 1)
    return "".join(lines[start:end])


def _reconstruct_attribute_chain(node: ast.AST) -> Optional[tuple[str, ast.AST]]:
    """
    If node is an Attribute chain (e.g. a.b.c or Name 'a'), reconstruct dotted name.
    Returns (dotted_string, root_node) or None.
    """
    parts = []
    curr = node
    while isinstance(curr, ast.Attribute):
        parts.append(curr.attr)
        curr = curr.value
    if isinstance(curr, ast.Name):
        parts.append(curr.id)
        return ".".join(reversed(parts)), curr
    return None


class _FileUsageVisitor(ast.NodeVisitor):
    """
    AST visitor scoped to a single file. Identifies imports, attribute uses,
    private API usages, and monkeypatching.
    """

    def __init__(
        self,
        rel_file: str,
        source_lines: list[str],
        module_to_dists: dict[str, list[str]],
    ):
        self.rel_file = rel_file
        self.source_lines = source_lines
        self.module_to_dists = module_to_dists

        # Local alias mapping: local_name -> (canonical_dotted_origin, top_module)
        # e.g. import requests as req -> "req": ("requests", "requests")
        # e.g. from requests.utils import quote as q -> "q": ("requests.utils.quote", "requests")
        self.aliases: dict[str, tuple[str, str]] = {}

        # Stack of enclosing function/class definitions
        self.scope_stack: list[str] = []

        # Collected usages: list of Usage objects
        self.usages: list[Usage] = []

        # Parent tracking to find outermost attribute chains only
        self.parent_map: dict[int, ast.AST] = {}

    def _current_scope(self) -> str:
        return ".".join(self.scope_stack)

    def _record(
        self,
        line: int,
        symbol: str,
        dist: str,
        kind: str,
    ) -> None:
        snippet = _extract_snippet(self.source_lines, line)
        self.usages.append(
            Usage(
                file=self.rel_file,
                line=line,
                symbol=symbol,
                snippet=snippet,
                dependency=dist,
                kind=kind,
                function=self._current_scope(),
                tag=VERIFIED,
            )
        )

    # ---------------------------------------------------------------- Scope Tracking

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    # ---------------------------------------------------------------- Imports

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top_mod = alias.name.split(".")[0]
            if top_mod in self.module_to_dists:
                local_name = alias.asname or alias.name
                self.aliases[local_name] = (alias.name, top_mod)

                # Check if any component in the imported name is private
                parts = alias.name.split(".")
                is_private = any(_is_private_symbol(p) for p in parts)
                kind = "private-api" if is_private else "import"

                for dist in self.module_to_dists[top_mod]:
                    self._record(
                        line=node.lineno,
                        symbol=alias.name,
                        dist=dist,
                        kind=kind,
                    )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # Ignore relative imports (level > 0)
        if node.level and node.level > 0:
            self.generic_visit(node)
            return

        if not node.module:
            self.generic_visit(node)
            return

        top_mod = node.module.split(".")[0]
        if top_mod in self.module_to_dists:
            module_private = any(_is_private_symbol(p) for p in node.module.split("."))
            for alias in node.names:
                full_symbol = f"{node.module}.{alias.name}"
                local_name = alias.asname or alias.name
                self.aliases[local_name] = (full_symbol, top_mod)

                is_private = module_private or _is_private_symbol(alias.name)
                kind = "private-api" if is_private else "import"

                for dist in self.module_to_dists[top_mod]:
                    self._record(
                        line=node.lineno,
                        symbol=full_symbol,
                        dist=dist,
                        kind=kind,
                    )
        self.generic_visit(node)

    # ---------------------------------------------------------------- Attributes

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # We only record the outermost attribute chain
        parent = self.parent_map.get(id(node))
        if isinstance(parent, ast.Attribute):
            # Inner chain, skip recording here; will be handled by outer Attribute
            self.generic_visit(node)
            return

        resolved = _reconstruct_attribute_chain(node)
        if resolved:
            chain_str, root = resolved
            if isinstance(root, ast.Name) and root.id in self.aliases:
                origin_symbol, top_mod = self.aliases[root.id]
                # Replace root alias with its origin
                parts = chain_str.split(".")
                suffix = ".".join(parts[1:])
                full_symbol = f"{origin_symbol}.{suffix}" if suffix else origin_symbol

                # Check if this attribute access is a monkeypatch assignment target
                # (Attribute assignments are handled in visit_Assign / visit_AugAssign / visit_Delete)
                if not self._is_assignment_target(node):
                    is_private = any(_is_private_symbol(p) for p in full_symbol.split("."))
                    kind = "private-api" if is_private else "attribute"
                    if top_mod in self.module_to_dists:
                        for dist in self.module_to_dists[top_mod]:
                            self._record(
                                line=node.lineno,
                                symbol=full_symbol,
                                dist=dist,
                                kind=kind,
                            )

        self.generic_visit(node)

    def _is_assignment_target(self, node: ast.AST) -> bool:
        """Check if node is an assignment, augassign, or delete target."""
        parent = self.parent_map.get(id(node))
        if isinstance(parent, (ast.Assign, ast.AnnAssign)):
            if node in getattr(parent, "targets", []) or getattr(parent, "target", None) is node:
                return True
        elif isinstance(parent, (ast.AugAssign, ast.Delete)):
            if getattr(parent, "target", None) is node or node in getattr(parent, "targets", []):
                return True
        return False

    # ---------------------------------------------------------------- Monkeypatching

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._check_monkeypatch_target(target, node.lineno)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._check_monkeypatch_target(node.target, node.lineno)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._check_monkeypatch_target(target, node.lineno)
        self.generic_visit(node)

    def _check_monkeypatch_target(self, target: ast.AST, lineno: int) -> None:
        resolved = _reconstruct_attribute_chain(target)
        if resolved:
            chain_str, root = resolved
            if isinstance(root, ast.Name) and root.id in self.aliases:
                origin_symbol, top_mod = self.aliases[root.id]
                parts = chain_str.split(".")
                suffix = ".".join(parts[1:])
                full_symbol = f"{origin_symbol}.{suffix}" if suffix else origin_symbol
                if top_mod in self.module_to_dists:
                    for dist in self.module_to_dists[top_mod]:
                        self._record(
                            line=lineno,
                            symbol=full_symbol,
                            dist=dist,
                            kind="monkeypatch",
                        )

    def visit_Call(self, node: ast.Call) -> None:
        # 1. setattr(flagged_module, 'attr', value)
        func_name = None
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        if func_name == "setattr" and len(node.args) >= 2:
            first_arg = node.args[0]
            resolved = _reconstruct_attribute_chain(first_arg)
            if resolved:
                chain_str, root = resolved
                if isinstance(root, ast.Name) and root.id in self.aliases:
                    origin_symbol, top_mod = self.aliases[root.id]
                    parts = chain_str.split(".")
                    suffix = ".".join(parts[1:])
                    full_symbol = f"{origin_symbol}.{suffix}" if suffix else origin_symbol

                    attr_name = ""
                    if isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                        attr_name = f".{node.args[1].value}"

                    if top_mod in self.module_to_dists:
                        for dist in self.module_to_dists[top_mod]:
                            self._record(
                                line=node.lineno,
                                symbol=f"{full_symbol}{attr_name}",
                                dist=dist,
                                kind="monkeypatch",
                            )

        # 2. mock.patch("flagged.module.attr") / monkeypatch.setattr("flagged.module.attr", ...)
        # Detect calls where any string argument starts with a flagged module name
        is_patch_call = False
        if func_name in ("patch", "setattr"):
            is_patch_call = True
        elif isinstance(node.func, ast.Attribute):
            full_func_resolved = _reconstruct_attribute_chain(node.func)
            if full_func_resolved:
                fn_str = full_func_resolved[0]
                if any(k in fn_str for k in ("mock.patch", "monkeypatch.setattr", "patch.object")):
                    is_patch_call = True

        if is_patch_call and node.args:
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                target_str = first_arg.value
                top_part = target_str.split(".")[0]
                if top_part in self.module_to_dists:
                    for dist in self.module_to_dists[top_part]:
                        self._record(
                            line=node.lineno,
                            symbol=target_str,
                            dist=dist,
                            kind="monkeypatch",
                        )

        self.generic_visit(node)


def find_usages(
    repo: Path,
    flagged: list[str],
    module_map: dict[str, list[str]],
) -> UsageResult:
    """
    Find all usages of flagged dependencies across all .py files in repo.

    Parameters:
        repo: Root path of the target repository.
        flagged: Normalized distribution names (e.g. 'jinja2', 'requests').
        module_map: Top-level import name -> list of normalized dist names.

    Returns:
        UsageResult with usages mapped per distribution and any file notes.
    """
    repo = Path(repo).resolve()
    normalized_flagged = [_normalize_name(f) for f in flagged]
    flagged_set = set(normalized_flagged)

    notes: list[str] = []

    # Map top-level importable module -> list of flagged distribution names
    module_to_flagged_dists: dict[str, list[str]] = {}
    dists_with_modules: set[str] = set()

    for mod, dists in module_map.items():
        matched_dists = [_normalize_name(d) for d in dists if _normalize_name(d) in flagged_set]
        if matched_dists:
            module_to_flagged_dists[mod] = matched_dists
            for d in matched_dists:
                dists_with_modules.add(d)

    # Note any flagged distribution that has no associated module in module_map
    for dist in normalized_flagged:
        if dist not in dists_with_modules:
            notes.append(f"flagged dependency '{dist}' has no top-level module in module_map")

    # Initialize usage lists for every flagged distribution
    usages_by_dist: dict[str, list[Usage]] = {d: [] for d in normalized_flagged}

    if not repo.exists() or not repo.is_dir():
        notes.append(f"repository directory does not exist: {repo}")
        return UsageResult(usages=usages_by_dist, notes=notes, status=StageStatus("usage", status=OK))

    log(f"Stage 3: Scanning AST in {repo} for {len(normalized_flagged)} flagged dependencies")

    # Collect all python files, filtering out ignored directories
    py_files: list[Path] = []
    for root, dirs, files in os.walk(repo):
        # Prune ignored directories in-place
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(".py"):
                py_files.append(Path(root) / f)

    py_files.sort()

    for py_path in py_files:
        try:
            rel_file = py_path.relative_to(repo).as_posix()
        except ValueError:
            rel_file = str(py_path).replace("\\", "/")

        try:
            source = py_path.read_text(encoding="utf-8")
        except Exception as exc:
            notes.append(f"{rel_file}: read error: {exc}")
            continue

        try:
            tree = ast.parse(source, filename=rel_file)
        except Exception as exc:
            notes.append(f"{rel_file}: syntax error: {exc}")
            continue

        source_lines = source.splitlines(keepends=True)

        visitor = _FileUsageVisitor(rel_file, source_lines, module_to_flagged_dists)

        # Build parent map for outermost attribute chain detection
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                visitor.parent_map[id(child)] = parent

        visitor.visit(tree)

        for u in visitor.usages:
            if u.dependency in usages_by_dist:
                usages_by_dist[u.dependency].append(u)

    # Deduplicate and sort by (file, line)
    for dist in usages_by_dist:
        seen: set[tuple[str, int, str, str]] = set()
        deduped: list[Usage] = []
        for u in usages_by_dist[dist]:
            key = (u.file, u.line, u.symbol, u.kind)
            if key not in seen:
                seen.add(key)
                deduped.append(u)
        deduped.sort(key=lambda x: (x.file, x.line))
        usages_by_dist[dist] = deduped

    total_usages = sum(len(v) for v in usages_by_dist.values())
    log(f"Stage 3 complete: Found {total_usages} usages across {len(py_files)} files")

    return UsageResult(
        usages=usages_by_dist,
        notes=notes,
        status=StageStatus("usage", status=OK),
    )


if __name__ == "__main__":
    import importlib.metadata
    import json
    import sys

    if len(sys.argv) < 3:
        print("Usage: python -m depexit.usage_finder <repo_path> <dist1> [<dist2>...]", file=sys.stderr)
        sys.exit(1)

    target_repo = Path(sys.argv[1])
    target_dists = sys.argv[2:]

    # Build real module_map from current environment
    raw_dist_map = importlib.metadata.packages_distributions()
    normalized_module_map: dict[str, list[str]] = {
        mod: [_normalize_name(d) for d in dists]
        for mod, dists in raw_dist_map.items()
    }

    result = find_usages(target_repo, target_dists, normalized_module_map)

    for dist, usages in result.usages.items():
        print(f"\n=== Dependency: {dist} ({len(usages)} usages) ===")
        for u in usages:
            print(f"  {u.file}:{u.line} [{u.kind}] {u.symbol} (in '{u.function or '<module>'}')")
            if u.kind in ("private-api", "monkeypatch"):
                print(f"    -> Special kind: {u.kind}")

    if result.notes:
        print(f"\nNotes ({len(result.notes)}):")
        for n in result.notes:
            print(f"  * {n}")
