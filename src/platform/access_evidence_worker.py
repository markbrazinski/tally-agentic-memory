"""Worker entry for the gap-driven BIND_ACCESS_EVIDENCE task.

Leases one runnable task and runs the verify-record-bind-requeue transaction.
Thin by design — all the invariants (verify before flip, durable verdict first,
fail closed, idempotent) live in access_evidence_repository. Returns the binding
result, or None when there is no runnable task.
"""

from __future__ import annotations

from src.external.dal import DAL
from src.platform.access_evidence_repository import (
    AccessBindingResult,
    claim_next_access_evidence_task,
    complete_access_binding,
)


def run_one_access_evidence_task(
    dal: DAL, *, worker_id: str
) -> AccessBindingResult | None:
    lease = claim_next_access_evidence_task(dal, worker_id=worker_id)
    if lease is None:
        return None
    return complete_access_binding(dal, lease=lease)
