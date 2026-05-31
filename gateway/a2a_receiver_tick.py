"""Bounded A2A receiver tick loop.

The receiver tick repeatedly calls the actionable receiver primitive until it
processes a configured number of messages or observes a configured number of
empty inbox checks. It is intentionally boring: no daemon ownership, no Discord,
no /v1/runs wiring, and no Kanban mutation. Service scheduling remains a rollout
choice outside this module.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any, Mapping

from gateway.a2a_actionable_receiver import Handler, ReceiptJournal, process_actionable_once


def run_receiver_tick(
    *,
    store: Any,
    target: str,
    consumer: str,
    handlers: Mapping[str, Handler],
    capabilities: Iterable[str] | None = None,
    receipt_journal: ReceiptJournal | None = None,
    max_messages: int = 10,
    max_idle_ticks: int = 1,
    block_ms: int = 1,
    idle_sleep_seconds: float = 0.0,
) -> dict[str, Any]:
    """Run a bounded receiver tick and return a compact summary.

    `max_messages` prevents one tick from becoming an unbounded worker.
    `max_idle_ticks` lets a scheduled tick check the inbox and exit cleanly when
    no work exists. The optional `receipt_journal` is local-only plumbing for
    compact A2A receipts; callers decide whether to write it to a file, stream,
    or later gated channel poster.
    """

    max_messages = max(1, int(max_messages or 1))
    max_idle_ticks = max(1, int(max_idle_ticks or 1))
    block_ms = max(1, int(block_ms or 1))
    idle_sleep_seconds = max(0.0, float(idle_sleep_seconds or 0.0))

    processed = 0
    idle_ticks = 0
    results: list[dict[str, Any]] = []
    stopped_reason = "idle"

    while processed < max_messages and idle_ticks < max_idle_ticks:
        result = process_actionable_once(
            store=store,
            target=target,
            consumer=consumer,
            handlers=handlers,
            capabilities=capabilities,
            receipt_journal=receipt_journal,
            block_ms=block_ms,
        )
        if result is None:
            idle_ticks += 1
            if idle_sleep_seconds and idle_ticks < max_idle_ticks:
                time.sleep(idle_sleep_seconds)
            continue
        processed += 1
        idle_ticks = 0
        results.append(result)

    if processed >= max_messages:
        stopped_reason = "max_messages"
    return {"processed": processed, "idle_ticks": idle_ticks, "stopped_reason": stopped_reason, "results": results}
