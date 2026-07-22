"""FastAPI app: local skeleton for Bundle 0 (no deploy this session - B0-S3's job).

Routes per bundle-0.md B0-S2: GET /healthz, POST/GET /invoices,
GET /cases/{id}, WS /feed. Response shapes match contract/fixtures/ where
a fixture exists; contract/fixtures/README.md's "shape TBD" list applies
to GET /cases/{id} (no worked TDD example) - shaped here as a reasonable
reading of TDD §3.2's field list, flagged as provisional in the docstring
rather than presented as frozen.

WS /feed broadcasts in-process (TDD §4: "one worker, FIFO" - no message
queue needed at this scale). Every clerk.step/clerk.filed event during a
POST /invoices run gets pushed to every connected WS client.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, datetime
from io import BytesIO

import pdfplumber
from fastapi import Depends, FastAPI, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.websockets import WebSocket, WebSocketDisconnect

from src.external.bedrock_extract import BedrockExtractor, apply_anti_hallucination_gate
from src.external.dal import DAL, Tenant
from src.platform.auth import AuthedActor, make_require_bearer_auth
from src.platform.clerk_pipeline import file_case, run_extraction_steps
from src.platform.seal import CaseNotFoundError, CaseNotSealableError, seal_case
from src.platform.temporal_replay import (
    ReplayNotFoundError,
    ReplayNotSealedError,
    ReplayUnavailableError,
    replay_case,
)

app = FastAPI(title="Tally")

DEMO_TENANT_ID = "10000000-0000-4000-8000-000000000002"  # Meridian Demo, seeded in B0-S1
DEMO_ACTOR = "rachel.martinez"

require_auth = make_require_bearer_auth(DEMO_TENANT_ID)


def _cors_allowed_origins() -> list[str]:
    """Explicit allowlist, never "*" (bearer auth, not cookies, but a
    wildcard would still let any site read responses). TALLY_CORS_ORIGINS
    is comma-separated for multiple dev/preview origins; TALLY_STATIC_ORIGIN
    is a placeholder the FE fills in once their static-host target is
    chosen - wiring it now means adding the origin is a config change, not
    a code change."""
    origins = [
        o.strip()
        for o in os.environ.get("TALLY_CORS_ORIGINS", "http://localhost:5173").split(",")
        if o.strip()
    ]
    static_origin = os.environ.get("TALLY_STATIC_ORIGIN", "").strip()
    if static_origin:
        origins.append(static_origin)
    return origins


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allowed_origins(),
    allow_credentials=False,  # bearer header, not cookies
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=600,
)

# invoices.s3_key is STRING NOT NULL (migrations/002_bundle0_schema.sql,
# matching TDD §2.9 - every real invoice has a real S3 pointer). This
# session does no S3 write at all (local-only pipeline, per B0-S2 scope),
# so a real key doesn't exist yet - this sentinel says so honestly rather
# than fighting the NOT NULL constraint with a fabricated-looking value.
# The API response still returns s3_key: null (contract/fixtures/
# POST_invoices.json's shape), so this never leaks past the DB row itself.
NO_S3_WRITE_THIS_SESSION = "local-only:no-s3-write-b0-s2"


class FeedBroadcaster:
    """In-process WS fan-out. One connected-clients set; every event goes
    to every client. No persistence - a client that wasn't connected when
    an event fired simply didn't see it (GET /recordings/log-style polling
    backstop is a later bundle's problem, per TDD §3.11)."""

    def __init__(self):
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, event: str, payload: dict) -> None:
        message = {"event": event, "ts": datetime.now(UTC).isoformat(), "payload": payload}
        dead = []
        for client in self._clients:
            try:
                await client.send_json(message)
            except Exception:  # noqa: BLE001 - a dead socket must not break the broadcast
                dead.append(client)
        for client in dead:
            self._clients.discard(client)


feed = FeedBroadcaster()


def _dal() -> DAL:
    return DAL.connect(Tenant(tenant_id=DEMO_TENANT_ID, actor=DEMO_ACTOR))


