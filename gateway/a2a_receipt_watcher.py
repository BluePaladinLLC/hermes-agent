"""Dry-run A2A receipt watcher primitives.

This module reads lifecycle events from Valkey/Redis Streams and renders the
compact text that a later Discord bridge may post. It does not send messages;
callers own delivery and cursor persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gateway.a2a_receipts import format_receipt_batch


@dataclass(frozen=True)
class ReceiptPollResult:
    """Result of polling a receipt event stream."""

    text: str
    next_id: str
    event_count: int


def poll_receipt_batch(
    client: Any,
    *,
    last_id: str = "0-0",
    stream: str = "cortex:coordination",
    count: int = 25,
    block_ms: int = 0,
    max_lines: int | None = None,
) -> ReceiptPollResult:
    """Read new receipt events and render a compact batch.

    Empty output means unchanged/silent; this is intentional for cron/watchdog
    use where unchanged ticks should not notify Bruno.
    """

    safe_count = max(1, int(count or 1))
    raw = client.xread({stream: last_id}, count=safe_count, block=max(0, int(block_ms or 0)))
    events: list[tuple[str, dict[str, Any]]] = []
    next_id = last_id
    for _stream_name, entries in raw or []:
        for entry_id, fields in entries or []:
            event_id = str(entry_id)
            next_id = event_id
            decoded = {
                str(key): value.decode() if isinstance(value, bytes) else value
                for key, value in dict(fields or {}).items()
            }
            events.append((event_id, decoded))
    if not events:
        return ReceiptPollResult(text="", next_id=last_id, event_count=0)
    text = format_receipt_batch(events, max_lines=max_lines if max_lines is not None else safe_count)
    return ReceiptPollResult(text=text, next_id=next_id, event_count=len(events))
