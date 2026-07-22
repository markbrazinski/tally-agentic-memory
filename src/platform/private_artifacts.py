"""Constrained writers for ignored recovery evidence containing private identifiers."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRIVATE_ROOT = REPOSITORY_ROOT / "runtime-artifacts"


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def private_artifact_path(path: Path, *, private_root: Path = DEFAULT_PRIVATE_ROOT) -> Path:
    """Resolve a path only inside the dedicated ignored private-artifact tree."""
    root = private_root.absolute()
    candidate = path if path.is_absolute() else REPOSITORY_ROOT / path
    candidate = candidate.absolute()
    if root.is_symlink() or candidate.is_symlink():
        raise OSError("private artifact paths must not be symbolic links")
    resolved_root = root.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    if not _within(resolved_candidate, resolved_root) or resolved_candidate == resolved_root:
        raise ValueError("private artifact output must be inside runtime-artifacts")
    current = candidate.parent
    while current != root.parent:
        if current.exists() and current.is_symlink():
            raise OSError("private artifact parent must not be a symbolic link")
        if current == root:
            break
        current = current.parent
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    candidate.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    os.chmod(candidate.parent, 0o700)
    return resolved_candidate


def write_private_json(
    path: Path,
    value: Mapping[str, object],
    *,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> None:
    """Write JSON mode 0600 without following the target or final parent symlink."""
    target = private_artifact_path(path, private_root=private_root)
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(target.parent, parent_flags)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(target.name, flags, 0o600, dir_fd=parent_fd)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                fd = -1
                json.dump(value, output, sort_keys=True, separators=(",", ":"), default=str)
                output.write("\n")
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        os.close(parent_fd)
