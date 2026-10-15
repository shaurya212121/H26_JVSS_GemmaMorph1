"""
Sandbox helpers shared by S2 (risk scan) and S5 (rehearsal): copy a repo to a
temp dir, create a fresh virtual environment (uv if present, else venv + pip),
install packages into it, and run commands, recording the real exit code.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from depexit.log import log
from depexit.models import RunResult, VERIFIED

COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache", ".tox",
    "node_modules", "*.egg-info", ".coverage", "htmlcov",
)
TAIL_CHARS = 4000


def _on_rm_error(func, path, exc) -> None:  # Windows: clear read-only bit, retry
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def safe_rmtree(path: Path) -> None:
    shutil.rmtree(path, onexc=_on_rm_error)


def copy_repo(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst, ignore=COPY_IGNORE, symlinks=True)
    return dst


def tail(text: str, limit: int = TAIL_CHARS) -> str:
    return text if len(text) <= limit else "...[truncated]...\n" + text[-limit:]


def run_command(label: str, cmd: list[str], cwd: Path, timeout: int) -> tuple[RunResult, str]:
    """Run cmd (an argument list), return (RunResult with the real exit code, full combined output)."""
    log(f"  $ {' '.join(cmd)[:160]}")
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return RunResult(label=label, command=cmd, exit_code=proc.returncode,
                         stdout_tail=tail(output), tag=VERIFIED), output
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        return RunResult(label=label, command=cmd, exit_code=None, timed_out=True,
                         stdout_tail=tail(f"[timed out after {timeout}s]\n{partial}"), tag=VERIFIED), partial
    except OSError as exc:
        return RunResult(label=label, command=cmd, exit_code=None,
                         stdout_tail=f"[could not start: {exc}]", tag=VERIFIED), str(exc)


@dataclass
class Env:
    root: Path
    python: Path
    use_uv: bool

    def pip_install_cmd(self, args: list[str]) -> list[str]:
        if self.use_uv:
            return ["uv", "pip", "install", "--python", str(self.python), *args]
        return [str(self.python), "-m", "pip", "install", "--disable-pip-version-check", *args]


def create_env(root: Path, timeout: int = 600) -> tuple[Env | None, RunResult]:
    """Create a fresh venv at root. Returns (Env or None on failure, the creation RunResult)."""
    use_uv = shutil.which("uv") is not None
    cmd = ["uv", "venv", str(root), "--python", sys.executable] if use_uv else [sys.executable, "-m", "venv", str(root)]
    result, _ = run_command("create-venv", cmd, root.parent, timeout)
    python = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if result.exit_code != 0 or not python.exists():
        return None, result
    return Env(root=root, python=python, use_uv=use_uv), result


def pip_install(env: Env, label: str, args: list[str], cwd: Path, timeout: int = 900) -> tuple[RunResult, str]:
    return run_command(label, env.pip_install_cmd(args), cwd, timeout)
