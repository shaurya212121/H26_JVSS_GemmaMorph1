"""
Exact old_text -> new_text edits, shared by S4 (validation) and S5 (application)
so both stages agree on what "the edit applies" means.

An edit applies only if old_text occurs EXACTLY ONCE in the file. Line endings
are compared as LF and the file's own style (LF or CRLF) is restored on write.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class EditError(ValueError):
    """The edit cannot be applied; the message is the reason."""


@dataclass
class AppliedEdit:
    text: str  # the file content (LF) after the edit
    start_line: int  # first line of new_text in the edited file, 1-based
    end_line: int  # last line of new_text (equal to start_line for a pure deletion)


def resolve_in_repo(repo: Path, rel: str) -> Path:
    """Resolve rel inside repo; refuse anything that escapes it or is not an existing file."""
    root = repo.resolve()
    path = (root / rel).resolve()
    if root != path and root not in path.parents:
        raise EditError(f"path '{rel}' is outside the repository")
    if not path.is_file():
        raise EditError(f"file '{rel}' does not exist")
    return path


def read_lf(path: Path) -> tuple[str, bool]:
    """Return (content with LF newlines, True if the file used CRLF)."""
    try:
        raw = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EditError(f"'{path.name}' is not valid UTF-8: {exc}") from exc
    return raw.replace("\r\n", "\n"), "\r\n" in raw


def write_lf(path: Path, text: str, crlf: bool) -> None:
    path.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def apply_edit(text: str, old_text: str, new_text: str) -> AppliedEdit:
    old, new = old_text.replace("\r\n", "\n"), new_text.replace("\r\n", "\n")
    if not old:
        raise EditError("old_text is empty")
    if old == new:
        raise EditError("old_text and new_text are identical")
    count = text.count(old)
    if count == 0:
        raise EditError("old_text does not occur in the file")
    if count > 1:
        raise EditError(f"old_text occurs {count} times in the file; it must be unique")
    index = text.index(old)
    start = text.count("\n", 0, index) + 1
    new_lines = (new.count("\n") + (0 if new.endswith("\n") else 1)) if new else 1
    return AppliedEdit(text=text[:index] + new + text[index + len(old):],
                       start_line=start, end_line=start + max(new_lines, 1) - 1)
