from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agent_sherlock.domain import InboundMessage


@dataclass(frozen=True, slots=True)
class ConnectorBatch:
    """Messages and the provider checkpoint that produced them."""

    messages: tuple[InboundMessage, ...] = ()
    checkpoint: str | None = None
    initialized: bool = False
    history_reset: bool = False


class PollingConnector(Protocol):
    """Contract for sources such as Gmail that are checked incrementally."""

    @property
    def name(self) -> str: ...

    def poll(self) -> ConnectorBatch: ...

    def acknowledge(self, batch: ConnectorBatch) -> None: ...
