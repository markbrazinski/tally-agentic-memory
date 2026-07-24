"""Controlled demonstration mail provider for Gate 6.

Delivers ONLY to the controlled demonstration inbox — never an arbitrary or
inferred external address. Idempotent on a provider idempotency key: a retry
with the same key returns the same message ID and never duplicates delivery.

This is a controlled demonstration provider. A real external send to an
owner-approved recipient/provider is a separate, explicitly-authorized step
(see the Gate 6 report). This adapter does not reach any real external mailbox.
"""

from __future__ import annotations

from dataclasses import dataclass

CONTROLLED_INBOX = "disputes-demo@controlled.example"
RECIPIENT_CLASS = "CONTROLLED_DEMONSTRATION_INBOX"
ACK_DISCLAIMER = (
    "Delivery acknowledgement does not indicate carrier receipt or acceptance."
)


class ControlledSendError(RuntimeError):
    """The controlled provider could not acknowledge the send."""


@dataclass(frozen=True)
class ControlledSendReceipt:
    provider_message_id: str
    recipient_class: str
    duplicate: bool
    disclaimer: str


class DemonstrationInboxProvider:
    """In-process controlled provider. Records deliveries by idempotency key.

    A production controlled provider would call an approved mail service; this
    one returns a ``demo-`` acknowledgement so the gated-send path can be proven
    end to end without an external mailbox. Never expands the recipient.
    """

    def __init__(self, *, fail: bool = False):
        self._delivered: dict[str, str] = {}
        self._fail = fail

    def send(
        self, *, provider_idempotency_key: str, subject: str, body: str,
        recipient_class: str,
    ) -> ControlledSendReceipt:
        if recipient_class != RECIPIENT_CLASS:
            raise ControlledSendError("RECIPIENT_NOT_CONTROLLED")
        if self._fail:
            raise ControlledSendError("PROVIDER_UNAVAILABLE")
        existing = self._delivered.get(provider_idempotency_key)
        if existing is not None:
            return ControlledSendReceipt(existing, RECIPIENT_CLASS, True, ACK_DISCLAIMER)
        message_id = f"demo-{provider_idempotency_key[:16]}"
        self._delivered[provider_idempotency_key] = message_id
        return ControlledSendReceipt(message_id, RECIPIENT_CLASS, False, ACK_DISCLAIMER)
