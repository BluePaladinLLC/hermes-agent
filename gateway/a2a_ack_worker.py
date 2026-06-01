"""Mechanical A2A inbox ACK worker.

This primitive is intentionally small: claim one inbound A2A packet, write an
`ack` packet back to the sender, and complete/ACK the original stream entry. It
is the missing actionability bridge between "message exists in an inbox" and
"the target runtime has looked at it and toggled a visible acknowledgement".

It does not call /v1/runs, post to Discord, mutate Kanban, or start a daemon.
Scheduling/wakeup remains a separate rollout gate.
"""

from __future__ import annotations

from typing import Any


def process_ack_once(
    *,
    store: Any,
    target: str,
    consumer: str,
    ack_body: str = "",
) -> dict[str, Any] | None:
    """Claim one inbound message and mechanically ACK it back to sender.

    Returns a small result payload containing the completed original state and
    the queued ACK state. Returns ``None`` when the inbox has no message.
    """

    claimed = store.claim(target=target, consumer=consumer, block_ms=1)
    if not claimed:
        return None

    sender = str(claimed.get("sender") or "").strip()
    if not sender:
        original = store.complete(
            claimed["message_id"],
            status="needs_human",
            result="Cannot ACK: original sender is missing",
        )
        return {"original": original, "ack": None}

    subject = str(claimed.get("subject") or "A2A packet").strip() or "A2A packet"
    message_id = str(claimed.get("message_id") or "")
    topic_id = str(claimed.get("topic_id") or "")
    body = ack_body or f"ACK: {target} saw {subject} and toggled mechanical inbox acknowledgement."

    ack = store.enqueue(
        sender=target,
        targets=[sender],
        message_type="ack",
        topic_id=topic_id,
        subject=f"ACK: {subject}",
        body=body,
        idempotency_key=f"ack:{topic_id}:{message_id}:{target}:{sender}",
    )
    original = store.complete(
        message_id,
        status="completed",
        result=f"ACK sent to {sender}",
        evidence_links=[f"a2a:{topic_id}"] if topic_id else [],
    )
    return {"original": original, "ack": ack}