@app.get("/healthz")
def healthz() -> dict:
    """Each dependency checked with a real, cheap query - never assumed
    healthy. MCP is configuration-only here: a live MCP call on every health
    request would be wasteful and would not prove the later-contest path."""
    db_status = "ok"
    try:
        with _dal() as dal:
            # tenants is the one table that IS the tenant-id source, not
            # a table scoped BY tenant_id - it has no tenant_id column
            # (found live: a naive WHERE tenant_id=%s here raised
            # UndefinedColumn, invisible to the mocked test suite since
            # nothing there validates SQL against the real schema).
            dal.execute("SELECT 1 FROM tenants WHERE id=%s LIMIT 1", (), tag="healthz.db")
    except Exception:  # noqa: BLE001 - a failed health check reports itself, never raises
        db_status = "error"

    mcp_configured = all(
        os.environ.get(name)
        for name in (
            "TALLY_MCP_CLUSTER_ID",
            "TALLY_MCP_DATABASE",
            "TALLY_MCP_ACCESS_TOKEN",
            "TALLY_MCP_SERVICE_IDENTITY",
            "TALLY_MCP_PERMISSION_MODE",
        )
    )
    return {
        "db": db_status,
        "mcp": "configured_not_checked" if mcp_configured else "not_configured",
        "bedrock": "not_checked",  # a live Bedrock call on every /healthz hit is wasteful;
                                    # confirmed manually this session instead (see HANDOFF).
        "last_snapshot": None,
        "version": "local-dev",
    }


@app.post("/invoices", status_code=202)
async def create_invoice(file: UploadFile, actor: AuthedActor = Depends(require_auth)) -> dict:
    """POST /invoices: upload a D&D invoice, trigger a Clerk run.

    File validation only this session (PDF content-type, <=10MB per §3.1) -
    the actual Clerk pipeline call happens as a background asyncio task
    (TDD §4: "in-process asyncio task inside the API service"), broadcasting
    clerk.step/clerk.filed over the WS feed as it runs.
    """
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=422, detail="file must be a PDF")

    body = await file.read()
    if len(body) > 10 * 1024 * 1024:
        raise HTTPException(status_code=422, detail="file exceeds 10 MB")

    sha256 = hashlib.sha256(body).hexdigest()

    # DAL.execute() is synchronous psycopg - run_in_threadpool keeps these
    # real network round-trips off the event loop, so a slow DB call here
    # doesn't stall every other concurrent request/WS keep-alive (found in
    # code review: this route was async def with blocking calls inline).
    result = await run_in_threadpool(_insert_invoice_and_run, sha256)
    if result["status"] == "DUPLICATE":
        return result

    invoice_id, clerk_run_id = result["invoice_id"], result["clerk_run_id"]
    asyncio.create_task(_run_clerk_pipeline_background(invoice_id, clerk_run_id, body, sha256))

    return {
        "invoice_id": invoice_id,
        "clerk_run_id": clerk_run_id,
        "status": "RECEIVED",
        "sha256": sha256,
        "s3_key": None,  # no S3 write this session - local-only pipeline, per B0-S2 scope
    }


def _insert_invoice_and_run(sha256: str) -> dict:
    """The synchronous DAL portion of create_invoice, run off the event
    loop via run_in_threadpool."""
    with _dal() as dal:
        existing = dal.execute(
            "SELECT id FROM invoices WHERE tenant_id=%s AND sha256=%s",
            (sha256,),
            tag="invoice.dedupe_check",
        )
        if existing:
            return {
                "invoice_id": str(existing[0][0]),
                "clerk_run_id": None,
                "status": "DUPLICATE",
                "sha256": sha256,
                "s3_key": None,
            }

        rows = dal.execute(
            """
            INSERT INTO invoices (tenant_id, carrier_id, received_at, s3_key, sha256, status)
            VALUES (%s, (SELECT id FROM carriers WHERE tenant_id=%s LIMIT 1), now(), %s, %s,
                    'RECEIVED')
            RETURNING id;
            """,
            (DEMO_TENANT_ID, NO_S3_WRITE_THIS_SESSION, sha256),
            tag="invoice.insert",
        )
        invoice_id = str(rows[0][0])

        run_rows = dal.execute(
            "INSERT INTO clerk_runs (tenant_id, invoice_id, status) VALUES (%s, %s, 'QUEUED') "
            "RETURNING id;",
            (invoice_id,),
            tag="clerk_run.insert",
        )
        clerk_run_id = str(run_rows[0][0])

    return {"invoice_id": invoice_id, "clerk_run_id": clerk_run_id, "status": "RECEIVED"}


