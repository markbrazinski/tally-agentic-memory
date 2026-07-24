from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.platform.app import app, require_auth
from src.platform.auth import AuthedActor


@pytest.fixture
def client():
    app.dependency_overrides[require_auth] = lambda: AuthedActor(
        user_id="00000000-0000-4000-8000-000000000042",
        display_name="Rachel Martinez",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _snapshot():
    return {
        "invoice": {
            "invoice_id": "00000000-0000-4000-8000-000000000100",
            "display_name": "INV-1048.pdf",
            "status": "RECEIVED",
        },
        "links": {},
    }


def test_controlled_upload_requires_idempotency_key(client):
    response = client.post(
        "/api/demo/invoices",
        files={"file": ("INV-1048.pdf", b"%PDF-1.4", "application/pdf")},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_controlled_upload_returns_created_and_replay_headers(client):
    pdf = (
        __import__("pathlib").Path(__file__).parents[1]
        / "fixtures"
        / "demo"
        / "INV-1048.pdf"
    ).read_bytes()
    with patch(
        "src.platform.intake_api.receive_invoice",
        return_value=(_snapshot(), False),
    ):
        created = client.post(
            "/api/demo/invoices",
            headers={"Idempotency-Key": "upload-1"},
            files={"file": ("INV-1048.pdf", pdf, "application/pdf")},
        )
    with patch(
        "src.platform.intake_api.receive_invoice",
        return_value=(_snapshot(), True),
    ):
        replay = client.post(
            "/api/demo/invoices",
            headers={"Idempotency-Key": "upload-1"},
            files={"file": ("INV-1048.pdf", pdf, "application/pdf")},
        )

    assert created.status_code == 201
    assert created.headers["idempotent-replay"] == "false"
    assert replay.status_code == 200
    assert replay.headers["idempotent-replay"] == "true"
    assert created.json()["invoice"]["invoice_id"] == replay.json()["invoice"]["invoice_id"]


def test_controlled_upload_rejects_non_pdf_bytes_before_dependencies(client):
    response = client.post(
        "/api/demo/invoices",
        headers={"Idempotency-Key": "upload-1"},
        files={"file": ("INV-1048.pdf", b"not-pdf", "application/pdf")},
    )

    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "UNSUPPORTED_FILE"
