"""One bounded Gate 4 judgment worker iteration (pure arithmetic; no model)."""

from __future__ import annotations

from src.external.dal import DAL
from src.platform.judgment_repository import (
    JudgmentCompletion,
    claim_next_judgment_task,
    complete_judgment,
    fail_judgment,
    load_day_inputs,
)


def run_one_judgment_task(dal: DAL, *, worker_id: str) -> JudgmentCompletion | None:
    lease = claim_next_judgment_task(dal, worker_id=worker_id)
    if lease is None:
        return None
    days = load_day_inputs(dal, reconstruction_id=lease.reconstruction_id)
    if not days:
        fail_judgment(dal, lease=lease, error_code="NO_CHARGED_DAYS")
        return None
    try:
        return complete_judgment(dal, lease=lease, days=days)
    except ValueError as exc:
        fail_judgment(dal, lease=lease, error_code=str(exc))
        return None