async def _run_clerk_pipeline_background(
    invoice_id: str, clerk_run_id: str, pdf_bytes: bytes, sha256: str, *, extractor=None
) -> None:
    """Steps 1->2->3->7, run as a background asyncio task per TDD §4
    ("in-process asyncio task inside the API service"), broadcasting
    clerk.step/clerk.filed over the WS feed as it runs.

    The real Bedrock call happens here, in the actual running app - never
    in the test suite. `extractor` defaults to a real BedrockExtractor()
    but is an explicit parameter specifically so tests can force a
    CannedResponseExtractor instead - asyncio.create_task()'s fire-and-
    forget nature means this coroutine CAN genuinely run after an HTTP
    handler returns (confirmed: it is not reliably prevented by test
    teardown timing alone), so "no live Bedrock in tests" needs to be a
    structural guarantee here, not an accident of when the event loop
    happens to close. The pipeline's own logic (extraction gate, steps
    2-3, the atomic filing commit) is exercised directly and fully by
    tests/unit/test_clerk_pipeline.py / test_end_to_end_fixture.py; this
    function is the thin wiring that connects the real HTTP trigger to
    that already-tested logic.
    """
    if extractor is None:
        extractor = BedrockExtractor()

    await feed.broadcast("clerk.step", {"run_id": clerk_run_id, "step": 1, "name": "extraction"})

    def _run_sync() -> dict:
        with _dal() as dal:
            carrier_rows = dal.execute(
                """
                SELECT c.id, c.date_format_hint FROM invoices i
                JOIN carriers c ON c.tenant_id = i.tenant_id AND c.id = i.carrier_id
                WHERE i.tenant_id=%s AND i.id=%s;
                """,
                (invoice_id,),
                tag="clerk.carrier_lookup",
            )
        carrier_id, date_format_hint = (
            (str(carrier_rows[0][0]), carrier_rows[0][1]) if carrier_rows else (None, None)
        )

        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)

        raw_result = extractor.extract(text, date_format_hint=date_format_hint)
        gated = apply_anti_hallucination_gate(raw_result, text)

        # billed_party_name: no invoice-header-parsing step exists in this
        # session's scope (that's a future bundle's job) - the demo
        # tenant's own name is used as a stand-in so the proper-party
        # heuristic has something real to compare against locally, rather
        # than leaving it None (which would always report field 7 missing).
        # invoice_date_raw=None: no invoice-header-date-parsing step
        # exists in this session's scope (see docstring above).
        clerk_result = run_extraction_steps(
            gated,
            billed_party_name="Example Mercantile (fictional)",
            invoice_date_raw=None,
            date_format_hint=date_format_hint,
        )

        with _dal() as dal:
            outcome = file_case(
                dal,
                invoice_id=invoice_id,
                clerk_run_id=clerk_run_id,
                carrier_id=carrier_id,
                pin_date=datetime.now(UTC).date().isoformat(),
                amount=0.0,  # no amount-extraction step in this session's scope
                clerk_result=clerk_result,
            )
        return outcome

    outcome = await run_in_threadpool(_run_sync)
    await feed.broadcast(
        "clerk.filed",
        {"run_id": clerk_run_id, "case_id": outcome["case_id"], "verdict": "see /cases/{id}"},
    )


@app.get("/invoices")
def list_invoices() -> dict:
    with _dal() as dal:
        rows = dal.execute(
            """
            SELECT i.id, c.scac, c.name, i.amount, f.verdict, f.cited_rule,
                   i.received_at, ca.id
            FROM invoices i
            JOIN carriers c ON c.tenant_id = i.tenant_id AND c.id = i.carrier_id
            LEFT JOIN cases ca ON ca.tenant_id = i.tenant_id AND ca.invoice_id = i.id
            LEFT JOIN findings f ON f.tenant_id = i.tenant_id AND f.id = ca.finding_id
            WHERE i.tenant_id = %s
            ORDER BY i.received_at DESC
            LIMIT 50;
            """,
            (),
            tag="intake.list",
        )
    items = [
        {
            "id": str(r[0]),
            "carrier": {"scac": r[1], "name": r[2]},
            "container_no": None,
            "amount": float(r[3]) if r[3] is not None else None,
            "verdict": r[4],
            "cited_rule": r[5],
            "window": None,
            "evidence_inventory": None,
            "received_at": r[6].isoformat() if r[6] else None,
            "case_id": str(r[7]) if r[7] else None,
        }
        for r in rows
    ]
    return {"items": items, "next_cursor": None}


