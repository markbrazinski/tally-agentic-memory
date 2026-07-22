"""Fictional source registry used by the public recovery fixture.

The names, URLs, lane, and identifiers below are deliberately synthetic. A
private deployment supplies its own registry and never commits source URLs or
capture identifiers to public Git history.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    """One capture target: a single URL Tally fetches once per run.

    Attributes:
        key: Unique, stable identifier for this source. Used as the S3 key
            prefix segment (`raw/{key}/{YYYY-MM-DD}/body.{ext}`), so it must
            be filesystem/URL-path safe and never change once captures exist.
        url: Canonical public artifact URL to fetch (page, or direct
            PDF/document link — per source recon decision logged in
            docs/evidence-log.md).
        expected_content_type: Content-Type the fetch is expected to return
            (e.g. "text/html", "application/pdf"). Used to sanity-check the
            response, not to enforce a hard fetch failure.
        lane_label: Human-readable trade-lane label for this source, e.g.
            "NP-SP" — surfaced in manifests and operator-facing tooling.
    """

    key: str
    url: str
    expected_content_type: str
    lane_label: str


SOURCES: tuple[Source, ...] = (
    Source(
        key="northstar-ocean-demo-tariff",
        url="https://fixtures.example.invalid/northstar/tariff.html",
        expected_content_type="text/html",
        lane_label="NP-SP",
    ),
    Source(
        key="bluehaven-maritime-demo-tariff",
        url="https://fixtures.example.invalid/bluehaven/tariff.pdf",
        expected_content_type="application/pdf",
        lane_label="NP-SP",
    ),
    Source(
        key="harborview-terminal-demo-tariff",
        url="https://fixtures.example.invalid/harborview/tariff.html",
        expected_content_type="text/html",
        lane_label="NP-SP",
    ),
)


def get_source(key: str) -> Source:
    """Look up a single registered source by key.

    Raises:
        KeyError: if no source with that key is registered.
    """
    for source in SOURCES:
        if source.key == key:
            return source
    raise KeyError(f"no registered source with key={key!r}")
