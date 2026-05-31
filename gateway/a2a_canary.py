"""Local A2A canaries for receiver lifecycle rollout.

These canaries exercise the A2A receiver primitives without network calls,
Discord posting, service ownership, gateway restarts, or Kanban mutation. They
model the Axon/Pons rollout gates in-process so the contract can be verified
before wiring real profile runtimes.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping
from uuid import uuid4

from gateway.a2a_actionable_receiver import A2AActionResult, Handler
from gateway.a2a_receiver_tick import run_receiver_tick


@dataclass
class A2ACanaryStore:
    """Tiny in-memory store with the receiver primitive's store interface."""

    queued: deque[dict[str, Any]] = field(default_factory=deque)
    completed: list[dict[str, Any]] = field(default_factory=list)
    enqueued: list[dict[str, Any]] = field(default_factory=list)

    def seed(
        self,
        *,
        sender: str,
        target: str,
        message_type: str,
        topic_id: str,
        subject: str,
        body: str = "",
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        packet = {
            "message_id": f"a2a_canary_{uuid4().hex[:12]}",
            "sender": sender,
            "target": target,
            "message_type": message_type,
            "topic_id": topic_id,
            "subject": subject,
            "body": body,
            "payload": dict(payload or {}),
        }
        self.queued.append(packet)
        return packet

    def claim(self, *, target: str, consumer: str, block_ms: int) -> dict[str, Any] | None:  # noqa: ARG002
        for _ in range(len(self.queued)):
            packet = self.queued.popleft()
            packet_target = packet.get("target") or _first_target(packet.get("targets"))
            if packet_target == target:
                packet["claimed_by"] = consumer
                return packet
            self.queued.append(packet)
        return None

    def enqueue(self, **kwargs: Any) -> dict[str, Any]:
        packet = {"message_id": f"a2a_canary_{uuid4().hex[:12]}", **kwargs}
        if "target" not in packet:
            packet["target"] = _first_target(packet.get("targets"))
        self.enqueued.append(packet)
        self.queued.append(packet)
        return packet

    def complete(self, message_id: str, **kwargs: Any) -> dict[str, Any]:
        record = {"message_id": message_id, **kwargs}
        self.completed.append(record)
        return record


def run_axon_pons_final_canary() -> dict[str, Any]:
    """Exercise Pons → Axon work request → ACK/start/final → Pons no-loop."""

    store = A2ACanaryStore()
    receipts: list[str] = []
    store.seed(
        sender="pons",
        target="axon",
        message_type="work_request",
        topic_id="a2a-canary-final",
        subject="A2A final canary",
        body="private canary body should not appear in receipts",
    )

    axon = _tick(
        store,
        target="axon",
        handlers={"work_request": lambda message: A2AActionResult(message_type="final", body="canary complete")},
        receipts=receipts,
    )
    pons = _tick(store, target="pons", handlers={}, receipts=receipts)

    return _summary("axon_pons_final", store=store, receipts=receipts, ticks=[axon, pons])


def run_axon_pons_clarification_canary() -> dict[str, Any]:
    """Exercise question → answer → final across Axon/Pons without lifecycle churn."""

    store = A2ACanaryStore()
    receipts: list[str] = []
    store.seed(
        sender="pons",
        target="axon",
        message_type="work_request",
        topic_id="a2a-canary-clarification",
        subject="A2A clarification canary",
        body="private canary body should not appear in receipts",
    )

    axon_question = _tick(
        store,
        target="axon",
        handlers={
            "work_request": lambda message: A2AActionResult(
                message_type="question",
                subject="Question: A2A clarification canary",
                body="Need the missing detail.",
            )
        },
        receipts=receipts,
    )
    pons_answer = _tick(
        store,
        target="pons",
        handlers={"question": lambda message: A2AActionResult(message_type="answer", body="Detail supplied.")},
        receipts=receipts,
    )
    axon_final = _tick(
        store,
        target="axon",
        handlers={"answer": lambda message: A2AActionResult(message_type="final", body="clarification closed")},
        receipts=receipts,
    )
    pons_close = _tick(store, target="pons", handlers={}, receipts=receipts)

    return _summary(
        "axon_pons_clarification",
        store=store,
        receipts=receipts,
        ticks=[axon_question, pons_answer, axon_final, pons_close],
    )


def _tick(store: A2ACanaryStore, *, target: str, handlers: Mapping[str, Handler], receipts: list[str]) -> dict[str, Any]:
    return run_receiver_tick(
        store=store,
        target=target,
        consumer=f"{target}-canary",
        handlers=handlers,
        receipt_journal=receipts.append,
        max_messages=10,
        max_idle_ticks=1,
    )


def _summary(name: str, *, store: A2ACanaryStore, receipts: list[str], ticks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": name,
        "ok": len(store.queued) == 0 and not any("private canary body" in line for line in receipts),
        "pending": len(store.queued),
        "completed": len(store.completed),
        "enqueued": len(store.enqueued),
        "receipt_count": len(receipts),
        "receipts": receipts,
        "tick_processed": [tick["processed"] for tick in ticks],
    }


def _first_target(value: Any) -> str:
    if isinstance(value, list) and value:
        return str(value[0])
    return str(value or "")
