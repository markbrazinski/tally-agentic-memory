"""Every contract fixture must be valid, parseable JSON.

Per bundle-0.md's lock #4, contract/fixtures/ is the frozen FE interface -
a syntax error here is worse than a normal bug, since it breaks the one
artifact another workstream (Bundle 1-FE) builds against without ever
running this repo's own test suite.
"""

from __future__ import annotations

import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES_DIR = os.path.join(REPO_ROOT, "contract", "fixtures")


def test_fixtures_directory_exists():
    assert os.path.isdir(FIXTURES_DIR)


def test_every_json_fixture_parses():
    json_files = [f for f in os.listdir(FIXTURES_DIR) if f.endswith(".json")]
    assert json_files, "expected at least one fixture file"
    for filename in json_files:
        with open(os.path.join(FIXTURES_DIR, filename)) as f:
            json.load(f)  # raises json.JSONDecodeError on malformed content


def test_readme_documents_every_json_fixture():
    """The README's route table is the FE team's map from route to file -
    a fixture added without a README entry is silently undiscoverable."""
    json_files = {f for f in os.listdir(FIXTURES_DIR) if f.endswith(".json")}
    with open(os.path.join(FIXTURES_DIR, "README.md")) as f:
        readme_text = f.read()
    undocumented = [f for f in json_files if f not in readme_text]
    assert undocumented == [], f"fixtures missing from README.md's table: {undocumented}"


def test_gate4_contract_examples_are_public_safe_and_truth_labeled():
    with open(os.path.join(FIXTURES_DIR, "GET_cases_id_replay.json")) as fixture_file:
        replay = json.load(fixture_file)
    with open(os.path.join(FIXTURES_DIR, "GET_evals_latest.json")) as fixture_file:
        evaluation = json.load(fixture_file)

    assert replay["then"]["state"] == "FILED"
    assert replay["now"]["state"] == "CONTESTED"
    assert replay["retention"]["ttl_seconds"] == 7_776_000
    assert replay["queries"] == []
    assert "immutable" not in replay["sealed_copy"]["source"]
    assert evaluation["classification"] == "synthetic demonstration contract example"
    assert evaluation["available"] is False
    assert evaluation["synthetic"] is True


def test_every_contract_fixture_has_explicit_synthetic_classification():
    for filename in os.listdir(FIXTURES_DIR):
        if not filename.endswith(".json"):
            continue
        with open(os.path.join(FIXTURES_DIR, filename)) as fixture_file:
            fixture = json.load(fixture_file)
        assert fixture["classification"] == "synthetic demonstration contract example"
