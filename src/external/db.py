"""CockroachDB connection layer (psycopg 3, raw SQL, no ORM).

Per CLAUDE.md's Architecture: "src/external — adapters: CockroachDB DAL
(psycopg 3, raw SQL, retry-on-40001, tenant injection, query-log
middleware)." This module ships the connection + retry piece; the query-log
middleware and tenant-injection helpers land alongside the routes/DAL calls
that need them (Bundle 0's job per bundle-r.md Session 3's own scope: "not
the full schema; that's Bundle 0's job and stays there").

CockroachDB's SERIALIZABLE isolation can abort a transaction under
contention with SQLSTATE 40001 ("restart transaction") - the standard,
expected response is a client-side retry with backoff, not a hard failure.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import TypeVar
from urllib.parse import quote

import psycopg
from psycopg import Connection

RETRYABLE_SQLSTATE = "40001"
DEFAULT_MAX_RETRIES = 5
DEFAULT_BASE_DELAY_SECONDS = 0.1


SSM_DSN_PARAMETER_NAME = "/example/tally/crdb-dsn"

# CockroachDB Cloud's CA cert, bundled alongside this module so it ships
# inside the Lambda deployment zip (deploy.sh copies src/ wholesale) - a
# Lambda's filesystem is ephemeral/per-invocation-environment, so pointing
# at a path like ~/.postgresql/root.crt (the psycopg/libpq default, which a
# local dev machine can populate once by hand) doesn't work in production;
# the cert has to travel with the code. Resolved relative to this file so
# it works both locally (src/external/cockroachdb-ca.crt) and in Lambda
# (/var/task/src/external/cockroachdb-ca.crt).
_CA_CERT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cockroachdb-ca.crt")


def _dsn_with_ca_cert(dsn: str) -> str:
    """Append sslrootcert=<bundled CA cert> to a DSN, unless already set.

    Uses "&" if the DSN already has query params (it always does - at
    minimum sslmode=verify-full), "?" otherwise. Does nothing if the DSN
    already specifies sslrootcert explicitly (respects an operator's
    override) or if the bundled cert file isn't present for some reason
    (falls back to whatever psycopg/libpq's own default resolution finds -
    better to attempt the connection than refuse to try).
    """
    if "sslrootcert=" in dsn or not os.path.exists(_CA_CERT_PATH):
        return dsn
    separator = "&" if "?" in dsn else "?"
    # URI query values must be percent-encoded - a local checkout path can
    # contain spaces or other reserved characters (e.g. this very repo's own
    # path has a space in it), which psycopg's conninfo URI parser rejects
    # unescaped.
    return f"{dsn}{separator}sslrootcert={quote(_CA_CERT_PATH, safe='')}"


def get_dsn() -> str:
    """Get the CockroachDB connection string: SSM SecureString first, env var fallback.

    Production (the deployed Lambdas) reads /example/tally/crdb-dsn from SSM
    Parameter Store (SecureString, KMS-encrypted with the account's default
    SSM key) rather than a plain Lambda environment variable - a connection
    string with an embedded password should not sit in plaintext in the
    Lambda console/API. Local dev keeps using TALLY_CRDB_DSN from .env
    (gitignored) via the env-var fallback, so nothing changes for anyone
    running this locally without AWS credentials in front of them.

    Tries SSM first; falls back to the env var only if the SSM read fails
    for any reason (no boto3, no AWS credentials, parameter doesn't exist,
    etc.) - this makes local dev work without needing AWS access at all,
    while production (which has both the SSM parameter and the IAM
    permission to read it) prefers the more secure source.

    Raises:
        RuntimeError: if neither SSM nor TALLY_CRDB_DSN produces a value -
            fail loudly at startup rather than produce a confusing
            connection error later.
    """
    try:
        import boto3

        ssm = boto3.client("ssm")
        response = ssm.get_parameter(Name=SSM_DSN_PARAMETER_NAME, WithDecryption=True)
        return response["Parameter"]["Value"]
    except Exception:  # noqa: BLE001 - any SSM failure falls back to the env var
        pass

    dsn = os.environ.get("TALLY_CRDB_DSN")
    if not dsn:
        raise RuntimeError(
            "No CockroachDB connection string available: SSM parameter "
            f"{SSM_DSN_PARAMETER_NAME} could not be read, and TALLY_CRDB_DSN "
            "is not set. Set the env var (see .env, gitignored) for local "
            "dev, or ensure the SSM parameter exists and this principal can "
            "read it in production."
        )
    return dsn


def connect(dsn: str | None = None) -> Connection:
    """Open one CockroachDB connection. Caller owns closing/context-managing it.

    autocommit=True: psycopg 3 defaults to autocommit=False, meaning ANY
    query - even a plain read like `load_carrier_id_by_scac`'s SELECT -
    opens an implicit transaction that stays open until something
    explicitly commits or rolls it back. Left on its default, a read-only
    helper silently leaves the connection straddling an open transaction;
    a later `run_with_retry` call's `conn.transaction()` then nests INSIDE
    that leftover transaction as a savepoint rather than opening its own
    commit boundary, so its inserts are never actually committed - they
    vanish the moment the connection closes (found via a real end-to-end
    smoke test against the live cluster: rows were visible within the same
    connection but gone from a fresh one). autocommit=True makes plain
    reads/writes execute and land immediately with no dangling state;
    `conn.transaction()` still opens a real multi-statement transaction
    when explicitly called (psycopg 3 supports this combination natively),
    which is the only place true atomicity is actually needed
    (commit_source_day's linked snapshot+recording inserts).
    """
    return psycopg.connect(_dsn_with_ca_cert(dsn or get_dsn()), autocommit=True)


T = TypeVar("T")


def run_with_retry(
    conn: Connection,
    fn: Callable[[Connection], T],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS,
) -> T:
    """Run `fn(conn)` inside a transaction, retrying on SQLSTATE 40001.

    CockroachDB's documented pattern for serialization-restart errors:
    retry the whole transaction body with exponential backoff. `fn` must
    be safe to re-run from scratch (no partial side effects outside the
    transaction) - callers should do all their work through `conn` inside
    `fn`, never partially outside it.

    Raises the last exception if `max_retries` is exhausted, or
    immediately re-raises any non-40001 error without retrying.
    """
    attempt = 0
    while True:
        try:
            with conn.transaction():
                return fn(conn)
        except psycopg.errors.SerializationFailure as exc:
            sqlstate = exc.sqlstate
            attempt += 1
            if sqlstate != RETRYABLE_SQLSTATE or attempt > max_retries:
                raise
            time.sleep(base_delay_seconds * (2**(attempt - 1)))
