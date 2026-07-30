from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """A message normalized by an input connector."""

    source: str
    account_id: str
    external_id: str
    conversation_id: str
    sender: str
    subject: str
    body: str
    received_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("source", "account_id", "external_id"):
            value = getattr(self, field_name)
            if not value or not value.strip():
                raise ValueError(f"{field_name} must not be empty")
        if self.received_at.tzinfo is None:
            raise ValueError("received_at must include a timezone")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.source, self.account_id, self.external_id
