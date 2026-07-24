"""Authenticated operator PDF import into the deployed Tally intake pipeline.

Submits a real PDF through the deployed multipart intake endpoint
(POST /api/demo/invoices) as an authenticated operator. This represents an
invoice entering Tally from an upstream channel; it drives the REAL deployed
pipeline (exact-version S3 preservation, durable extraction + reconstruction,
SSE progress), not a database fixture. The invoice then appears in the UI without
a hard refresh, and this script returns its invoice_id.

The `--source` value is explicit: `operator_import` (an operator submitting a
PDF) or `forwarded_email_simulation` (a stand-in for a forwarded-email path).
A real inbound-email adapter would later POST the same PDF + metadata to this
SAME intake contract without changing downstream processing.

Auth: against the judge-demo deployment, pass a Cognito username/password (the
script logs in and uses the session cookie). For a local/static-bearer backend,
pass --bearer.

Usage:
  python -m scripts.operator_import --url https://<host> --pdf tests/fixtures/demo/INV-1048.pdf \\
      --source operator_import --username judge --password '***'
  # local:
  python -m scripts.operator_import --url http://localhost:8000 --pdf <pdf> --bearer <token>
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

import httpx


def _login(client: httpx.Client, base: str, username: str, password: str) -> None:
    r = client.post(f"{base}/api/login", json={"username": username, "password": password})
    if r.status_code != 200:
        raise SystemExit(f"login failed: {r.status_code} {r.text[:200]}")
    # The session cookie is now set on the client and carried to the import.


def import_pdf(
    *,
    base_url: str,
    pdf_path: str,
    source: str,
    idempotency_key: str,
    bearer: str | None = None,
    username: str | None = None,
    password: str | None = None,
    timeout: float = 60.0,
) -> dict:
    """Import one PDF through the deployed intake endpoint. Returns the invoice
    snapshot (including invoice_id). This is the function a future email adapter
    would call with the same contract."""
    base = base_url.rstrip("/")
    with open(pdf_path, "rb") as fh:
        pdf_bytes = fh.read()

    headers = {"Idempotency-Key": idempotency_key}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        if username and password:
            _login(client, base, username, password)
        files = {"file": ("invoice.pdf", pdf_bytes, "application/pdf")}
        data = {"demo_scenario": "locked-inv-1048", "import_source": source}
        resp = client.post(
            f"{base}/api/demo/invoices", headers=headers, files=files, data=data
        )
    if resp.status_code not in (200, 201):
        raise SystemExit(f"import failed: {resp.status_code} {resp.text[:300]}")
    return {"status": resp.status_code, "replay": resp.status_code == 200,
            "snapshot": resp.json()}


def main() -> None:
    p = argparse.ArgumentParser(description="Authenticated operator PDF import.")
    p.add_argument("--url", required=True, help="deployed base URL (https://host)")
    p.add_argument("--pdf", required=True, help="path to the PDF to import")
    p.add_argument("--source", default="operator_import",
                   choices=["operator_import", "forwarded_email_simulation"])
    p.add_argument("--idempotency-key", default=None,
                   help="stable key for idempotent replay (default: random)")
    p.add_argument("--bearer", default=None, help="static bearer token (local backend)")
    p.add_argument("--username", default=None, help="Cognito username (deployed)")
    p.add_argument("--password", default=None, help="Cognito password (deployed)")
    args = p.parse_args()

    key = args.idempotency_key or f"operator-import-{uuid.uuid4()}"
    result = import_pdf(
        base_url=args.url, pdf_path=args.pdf, source=args.source,
        idempotency_key=key, bearer=args.bearer,
        username=args.username, password=args.password,
    )
    inv = result["snapshot"].get("invoice", {})
    out = {
        "http_status": result["status"],
        "idempotent_replay": result["replay"],
        "invoice_id": inv.get("invoice_id"),
        "import_source": inv.get("import_source"),
        "aggregate_status": inv.get("aggregate_status"),
        "idempotency_key": key,
    }
    print(json.dumps(out, indent=2))
    if not out["invoice_id"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
