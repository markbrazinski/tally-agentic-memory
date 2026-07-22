"""Unit tests for src/platform/auth.py.

Per CLAUDE.md ("zero network calls in the test suite"), src.platform.auth.get_demo_token
and the users.id lookup are patched - no real SSM/CockroachDB call happens here.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from src.platform.auth import DEMO_ACTOR_EMAIL, make_require_bearer_auth

TENANT_ID = "10000000-0000-4000-8000-000000000002"
RACHEL_USER_ID = "00000000-0000-0000-0000-000000000042"


def _dep():
    return make_require_bearer_auth(TENANT_ID)


def test_missing_authorization_header_is_401():
    dep = _dep()
    with pytest.raises(HTTPException) as exc_info:
        dep(authorization=None)
    assert exc_info.value.status_code == 401


def test_malformed_authorization_header_is_401():
    dep = _dep()
    with pytest.raises(HTTPException) as exc_info:
        dep(authorization="NotBearer sometoken")
    assert exc_info.value.status_code == 401


def test_wrong_token_is_401():
    dep = _dep()
    with patch("src.platform.auth.get_demo_token", return_value="correct-token"):
        with pytest.raises(HTTPException) as exc_info:
            dep(authorization="Bearer wrong-token")
    assert exc_info.value.status_code == 401


def test_correct_token_resolves_to_rachel_user_id():
    dep = _dep()
    with (
        patch("src.platform.auth.get_demo_token", return_value="correct-token"),
        patch("src.platform.auth._resolve_rachel_user_id", return_value=RACHEL_USER_ID),
    ):
        actor = dep(authorization="Bearer correct-token")

    assert actor.user_id == RACHEL_USER_ID
    assert actor.display_name == "rachel.martinez"


def test_token_with_extra_whitespace_still_matches():
    dep = _dep()
    with (
        patch("src.platform.auth.get_demo_token", return_value="correct-token"),
        patch("src.platform.auth._resolve_rachel_user_id", return_value=RACHEL_USER_ID),
    ):
        actor = dep(authorization="Bearer   correct-token  ")

    assert actor.user_id == RACHEL_USER_ID


def test_demo_actor_email_matches_seed_demo_tenant_constant():
    """Regression guard: auth.py's DEMO_ACTOR_EMAIL must match
    seed_demo_tenant.py's DEMO_USER email exactly, or the lookup silently
    finds no row for a real deployed environment."""
    from src.external.seed_demo_tenant import DEMO_USER

    assert DEMO_ACTOR_EMAIL == DEMO_USER["email"]
