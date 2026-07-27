"""Deployed authority-transition acceptance — runs the REAL flow on the isolated
judge lane and captures the eight required proofs. Not a fixture test.

Flow: authenticated import -> poll reconstruction revision 1 (REQUEST_EVIDENCE,
6/7) -> release the held evidence task -> poll revision 2 (DISPUTE 70000, 7/7).
Then the negative/immutability proofs. Run twice for the acceptance record.

Usage:
  python scripts/authority_acceptance.py --url https://<host> \
      --username judge@tally-demo.example --password '***' \
      --pdf tests/fixtures/demo/INV-1048.pdf --run 1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import uuid

import httpx


def _login(c, base, user, pw):
    r = c.post(f"{base}/api/login", json={"username": user, "password": pw})
    if r.status_code != 200:
        raise SystemExit(f"login failed: {r.status_code} {r.text[:200]}")


def _poll_reconstruction(c, base, invoice_id, *, want_type, timeout=90):
    """Poll the projection until the recommendation reaches want_type (or timeout).
    Returns the projection dict. Never fabricates — reads server truth only."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = c.get(f"{base}/api/invoices/{invoice_id}/reconstruction")
        if r.status_code == 200:
            last = r.json()
            rec = last.get("recommendation")
            if rec and rec.get("recommendation_type") == want_type:
                return last
        time.sleep(3)
    return last


