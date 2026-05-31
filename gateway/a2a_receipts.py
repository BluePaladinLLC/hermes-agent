"""Compact A2A lifecycle receipt formatting.

A2A is the work bus; human-visible channels such as #agent-comms are only a
receipt/audit surface. This module produces payload-safe lifecycle summaries and
intentionally performs no network or Discord posting.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

RECEIPT_EVENTS = frozenset(
    {
        "sent",
        "delivered",
        "alerted",
        "claimed",
        "read",
        "ack",
        "queued",
        "work_started",
        "question",
        "needs_clarification",
        "answer",
        "reply",
        "fyi_logged",
        "needs_human",
        "final",
        "failed",
        "timeout",
        "closed",
    }
)

_STATUS_EMOJI = {
    "sent": "📨",
    "delivered": "📬",
    "alerted": "🔔",
    "claimed": "👀",
    "read": "👀",
    "ack": "✅",
    "queued": "🕒",
    "work_started": "⚙️",
    "question": "❓",
    "needs_clarification": "❓",
    "answer": "💬",
    "reply": "💬",
    "fyi_logged": "📝",
    "needs_human": "🟡",
    "final": "🏁",
    "closed": "🏁",
    "failed": "🔴",
    "timeout": "⏱️",
}

_SECRET_MARKERS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "api key",
    "apikey",
    "private_key",
    "credential",
    "authorization",
    "bearer",
)


def format_a2a_receipt(event: Mapping[str, Any]) -> str:
    """Return one compact, payload-safe lifecycle receipt line.

    The formatter deliberately ignores message bodies and payload contents. It
    only uses routing/lifecycle metadata, so callers can safely post the output
    to an audit channel without leaking private task text.
    """

    event_type = _clean_token(event.get("event") or event.get("event_type") or event.get("message_type") or "update")
    if event_type not in RECEIPT_EVENTS:
        event_type = "reply" if event_type else "update"
    sender = _clean_name(event.get("sender")) or "unknown"
    target = _target_name(event) or "unknown"
    subject = _clean_subject(event.get("subject") or event.get("topic") or "A2A packet")
    message_id = _short_id(event.get("message_id") or event.get("id"))
    topic_id = _short_id(event.get("topic_id") or event.get("thread_id") or event.get("correlation_id"))
    status = _clean_token(event.get("status") or event_type)
    emoji = _STATUS_EMOJI.get(event_type, "📜")

    parts = [f"{emoji} A2A", f"{sender} → {target}", event_type]
    if status and status != event_type:
        parts.append(status)
    if message_id:
        parts.append(f"msg `{message_id}`")
    if topic_id:
        parts.append(f"topic `{topic_id}`")
    parts.append(f"`{subject}`")
    return " | ".join(parts)


def format_a2a_receipts(events: list[Mapping[str, Any]]) -> list[str]:
    """Format multiple A2A lifecycle events as compact receipt lines."""

    return [format_a2a_receipt(event) for event in events]


def _clean_token(value: Any) -> str:
    text = str(value or "").strip().lower().replace(" ", "_")
    return "".join(ch for ch in text if ch.isalnum() or ch in {"_", "-"})[:40]


def _clean_name(value: Any) -> str:
    text = str(value or "").strip()
    return "".join(ch for ch in text if ch.isalnum() or ch in {"_", "-", "."})[:48]


def _target_name(event: Mapping[str, Any]) -> str:
    target = event.get("target") or event.get("receiver")
    if not target and isinstance(event.get("targets"), list) and event["targets"]:
        target = event["targets"][0]
    return _clean_name(target)


def _short_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = "".join(ch for ch in text if ch.isalnum() or ch in {"_", "-", "/", ":"})
    return text[:20]


def _clean_subject(value: Any) -> str:
    text = str(value or "").replace("\n", " ").replace("\r", " ").strip()
    lowered = text.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        return "[redacted-sensitive-subject]"
    while "  " in text:
        text = text.replace("  ", " ")
    return text[:140] or "A2A packet"
