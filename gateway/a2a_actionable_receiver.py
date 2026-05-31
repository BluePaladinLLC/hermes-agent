"""Actionable A2A receiver tick.

This module models A2A like a human inbox: an inbound packet is claimed,
acknowledged, dispositioned, and then closed. Only work-bearing packet types
wake handler execution. Lifecycle/status packets update state only and must not
create reply loops.

It intentionally does not start a daemon, call /v1/runs by itself, post to
Discord, restart services, or mutate Kanban.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

ACTIONABLE_MESSAGE_TYPES = frozenset({"work_request", "handoff", "consult", "question", "answer", "reply_required"})
LIFECYCLE_MESSAGE_TYPES = frozenset(
    {
        "ack",
        "nack",
        "read",
        "queued",
        "work_started",
        "needs_clarification",
        "needs_human",
        "work_result",
        "final",
        "failed",
        "timeout",
        "fyi_logged",
    }
)
REPLY_MESSAGE_TYPES = frozenset({"question", "answer", "work_result", "needs_human", "final", "nack"})
TERMINAL_STATUS_ALIASES = {
    "": "completed",
    "final": "completed",
    "done": "completed",
    "ok": "completed",
    "success": "completed",
    "succeeded": "completed",
    "complete": "completed",
    "completed": "completed",
    "needs_human": "needs_human",
    "blocked": "needs_human",
    "failed": "failed",
    "failure": "failed",
    "error": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}
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
    capabilities: Iterable[str] | None = None,
    block_ms: int = 1,
) -> dict[str, Any] | None:
    """Process one inbound A2A packet as an actionable handoff.

    Returns ``None`` when no message is available. The store dependency is a
    narrow inbox abstraction with ``claim()``, ``enqueue()``, and ``complete()``
    methods, keeping this primitive unit-testable and safe to stage before
    wiring live agent execution.
    """

    message = store.claim(target=target, consumer=consumer, block_ms=max(1, int(block_ms or 1)))
    if not message:
        return None

    sender = str(message.get("sender") or "").strip()
    message_id = str(message.get("message_id") or "")
    topic_id = str(message.get("topic_id") or "")
    subject = str(message.get("subject") or "A2A packet").strip() or "A2A packet"
    message_type = str(message.get("message_type") or "").lower().strip()

    if message_type not in ACTIONABLE_MESSAGE_TYPES:
        original = store.complete(
            message_id,
            status="completed",
            result=f"Ignored non-actionable A2A lifecycle/status packet: {message_type or 'unknown'}",
        )
        return {"original": original, "ack": None, "started": None, "reply": None, "ignored": True}

    if not sender:
        original = store.complete(message_id, status="needs_human", result="Cannot process: original sender is missing")
        return {"original": original, "ack": None, "started": None, "reply": None}

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

    requested_capabilities = _requested_capabilities(message)
    missing_capabilities = _missing_capabilities(requested_capabilities, capabilities)
    if missing_capabilities:
        action = A2AActionResult(
            message_type="needs_human",
            status="needs_human",
            subject=f"Needs capability: {subject}",
            body="Cannot process: missing capability " + ", ".join(missing_capabilities),
        )
        reply = _send_action_reply(
            store,
            action,
            message=message,
            sender=target,
            target=sender,
            payload={
                "requested_capabilities": requested_capabilities,
                "missing_capabilities": missing_capabilities,
            },
        )
        original = store.complete(message_id, status="needs_human", result=action.body, evidence_links=action.evidence_links)
        return {"original": original, "ack": ack, "started": None, "reply": reply}

    handler = _select_handler(handlers, message_type=message_type, requested_capabilities=requested_capabilities)
    if not handler:
        action = A2AActionResult(
            message_type="needs_human",
            status="needs_human",
            subject=f"Needs human: {subject}",
            body=f"No handler registered for message_type={message_type or 'unknown'}",
        )
        reply = _send_action_reply(store, action, message=message, sender=target, target=sender)
        original = store.complete(message_id, status="needs_human", result=action.body, evidence_links=action.evidence_links)
        return {"original": original, "ack": ack, "started": None, "reply": reply}

    started = None
    if message_type == "work_request":
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

    reply = _send_action_reply(store, action, message=message, sender=target, target=sender)
    original = store.complete(
        message_id,
        status=action.status,
        result=action.body,
        evidence_links=action.evidence_links,
    )
    return {"original": original, "ack": ack, "started": started, "reply": reply}


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


def _requested_capabilities(message: Mapping[str, Any]) -> list[str]:
    payload_value = message.get("payload")
    payload: Mapping[str, Any] = payload_value if isinstance(payload_value, Mapping) else {}
    values: list[Any] = []
    for key in ("required_capabilities", "required_capability", "capabilities", "capability"):
        if key not in payload:
            continue
        value = payload.get(key)
        if isinstance(value, str):
            values.extend(part.strip() for part in value.split(","))
        elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
            values.extend(value)
        elif value:
            values.append(value)
    cleaned: list[str] = []
    for value in values:
        text = str(value or "").strip().lower()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _missing_capabilities(requested: Sequence[str], available: Iterable[str] | None) -> list[str]:
    if not requested or available is None:
        return []
    available_set = {str(item or "").strip().lower() for item in available if str(item or "").strip()}
    return [capability for capability in requested if capability not in available_set]


def _select_handler(
    handlers: Mapping[str, Handler],
    *,
    message_type: str,
    requested_capabilities: Sequence[str],
) -> Handler | None:
    for capability in requested_capabilities:
        handler = handlers.get(f"{message_type}:{capability}") or handlers.get(f"capability:{capability}")
        if handler:
            return handler
    return handlers.get(message_type) or handlers.get("*")


def _clean_action_result(action: A2AActionResult) -> A2AActionResult:
    message_type = action.message_type.lower().strip()
    if message_type not in REPLY_MESSAGE_TYPES:
        message_type = "final"
    terminal_status = TERMINAL_STATUS_ALIASES.get(action.status.lower().strip(), "completed")
    return A2AActionResult(
        message_type=message_type,
        status=terminal_status,
        subject=action.subject,
        body=action.body or terminal_status,
        evidence_links=list(action.evidence_links or []),
    )


def _send_action_reply(
    store: Any,
    action: A2AActionResult,
    *,
    message: Mapping[str, Any],
    sender: str,
    target: str,
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    subject = action.subject or f"{action.message_type}: {message.get('subject') or 'A2A packet'}"
    reply_payload = {"evidence_links": list(action.evidence_links or [])}
    if payload:
        reply_payload.update(payload)
    return _enqueue_reply(
        store,
        sender=sender,
        target=target,
        message_type=action.message_type,
        topic_id=str(message.get("topic_id") or ""),
        subject=subject,
        body=action.body,
        payload=reply_payload,
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