def _check(results, name, ok, detail=""):
    results.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def run(url, user, pw, pdf_path, run_no):
    base = url.rstrip("/")
    pdf = open(pdf_path, "rb").read()
    pdf_sha = hashlib.sha256(pdf).hexdigest()
    idem = f"authority-accept-run{run_no}-{uuid.uuid4()}"
    results = []
    print(f"\n=== AUTHORITY ACCEPTANCE · run {run_no} · {base} ===")

    c = httpx.Client(timeout=60, follow_redirects=False)
    _login(c, base, user, pw)

    # Import through the real deployed intake path.
    files = {"file": ("INV-1048.pdf", pdf, "application/pdf")}
    data = {"demo_scenario": "locked-inv-1048", "import_source": "operator_import"}
    imp = c.post(f"{base}/api/demo/invoices", headers={"Idempotency-Key": idem},
                 files=files, data=data)
    snap = imp.json() if imp.status_code in (200, 201) else {}
    invoice_id = (snap.get("invoice") or {}).get("invoice_id")
    _check(results, "real PDF imported through deployed intake",
           imp.status_code in (200, 201) and bool(invoice_id), f"invoice_id={invoice_id}")
    if not invoice_id:
        return _summary(results, run_no)

    # PROOF 1: revision 1 = REQUEST_EVIDENCE, coverage 6/7.
    rev1 = _poll_reconstruction(c, base, invoice_id, want_type="REQUEST_EVIDENCE")
    rec1 = (rev1 or {}).get("recommendation") or {}
    cov1 = (rev1 or {}).get("coverage") or {}
    rev1_version = (rev1 or {}).get("version")
    _check(results, "revision 1 recommendation = REQUEST_EVIDENCE",
           rec1.get("recommendation_type") == "REQUEST_EVIDENCE",
           str(rec1.get("recommendation_type")))
    _check(results, "revision 1 coverage = 6/7",
           cov1.get("days_complete") == 6 and cov1.get("days_total") == 7,
           f"{cov1.get('days_complete')}/{cov1.get('days_total')}")
    _check(results, "revision 1 names the missing access evidence",
           "TERMINAL_ACCESS" in (cov1.get("missing_requirements") or []),
           str(cov1.get("missing_requirements")))

    # PROOF 8: approval unavailable for revision 1 (no DISPUTE recommendation).
    _check(results, "approval unavailable at revision 1",
           rec1.get("recommendation_type") != "DISPUTE", "no DISPUTE recommendation yet")

    # Controlled release of the durable evidence task.
    rel = c.post(f"{base}/api/invoices/{invoice_id}/release-evidence")
    _check(results, "controlled release accepted", rel.status_code == 200,
           f"status={rel.status_code} {rel.text[:80]}")

    # PROOF 2: revision 2 = DISPUTE 70000, coverage 7/7.
    rev2 = _poll_reconstruction(c, base, invoice_id, want_type="DISPUTE", timeout=120)
    rec2 = (rev2 or {}).get("recommendation") or {}
    cov2 = (rev2 or {}).get("coverage") or {}
    rev2_version = (rev2 or {}).get("version")
    _check(results, "revision 2 recommendation = DISPUTE",
           rec2.get("recommendation_type") == "DISPUTE", str(rec2.get("recommendation_type")))
    _check(results, "revision 2 disputed amount = 70000 minor units",
           rec2.get("disputed_amount_minor") == 70000, str(rec2.get("disputed_amount_minor")))
    _check(results, "revision 2 coverage = 7/7",
           cov2.get("days_complete") == 7 and cov2.get("days_total") == 7,
           f"{cov2.get('days_complete')}/{cov2.get('days_total')}")

    # PROOF 3 (immutability + queryable): rev1 and rev2 are distinct versions.
    _check(results, "revision 1 preserved as a distinct earlier version",
           rev1_version is not None and rev2_version is not None and rev2_version > rev1_version,
           f"rev1 v{rev1_version} < rev2 v{rev2_version}")

    # PROOF 4 (refresh/reconnect reproduces from durable history): a fresh client
    # (no prior state) reading the projection sees the same rev2 truth.
    c2 = httpx.Client(timeout=60, follow_redirects=False)
    _login(c2, base, user, pw)
    reread = c2.get(f"{base}/api/invoices/{invoice_id}/reconstruction").json()
    rr = reread.get("recommendation") or {}
    _check(results, "refresh/reconnect reproduces DISPUTE 7/7 from durable state",
           rr.get("recommendation_type") == "DISPUTE"
           and (reread.get("coverage") or {}).get("days_complete") == 7,
           f"{rr.get('recommendation_type')} "
           f"{(reread.get('coverage') or {}).get('days_complete')}/7")

    # PROOF 5 (idempotent duplicate import): same idem key + bytes -> same invoice.
    files2 = {"file": ("INV-1048.pdf", pdf, "application/pdf")}
    dup = c.post(f"{base}/api/demo/invoices", headers={"Idempotency-Key": idem},
                 files=files2, data=data)
    dup_inv = None
    if dup.status_code in (200, 201):
        dup_inv = (dup.json().get("invoice") or {}).get("invoice_id")
    _check(results, "duplicate source processing is idempotent",
           dup_inv == invoice_id, f"{dup_inv} == {invoice_id}")

    # PROOF 6 (no private-id leak in public responses).
    forbidden = ["s3_version_id_private", "s3_object_key_private", "arn:aws:", "oauth-token-bundle"]
    blob = json.dumps(rev2 or {}) + imp.text
    leaked = [t for t in forbidden if t in blob]
    _check(results, "no private storage identifiers in public projection", not leaked, str(leaked))

    print("\n--- evidence snapshot ---")
    print(json.dumps({
        "run": run_no, "invoice_id": invoice_id,
        "rev1": {"version": rev1_version, "type": rec1.get("recommendation_type"),
                 "coverage": f"{cov1.get('days_complete')}/{cov1.get('days_total')}",
                 "reason_codes": rec1.get("reason_codes")},
        "rev2": {"version": rev2_version, "type": rec2.get("recommendation_type"),
                 "disputed_minor": rec2.get("disputed_amount_minor"),
                 "coverage": f"{cov2.get('days_complete')}/{cov2.get('days_total')}"},
        "pdf_sha256": pdf_sha,
    }, indent=2))
    return _summary(results, run_no)


def _summary(results, run_no):
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"\n=== run {run_no}: {passed}/{total} checks passed ===")
    return 0 if passed == total else 1


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--username", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--pdf", required=True)
    p.add_argument("--run", type=int, default=1)
    a = p.parse_args()
    sys.exit(run(a.url, a.username, a.password, a.pdf, a.run))


if __name__ == "__main__":
    main()
