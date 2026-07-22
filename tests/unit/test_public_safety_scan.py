from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from scripts.public_safety_scan import _exact_values, main, scan_repository, scan_text


def _codes(text: str, *, exact: tuple[str, ...] = ()) -> set[str]:
    return {finding.code for finding in scan_text(text, path="fixture", exact_values=exact)}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Synthetic Test Author")
    _git(repo, "config", "user.email", "author@fictional.example")
    (repo / "safe.txt").write_text("synthetic public fixture\n", encoding="utf-8")
    _git(repo, "add", "safe.txt")
    _git(repo, "commit", "-m", "safe initial commit")
    return repo


def test_safe_placeholders_and_fictional_email_pass():
    text = "\n".join(
        (
            "AWS_ACCOUNT_ID=" + "0" * 12,
            "TALLY_CRDB_DSN=REPLACE_WITH_SECRET_DSN",
            "contact=reviewer@fictional.example",
            "Authorization: Bearer {your-service-account-api-key}",
        )
    )
    assert _codes(text) == set()


def test_common_secret_and_metadata_shapes_are_opaque_findings():
    access_key = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
    dsn = "postgres" + "ql://private-user:private-password@private.example/db"
    account = "123456" + "789012"
    home = "/" + "Users/private/person/file"
    email = "reviewer@" + "real-domain.com"
    text = "\n".join((access_key, dsn, f"arn:aws:iam::{account}:role/private", home, email))

    assert _codes(text) == {
        "aws_access_key_id",
        "credentialed_dsn",
        "aws_account_id",
        "home_path",
        "non_example_email",
    }


def test_test_source_does_not_embed_the_prohibited_fixture_shapes():
    source = Path(__file__).read_text(encoding="utf-8")
    assert _codes(source) == set()


def test_private_exact_values_are_detected_even_when_shape_is_opaque():
    private_value = "opaque-private-" + "version"
    assert _codes("prefix " + private_value + " suffix", exact=(private_value,)) == {
        "exact_prohibited_value"
    }


def test_nested_json_exact_values_are_collected_recursively(tmp_path: Path):
    opaque = "nested-opaque-" + "value"
    numeric = int("123456" + "789012")
    exact_file = tmp_path / "exact-values.json"
    exact_file.write_text(
        json.dumps({"environments": [{"private": {"version": opaque}}], "account": numeric}),
        encoding="utf-8",
    )

    assert _exact_values(exact_file) == (opaque, str(numeric))


def test_auto_discovery_scans_removed_blobs_on_non_current_refs(tmp_path: Path):
    repo = _repo(tmp_path)
    access_key = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
    _git(repo, "switch", "-c", "historical-side")
    (repo / "temporary.txt").write_text(access_key, encoding="utf-8")
    _git(repo, "add", "temporary.txt")
    _git(repo, "commit", "-m", "temporary historical content")
    (repo / "temporary.txt").write_text("safe replacement\n", encoding="utf-8")
    _git(repo, "commit", "-am", "remove historical content")
    _git(repo, "switch", "main")

    report = scan_repository(repo)

    assert report.refs_scanned >= 3  # HEAD plus both local branches
    assert "aws_access_key_id" in {finding.code for finding in report.findings}
    assert "history_blob" in {finding.path for finding in report.findings}


def test_commit_and_annotated_tag_metadata_are_scanned(tmp_path: Path):
    repo = _repo(tmp_path)
    commit_value = "opaque-commit-" + "metadata"
    tag_value = "opaque-tag-" + "metadata"
    _git(repo, "commit", "--allow-empty", "-m", commit_value)
    _git(repo, "tag", "-a", "public-candidate", "-m", tag_value)

    report = scan_repository(repo, exact_values=(commit_value, tag_value))
    origins = {
        finding.path
        for finding in report.findings
        if finding.code == "exact_prohibited_value"
    }

    assert "commit_metadata" in origins
    assert "tag_metadata" in origins


def test_dirty_tracked_and_untracked_files_are_both_scanned(tmp_path: Path):
    repo = _repo(tmp_path)
    dirty_value = "opaque-dirty-" + "value"
    untracked_value = "opaque-untracked-" + "value"
    (repo / "safe.txt").write_text(dirty_value, encoding="utf-8")
    (repo / "new.txt").write_text(untracked_value, encoding="utf-8")

    report = scan_repository(repo, exact_values=(dirty_value, untracked_value))
    worktree_findings = [
        finding
        for finding in report.findings
        if finding.code == "exact_prohibited_value" and finding.path == "worktree_file"
    ]

    assert len(worktree_findings) == 2
    assert report.worktree_files_scanned == 2


def test_binary_history_requires_digest_allowlist(tmp_path: Path):
    repo = _repo(tmp_path)
    # ASCII-only PDF headers still denote binary review surfaces; NUL/UTF-8
    # heuristics alone are insufficient.
    binary = b"%PDF-1.7\nsynthetic fixture\n%%EOF\n"
    binary_path = repo / "artifact.bin"
    binary_path.write_bytes(binary)
    _git(repo, "add", "artifact.bin")
    _git(repo, "commit", "-m", "add reviewed binary")
    binary_path.write_text("safe text replacement\n", encoding="utf-8")
    _git(repo, "commit", "-am", "replace reviewed binary")

    blocked = scan_repository(repo)
    allowed = scan_repository(repo, allowed_binary_sha256=(hashlib.sha256(binary).hexdigest(),))

    assert "binary_manual_review" in {finding.code for finding in blocked.findings}
    assert "binary_manual_review" not in {finding.code for finding in allowed.findings}


def test_cli_output_never_echoes_matching_values(tmp_path: Path, capsys):
    repo = _repo(tmp_path)
    opaque = "never-echo-this-" + "private-value"
    (repo / "untracked.txt").write_text(opaque, encoding="utf-8")
    exact_file = tmp_path / "private-input.json"
    exact_file.write_text(json.dumps({"nested": {"value": opaque}}), encoding="utf-8")

    assert main(["--repo", str(repo), "--exact-values-file", str(exact_file)]) == 1
    captured = capsys.readouterr()

    assert opaque not in captured.out
    assert opaque not in captured.err
    assert "exact_prohibited_value" in captured.out
