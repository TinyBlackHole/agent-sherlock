from __future__ import annotations

from datetime import UTC, datetime

from agent_sherlock.connectors.base import ConnectorBatch
from agent_sherlock.domain import InboundMessage
from agent_sherlock.integrations.gmail import (
    GmailError,
    GmailFetchPlan,
    GmailMailbox,
    GmailMessage,
    GmailPaths,
    commit_gmail_fetch,
    prepare_gmail_fetch,
)


class GmailConnectorError(GmailError):
    """Raised when a Gmail connector batch is acknowledged out of order."""


class GmailConnector:
    """Normalize incremental Gmail history without advancing it prematurely."""

    name = "gmail"

    def __init__(self, mailbox: GmailMailbox, *, paths: GmailPaths | None = None):
        self.mailbox = mailbox
        self.paths = paths or GmailPaths.default()
        self._pending: tuple[ConnectorBatch, GmailFetchPlan] | None = None

    def poll(self) -> ConnectorBatch:
        if self._pending is not None:
            return self._pending[0]

        plan = prepare_gmail_fetch(self.mailbox, paths=self.paths)
        checkpoint = plan.next_state.history_id
        account_id = plan.next_state.email_address or "me"
        messages = [
            _normalize_message(message, account_id=account_id)
            for message in plan.result.messages
        ]
        messages.sort(
            key=lambda message: (
                message.received_at,
                message.external_id,
            )
        )
        batch = ConnectorBatch(
            messages=tuple(messages),
            checkpoint=checkpoint,
            initialized=plan.result.initialized,
            history_reset=plan.result.history_reset,
        )
        self._pending = (batch, plan)
        return batch

    def acknowledge(self, batch: ConnectorBatch) -> None:
        if batch.checkpoint is None:
            return
        if self._pending is None or self._pending[0] != batch:
            raise GmailConnectorError(
                "Gmail connector batch is not pending acknowledgement."
            )
        plan = self._pending[1]
        commit_gmail_fetch(plan)
        self._pending = None


def _normalize_message(
    message: GmailMessage,
    *,
    account_id: str,
) -> InboundMessage:
    internal_date = max(message.internal_date, 0)
    received_at = datetime.fromtimestamp(internal_date / 1_000, tz=UTC)
    return InboundMessage(
        source="gmail",
        account_id=account_id,
        external_id=message.message_id,
        conversation_id=message.thread_id,
        sender=message.sender,
        subject=message.subject,
        body=message.body,
        received_at=received_at,
        metadata={"date_header": message.date},
    )
