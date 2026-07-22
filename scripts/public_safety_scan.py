"""Fail-closed privacy scan for a candidate public Git repository.

The command reports only fixed finding codes and aggregate counts. It never
prints matching text, object IDs, ref names, paths, or exact prohibited values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class Finding:
    code: str
    path: str


@dataclass(frozen=True)
class ScanReport:
    refs_scanned: int
    objects_scanned: int
    worktree_files_scanned: int
    findings: tuple[Finding, ...]


class ScanError(RuntimeError):
    """A safe-to-suppress repository or input failure."""


PATTERNS = {
    "aws_access_key_id": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "credentialed_dsn": re.compile(r"postgres(?:ql)?://[^\s/:]+:[^\s@]+@", re.I),
    "literal_bearer": re.compile(
        r"Authorization:\s*Bearer\s+(?![<{]|REPLACE_|test-only)[A-Za-z0-9._~+/-]{20,}",
        re.I,
    ),
    "home_path": re.compile(r"/(?:Users|home)/[^/\s]+/"),
    "cockroach_cluster_url": re.compile(r"cockroachlabs\.cloud/cluster/[0-9a-f-]{36}", re.I),
}
ACCOUNT_IDS = (
    re.compile(r"arn:aws:[^:\s]*:[^:\s]*:([0-9]{12}):"),
    re.compile(r"\bAWS_ACCOUNT_ID\s*=\s*([0-9]{12})\b"),
    re.compile(r"\b([0-9]{12})\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com\b"),
)
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
OBJECT_ID = re.compile(r"^[0-9a-f]{40,64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
BINARY_MAGIC = (
    b"%PDF-",
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"PK\x03\x04",
    b"\x1f\x8b",
    b"\x7fELF",
    b"SQLite format 3\x00",
)


def scan_text(text: str, *, path: str, exact_values: tuple[str, ...] = ()) -> list[Finding]:
    """Return opaque classifications; never retain or return matched values."""
    findings = [Finding(code, path) for code, pattern in PATTERNS.items() if pattern.search(text)]
    if any(
        value != "000000000000"
        for pattern in ACCOUNT_IDS
        for value in pattern.findall(text)
    ):
        findings.append(Finding("aws_account_id", path))
    for email in EMAIL.findall(text):
        lowered = email.lower()
        reserved = lowered.endswith((".example", ".test", ".invalid"))
        if not reserved and "replace_with" not in lowered:
            findings.append(Finding("non_example_email", path))
            break
    if any(value and value in text for value in exact_values):
        findings.append(Finding("exact_prohibited_value", path))
    return findings


def _run_git(repo: Path, *args: str, timeout: int = 120) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScanError("git_operation_failed") from exc
    return result.stdout


def discover_refs(repo: Path) -> tuple[str, ...]:
    """Discover every local ref plus detached HEAD, without exposing names."""
    raw = _run_git(repo, "for-each-ref", "--format=%(refname)")
    refs = {line for line in raw.decode("utf-8", errors="strict").splitlines() if line}
    try:
        _run_git(repo, "rev-parse", "--verify", "HEAD")
    except ScanError:
        pass
    else:
        refs.add("HEAD")
    return tuple(sorted(refs))


def _validated_refs(repo: Path, refs: Iterable[str]) -> tuple[str, ...]:
    validated = []
    for ref in refs:
        if not ref or ref.startswith("-") or "\x00" in ref or "\n" in ref:
            raise ScanError("invalid_ref")
        _run_git(repo, "rev-parse", "--verify", ref)
        validated.append(ref)
    return tuple(dict.fromkeys(validated))


def _reachable_objects(repo: Path, refs: tuple[str, ...]) -> tuple[str, ...]:
    if not refs:
        return ()
    raw = _run_git(repo, "rev-list", "--objects", "--no-object-names", *refs)
    objects = []
    for line in raw.decode("ascii", errors="strict").splitlines():
        object_id = line.strip()
        if not OBJECT_ID.fullmatch(object_id):
            raise ScanError("invalid_git_object")
        objects.append(object_id)
    return tuple(dict.fromkeys(objects))


def _object(repo: Path, object_id: str) -> tuple[str, bytes]:
    kind = _run_git(repo, "cat-file", "-t", object_id, timeout=30).decode("ascii").strip()
    if kind not in {"blob", "commit", "tag", "tree"}:
        raise ScanError("unsupported_git_object")
    return kind, _run_git(repo, "cat-file", kind, object_id, timeout=30)


def _scan_bytes(
    data: bytes,
    *,
    origin: str,
    exact_values: tuple[str, ...],
    allowed_binary_sha256: frozenset[str],
) -> list[Finding]:
    binary = any(data.startswith(magic) for magic in BINARY_MAGIC)
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        text = ""
        binary = True
    else:
        binary = binary or "\x00" in text
    if binary:
        digest = hashlib.sha256(data).hexdigest()
        return [] if digest in allowed_binary_sha256 else [Finding("binary_manual_review", origin)]
    return scan_text(text, path=origin, exact_values=exact_values)


def _worktree_paths(repo: Path) -> tuple[Path, ...]:
    raw = _run_git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    paths = []
    for encoded in raw.split(b"\x00"):
        if not encoded:
            continue
        try:
            relative = Path(os.fsdecode(encoded))
        except UnicodeError as exc:
            raise ScanError("worktree_path_unreadable") from exc
        if relative.is_absolute() or ".." in relative.parts:
            raise ScanError("worktree_path_unsafe")
        paths.append(relative)
    return tuple(dict.fromkeys(paths))


def _worktree_bytes(path: Path) -> bytes | None:
    try:
        if path.is_symlink():
            return os.fsencode(os.readlink(path))
        if not path.exists() or not path.is_file():
            return None
        return path.read_bytes()
    except OSError as exc:
        raise ScanError("worktree_file_unreadable") from exc


def scan_repository(
    repo: Path,
    *,
    refs: Iterable[str] | None = None,
    exact_values: tuple[str, ...] = (),
    allowed_binary_sha256: Iterable[str] = (),
) -> ScanReport:
    repo = repo.resolve()
    selected_refs = discover_refs(repo) if refs is None else tuple(refs)
    selected_refs = _validated_refs(repo, selected_refs)
    allowed = frozenset(value.lower() for value in allowed_binary_sha256)
    if any(not SHA256.fullmatch(value) for value in allowed):
        raise ScanError("invalid_binary_allowlist")

    findings: list[Finding] = []
    if selected_refs:
        # Ref names and annotated tag names are metadata too. They remain
        # internal to the scanner; output exposes only fixed finding codes.
        findings.extend(
            scan_text("\n".join(selected_refs), path="ref_metadata", exact_values=exact_values)
        )

    object_ids = _reachable_objects(repo, selected_refs)
    for object_id in object_ids:
        kind, data = _object(repo, object_id)
        if kind == "tree":
            continue
        origin = "history_blob" if kind == "blob" else f"{kind}_metadata"
        findings.extend(
            _scan_bytes(
                data,
                origin=origin,
                exact_values=exact_values,
                allowed_binary_sha256=allowed,
            )
        )

    worktree_paths = _worktree_paths(repo)
    files_scanned = 0
    for relative in worktree_paths:
        data = _worktree_bytes(repo / relative)
        if data is None:
            continue
        files_scanned += 1
        findings.extend(
            _scan_bytes(
                data,
                origin="worktree_file",
                exact_values=exact_values,
                allowed_binary_sha256=allowed,
            )
        )

    return ScanReport(
        refs_scanned=len(selected_refs),
        objects_scanned=len(object_ids),
        worktree_files_scanned=files_scanned,
        findings=tuple(findings),
    )


def _nested_scalar_values(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for nested in value.values():
            yield from _nested_scalar_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _nested_scalar_values(nested)
    elif isinstance(value, str):
        if value:
            yield value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield str(value)


def _exact_values(path: Path | None) -> tuple[str, ...]:
    if path is None:
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ScanError("exact_values_unreadable") from exc
    return tuple(dict.fromkeys(_nested_scalar_values(payload)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--ref", action="append", default=None)
    parser.add_argument("--exact-values-file", type=Path)
    parser.add_argument("--allow-binary-sha256", action="append", default=[])
    args = parser.parse_args(argv)

    try:
        report = scan_repository(
            args.repo,
            refs=args.ref,
            exact_values=_exact_values(args.exact_values_file),
            allowed_binary_sha256=args.allow_binary_sha256,
        )
    except (ScanError, UnicodeError, ValueError):
        print("scan_error=true")
        print("passed=false")
        return 2

    codes = sorted({finding.code for finding in report.findings})
    print(f"refs_scanned={report.refs_scanned}")
    print(f"objects_scanned={report.objects_scanned}")
    print(f"worktree_files_scanned={report.worktree_files_scanned}")
    print(f"findings={len(report.findings)}")
    print("finding_codes=" + (",".join(codes) if codes else "none"))
    print(f"passed={str(not report.findings).lower()}")
    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
