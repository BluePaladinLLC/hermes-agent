"""Actionable A2A receiver tick.

This is the product step after mechanical ACK: claim one inbound packet,
acknowledge it, optionally mark work as started, run an injected bounded action
handler, send a typed reply packet back to the sender, then complete/XACK the
original. It intentionally does not start a daemon, call /v1/runs by itself,
post to Discord, or mutate Kanban.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

_REPLY_TYPES = {"question", "work_result", "needs_human", "final", "nack"}
_NON_ACTIONABLE_MESSAGE_TYPES = {"ack", "nack", "work_started", "work_result", "needs_human", "final", "answer"}
_NO_REPLY_MARKERS = {"final-no-reply", "no-reply"}
Handler = Callable[[dict[str, Any]], "A2AActionResult | Mapping[str, Any] | str | None"]


@dataclass(frozen=True)
class A2AActionResult:
    """Typed reply produced by an actionable A2A handler."""

    message_type: str = "final"
    status: str = "completed"
    body: str = ""
    subject: str = ""
    evidence_links: Sequence[str] = field(default_factory=list)


def process_actionable_once(
    *,
    store: Any,
    target: str,
    consumer: str,
    handlers: Mapping[str, Handler],
    block_ms: int = 1,
) -> dict[str, Any] | None:
    """Process one inbound A2A packet as an actionable handoff.

    Returns None when no message is available. The handler is injected so this
    primitive can be tested and staged without wiring live /v1/runs execution.
    """

    message = store.claim(target=target, consumer=consumer, block_ms=max(1, int(block_ms or 1)))
    if not message:
        return None

    sender = str(message.get("sender") or "").strip()
    message_id = str(message.get("message_id") or "")
    topic_id = str(message.get("topic_id") or "")
    subject = str(message.get("subject") or "A2A packet").strip() or "A2A packet"
    message_type = str(message.get("message_type") or "").lower()

    if not sender:
        original = store.complete(message_id, status="needs_human", result="Cannot process: original sender is missing")
        return {"original": original, "ack": None, "started": None, "reply": None}

    if message_type in _NON_ACTIONABLE_MESSAGE_TYPES:
        result = f"Observed non-actionable A2A message_type={message_type or 'unknown'}; no reply sent."
        original = store.complete(message_id, status="completed", result=result)
        return {"original": original, "ack": None, "started": None, "reply": None}

    no_reply = _is_no_reply_message(message)
    ack = None
    if not no_reply:
        ack = _enqueue_reply(
            store,
            sender=target,
            target=sender,
            message_type="ack",
            topic_id=topic_id,
            subject=f"ACK: {subject}",
            body=f"ACK: {target} received {subject}.",
            idempotency_key=f"actionable:ack:{topic_id}:{message_id}:{target}:{sender}",
        )

    handler = handlers.get(message_type)
    if not handler:
        action = A2AActionResult(
            message_type="needs_human",
            status="needs_human",
            subject=f"Needs human: {subject}",
            body=f"No handler registered for message_type={message_type or 'unknown'}",
        )
        reply = None
        if not no_reply:
            reply = _send_action_reply(store, action, message=message, sender=target, target=sender)
        original = store.complete(message_id, status="needs_human", result=action.body, evidence_links=action.evidence_links)
        return {"original": original, "ack": ack, "started": None, "reply": reply}

    started = None
    if message_type == "work_request" and not no_reply:
        started = _enqueue_reply(
            store,
            sender=target,
            target=sender,
            message_type="work_started",
            topic_id=topic_id,
            subject=f"Started: {subject}",
            body=f"{target} started {subject}.",
            idempotency_key=f"actionable:started:{topic_id}:{message_id}:{target}:{sender}",
        )

    try:
        action = _normalize_action_result(handler(message))
    except Exception as exc:  # noqa: BLE001 - handler boundary converts failures into visible packets
        action = A2AActionResult(
            message_type="needs_human",
            status="failed",
            subject=f"Failed: {subject}",
            body=f"Handler failed: {exc}",
        )

    reply = None
    if not no_reply:
        reply = _send_action_reply(store, action, message=message, sender=target, target=sender)
    original = store.complete(
        message_id,
        status=action.status,
        result=action.body,
        evidence_links=action.evidence_links,
    )
    return {"original": original, "ack": ack, "started": started, "reply": reply}


def _is_no_reply_message(message: Mapping[str, Any]) -> bool:
    """Return True when the packet explicitly asks receivers not to answer."""

    subject = str(message.get("subject") or "").lower()
    body = str(message.get("body") or "").lower()
    if any(marker in subject or marker in body for marker in _NO_REPLY_MARKERS):
        return True

    payload = message.get("payload") or {}
    if isinstance(payload, Mapping):
        if payload.get("no_reply") is True or payload.get("final_no_reply") is True:
            return True
        delivery = payload.get("delivery") or {}
        if isinstance(delivery, Mapping) and delivery.get("no_reply") is True:
            return True
    return False


def _normalize_action_result(value: A2AActionResult | Mapping[str, Any] | str | None) -> A2AActionResult:
    if isinstance(value, A2AActionResult):
        return _clean_action_result(value)
    if isinstance(value, Mapping):
        return _clean_action_result(
            A2AActionResult(
                message_type=str(value.get("message_type") or "final"),
                status=str(value.get("status") or "completed"),
                subject=str(value.get("subject") or ""),
                body=str(value.get("body") or value.get("result") or ""),
                evidence_links=list(value.get("evidence_links") or []),
            )
        )
    if value is None:
        return A2AActionResult(message_type="final", status="completed", body="completed")
    return A2AActionResult(message_type="final", status="completed", body=str(value))


def _clean_action_result(action: A2AActionResult) -> A2AActionResult:
    message_type = action.message_type.lower().strip()
    if message_type not in _REPLY_TYPES:
        message_type = "final"
    return A2AActionResult(
        message_type=message_type,
        status=action.status or "completed",
        subject=action.subject,
        body=action.body or action.status or "completed",
        evidence_links=list(action.evidence_links or []),
    )


def _send_action_reply(
    store: Any,
    action: A2AActionResult,
    *,
    message: Mapping[str, Any],
    sender: str,
    target: str,
) -> dict[str, Any]:
    subject = action.subject or f"{action.message_type}: {message.get('subject') or 'A2A packet'}"
    return _enqueue_reply(
        store,
        sender=sender,
        target=target,
        message_type=action.message_type,
        topic_id=str(message.get("topic_id") or ""),
        subject=subject,
        body=action.body,
        payload={"evidence_links": list(action.evidence_links or [])},
        idempotency_key=(
            f"actionable:reply:{message.get('topic_id') or ''}:{message.get('message_id') or ''}:"
            f"{sender}:{target}:{action.message_type}"
        ),
    )


def _enqueue_reply(
    store: Any,
    *,
    sender: str,
    target: str,
    message_type: str,
    topic_id: str,
    subject: str,
    body: str,
    idempotency_key: str,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return store.enqueue(
        sender=sender,
        targets=[target],
        message_type=message_type,
        topic_id=topic_id,
        subject=subject,
        body=body or message_type,
        payload=payload or {},
        idempotency_key=idempotency_key,
    )