@app.get("/cases/{case_id}")
def get_case(case_id: str) -> dict:
    """Provisional shape: no worked TDD example exists for this route
    (contract/fixtures/README.md flags it). Shaped from §3.2's prose field
    list (case + finding + invoice summary + evidence list); escalate to
    strategy before treating this as frozen."""
    with _dal() as dal:
        rows = dal.execute(
            """
            SELECT ca.id, ca.state, ca.pin_date, ca.amount, ca.invoice_id,
                   f.verdict, f.cited_rule, f.summary, ca.decision_reason
            FROM cases ca
            LEFT JOIN findings f ON f.tenant_id = ca.tenant_id AND f.id = ca.finding_id
            WHERE ca.tenant_id = %s AND ca.id = %s;
            """,
            (case_id,),
            tag="case.get",
        )
    if not rows:
        raise HTTPException(status_code=404, detail="case not found")
    r = rows[0]
    return {
        "id": str(r[0]),
        "state": r[1],
        "pin_date": r[2].isoformat() if r[2] else None,
        "amount": float(r[3]) if r[3] is not None else None,
        "invoice_id": str(r[4]),
        "finding": {"verdict": r[5], "cited_rule": r[6], "summary": r[7]},
        "decision_reason": r[8],
    }


@app.get("/cases/{case_id}/replay")
def get_case_replay(
    case_id: str, _actor: AuthedActor = Depends(require_auth)
) -> dict:
    """Reconstruct a sealed case at its stored Cockroach HLC.

    The adapter derives the AOST value only from ``cases.sealed_txn_ts``,
    validates the live 90-day retention configuration, and has no current-read
    fallback when exact history is unavailable.
    """
    try:
        with _dal() as dal:
            return replay_case(dal, case_id=case_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="case_id must be a UUID") from None
    except ReplayNotFoundError:
        raise HTTPException(status_code=404, detail="case not found") from None
    except ReplayNotSealedError:
        raise HTTPException(status_code=409, detail="case has no sealed replay") from None
    except ReplayUnavailableError:
        raise HTTPException(status_code=503, detail="exact historical replay unavailable") from None
    except Exception:
        # Connection construction/close can fail outside replay_case's own
        # fail-closed adapter boundary. Never expose raw DSN/driver details.
        raise HTTPException(status_code=503, detail="exact historical replay unavailable") from None


@app.post("/cases/{case_id}/approve")
async def approve_case(case_id: str, actor: AuthedActor = Depends(require_auth)) -> dict:
    """POST /cases/{id}/approve: the seal (TDD §2.21-B). The only
    irreversible verb in the product. Executes seal_case's atomic
    transaction; idempotent on an already-FILED case (200 with
    already_sealed:true); 409 if state is CONTESTED/RESOLVED; 404 if the
    case doesn't exist.
    """

    def _run_sync() -> dict:
        with _dal() as dal:
            return seal_case(
                dal,
                case_id=case_id,
                sealed_by_user_id=actor.user_id,
                sealed_at_display=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            )

    try:
        result = await run_in_threadpool(_run_sync)
    except CaseNotFoundError:
        raise HTTPException(status_code=404, detail="case not found") from None
    except CaseNotSealableError as exc:
        raise HTTPException(
            status_code=409, detail=f"case is {exc.args[0]}, cannot be sealed"
        ) from None

    if result["already_sealed"]:
        await feed.broadcast(
            "case.sealed",
            {"case_id": case_id, "sealed_txn_ts": result["sealed_txn_ts"], "already_sealed": True},
        )
    else:
        await feed.broadcast(
            "case.sealed",
            {
                "case_id": case_id,
                "sealed_at_display": result["sealed_at_display"],
                "sealed_txn_ts": result["sealed_txn_ts"],
            },
        )

    return result


@app.websocket("/feed")
async def ws_feed(websocket: WebSocket) -> None:
    await feed.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # client sends nothing meaningful; just keep-alive
    except WebSocketDisconnect:
        feed.disconnect(websocket)
