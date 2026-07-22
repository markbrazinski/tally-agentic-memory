from __future__ import annotations

from types import SimpleNamespace

import scripts.gate5b_token_leak_scan as leak_scan
from scripts.gate5b_token_leak_scan import run_scan
from scripts.public_safety_scan import Finding, ScanReport


class Store:
    def load(self):
        return SimpleNamespace(
            access_token="private-access",
            refresh_token="private-refresh",
            client_id="private-client",
        )


def test_live_values_are_used_in_memory_and_never_returned(monkeypatch, tmp_path):
    captured = {}

    def scan(repo, *, exact_values):
        captured["repo"] = repo
        captured["exact_values"] = exact_values
        return ScanReport(1, 1, 1, ())

    monkeypatch.setattr(leak_scan, "scan_repository", scan)
    findings, passed = run_scan(Store(), repo=tmp_path)
    assert (findings, passed) == (0, True)
    assert captured["exact_values"] == (
        "private-access",
        "private-refresh",
        "private-client",
    )


def test_only_opaque_exact_value_finding_count_is_returned(monkeypatch, tmp_path):
    monkeypatch.setattr(
        leak_scan,
        "scan_repository",
        lambda *_args, **_kwargs: ScanReport(
            1,
            1,
            1,
            (Finding("exact_prohibited_value", "opaque"),),
        ),
    )
    assert run_scan(Store(), repo=tmp_path) == (1, False)
