"""Real controlled SES sender for the demonstration send (Demo v3, P5).

Conforms to the same interface + receipt shape as ``DemonstrationInboxProvider``
(``send(*, provider_idempotency_key, subject, body, recipient_class)`` ->
``ControlledSendReceipt``) so the gated-send repository call site is unchanged.

Design (owner-confirmed):
- The SES TRANSPORT envelope (From + To) is a single server-controlled verified
  identity read from SSM (sandbox self-send). The browser never supplies or sees
  it. The fictional business From/To live only in the sealed correspondence
  record, never in the transport.
- Idempotent by ``provider_idempotency_key`` (the sealed send-intent id): SES
  requests are not natively idempotent, so the durable ``send_attempts`` row is
  the idempotency authority (see correspondence_repository); this adapter is
  called at most once per committed intent by the delivery worker.
- Only the controlled recipient class is accepted — never an inferred external
  address. Delivery acknowledgement is not carrier receipt or acceptance.

Sandbox note: SES sandbox delivers only between VERIFIED identities, so the one
transport address is both sender and recipient.
"""

from __future__ import annotations

import os

from src.external.controlled_mail import (
    ACK_DISCLAIMER,
    RECIPIENT_CLASS,
    ControlledSendError,
    ControlledSendReceipt,
)

# Server-only SSM parameters (String; not secrets). Read via the same boundary as
# the app's other config. The browser never receives these.
SSM_TRANSPORT_PARAM = "/example/tally/ses-transport-address"
SSM_SENDER_PARAM = "/example/tally/ses-sender-address"


class SesControlledMailProvider:
    """boto3 SES sandbox sender. Transport From/To = one verified SSM address."""

    def __init__(self, *, ses_client=None, ssm_client=None, region: str | None = None):
        self._region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._ses = ses_client
        self._ssm = ssm_client
        self._transport: str | None = None
        self._sender: str | None = None

    # --- lazy AWS clients so unit tests can inject fakes / run offline ---
    def _ses_client(self):
        if self._ses is None:
            import boto3

            self._ses = boto3.client("ses", region_name=self._region)
        return self._ses

    def _resolve_addresses(self) -> tuple[str, str]:
        """(sender, recipient) transport addresses from SSM. Cached per instance."""
        if self._transport is not None and self._sender is not None:
            return self._sender, self._transport
        if self._ssm is None:
            import boto3

            self._ssm = boto3.client("ssm", region_name=self._region)
        # Transport address is required; sender defaults to it (sandbox self-send).
        transport = self._get_param(SSM_TRANSPORT_PARAM)
        if not transport:
            raise ControlledSendError("SES_TRANSPORT_ADDRESS_UNCONFIGURED")
        sender = self._get_param(SSM_SENDER_PARAM) or transport
        self._transport, self._sender = transport, sender
        return sender, transport

    def _get_param(self, name: str) -> str | None:
        try:
            resp = self._ssm.get_parameter(Name=name, WithDecryption=True)
            return resp["Parameter"]["Value"]
        except Exception:
            return None

    def send(
        self, *, provider_idempotency_key: str, subject: str, body: str,
        recipient_class: str,
    ) -> ControlledSendReceipt:
        # Never expand the recipient: only the controlled demonstration class.
        if recipient_class != RECIPIENT_CLASS:
            raise ControlledSendError("RECIPIENT_NOT_CONTROLLED")
        sender, recipient = self._resolve_addresses()
        try:
            resp = self._ses_client().send_email(
                Source=sender,
                Destination={"ToAddresses": [recipient]},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                },
                # Tag the send so the intent id is auditable in SES too.
                Tags=[{"Name": "send_intent", "Value": provider_idempotency_key[:256]}],
            )
        except ControlledSendError:
            raise
        except Exception as exc:  # boto/ClientError -> fail closed, never fabricate
            raise ControlledSendError("SES_SEND_FAILED") from exc
        message_id = resp.get("MessageId")
        if not message_id:
            raise ControlledSendError("SES_NO_MESSAGE_ID")
        return ControlledSendReceipt(
            provider_message_id=message_id,
            recipient_class=RECIPIENT_CLASS,
            duplicate=False,
            disclaimer=ACK_DISCLAIMER,
        )
