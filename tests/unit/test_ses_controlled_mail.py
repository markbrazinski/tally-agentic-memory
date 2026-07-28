"""SES controlled-sender unit tests (Demo v3 P5). Zero network — fake clients.

Proves: transport From/To come from SSM (browser never supplies them); only the
controlled recipient class is accepted; the real SES MessageId is returned;
fail-closed when SSM is unconfigured or SES errors.
"""

from __future__ import annotations

import pytest

from src.external.controlled_mail import RECIPIENT_CLASS, ControlledSendError
from src.external.ses_controlled_mail import (
    SSM_SENDER_PARAM,
    SSM_TRANSPORT_PARAM,
    SesControlledMailProvider,
)

ADDR = "demo@example.test"


class FakeSsm:
    def __init__(self, params):
        self.params = params

    def get_parameter(self, Name, WithDecryption=False):  # noqa: N803 (boto API)
        if Name not in self.params:
            raise KeyError(Name)
        return {"Parameter": {"Value": self.params[Name]}}


class FakeSes:
    def __init__(self, message_id="0100-ses-real-id", raise_exc=None):
        self.message_id = message_id
        self.raise_exc = raise_exc
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc:
            raise self.raise_exc
        return {"MessageId": self.message_id}


def _provider(ses, params=None):
    return SesControlledMailProvider(
        ses_client=ses,
        ssm_client=FakeSsm(params if params is not None else {
            SSM_TRANSPORT_PARAM: ADDR,
        }),
    )


def test_send_uses_ssm_transport_and_returns_real_message_id():
    ses = FakeSes(message_id="0100-abc-real")
    p = _provider(ses)
    r = p.send(provider_idempotency_key="intent-123", subject="Adjustment request",
               body="…", recipient_class=RECIPIENT_CLASS)
    assert r.provider_message_id == "0100-abc-real"
    assert r.recipient_class == RECIPIENT_CLASS and r.duplicate is False
    # Transport From/To both the single verified SSM address (sandbox self-send).
    call = ses.calls[0]
    assert call["Source"] == ADDR
    assert call["Destination"]["ToAddresses"] == [ADDR]
    # The send-intent id is tagged for audit, not exposed as a recipient.
    assert any(t["Value"] == "intent-123" for t in call["Tags"])


def test_separate_sender_param_overrides_recipient():
    ses = FakeSes()
    p = _provider(ses, params={SSM_TRANSPORT_PARAM: "to@example.test",
                               SSM_SENDER_PARAM: "from@example.test"})
    p.send(provider_idempotency_key="k", subject="s", body="b",
           recipient_class=RECIPIENT_CLASS)
    call = ses.calls[0]
    assert call["Source"] == "from@example.test"
    assert call["Destination"]["ToAddresses"] == ["to@example.test"]


def test_non_controlled_recipient_class_refused_before_any_send():
    ses = FakeSes()
    p = _provider(ses)
    with pytest.raises(ControlledSendError, match="RECIPIENT_NOT_CONTROLLED"):
        p.send(provider_idempotency_key="k", subject="s", body="b",
               recipient_class="ARBITRARY_EXTERNAL")
    assert ses.calls == []  # never reached SES


def test_unconfigured_transport_fails_closed():
    ses = FakeSes()
    p = _provider(ses, params={})  # no SSM transport param
    with pytest.raises(ControlledSendError, match="SES_TRANSPORT_ADDRESS_UNCONFIGURED"):
        p.send(provider_idempotency_key="k", subject="s", body="b",
               recipient_class=RECIPIENT_CLASS)
    assert ses.calls == []


def test_ses_error_fails_closed_no_fabricated_id():
    ses = FakeSes(raise_exc=RuntimeError("throttled"))
    p = _provider(ses)
    with pytest.raises(ControlledSendError, match="SES_SEND_FAILED"):
        p.send(provider_idempotency_key="k", subject="s", body="b",
               recipient_class=RECIPIENT_CLASS)


def test_missing_message_id_fails_closed():
    ses = FakeSes(message_id=None)
    p = _provider(ses)
    with pytest.raises(ControlledSendError, match="SES_NO_MESSAGE_ID"):
        p.send(provider_idempotency_key="k", subject="s", body="b",
               recipient_class=RECIPIENT_CLASS)
