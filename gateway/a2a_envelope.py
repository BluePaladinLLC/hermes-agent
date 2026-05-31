"""Unified Cortex A2A message envelope validation."""

from __future__ import annotations

import re
from typing import Any, Mapping

_MESSAGE_TYPES = {
    "consult",
    "handoff",
    "work_request",
    "question",
    "answer",
    "ack",
    "nack",
    "work_started",
    "work_result",
    "needs_human",
    "final",
}

_DEFAULT_OWNERSHIP = {
    "consult": "sender_owns",
    "question": "sender_owns",
    "answer": "sender_owns",
    "ack": "sender_owns",
    "nack": "sender_owns",
    "handoff": "receiver_owns",
    "work_request": "bounded_subtask",
    "work_started": "bounded_subtask",
    "work_result": "bounded_subtask",
    "needs_human": "bounded_subtask",
    "final": "sender_owns",
}

_OWNERSHIP_MODES = {"sender_owns", "receiver_owns", "bounded_subtask"}
_PRIVACY_SCOPES = {"team", "private", "admin"}
_MAX_TURNS_HARD_CAP = 8


class A2AEnvelopeError(ValueError):
    """Raised when an A2A envelope is invalid."""


def _text(value: Any, field: str, *, max_chars: int = 4_000, required: bool = True) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise A2AEnvelopeError(f"{field} is required")
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def _actor(value: Any, field: str) -> str:
    text = _text(value, field, max_chars=80)
    cleaned = re.sub(r"[^A-Za-z0-9_.:@#-]", "-", text)
    if not cleaned:
        raise A2AEnvelopeError(f"{field} is required")
    return cleaned[:80]


def _targets(value: Any) -> list[str]:
    raw = value if isinstance(value, (list, tuple, set)) else [value]
    targets = [_actor(item, "to") for item in raw if str(item or "").strip()]
    if not targets:
        raise A2AEnvelopeError("to is required")
    return list(dict.fromkeys(targets))


def _int_range(value: Any, field: str, *, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise A2AEnvelopeError(f"{field} must be an integer") from exc
    if number < minimum or number > maximum:
        raise A2AEnvelopeError(f"{field} must be between {minimum} and {maximum}")
    return number


def normalize_a2a_envelope(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a Cortex A2A v1 envelope."""

    if not isinstance(payload, Mapping):
        raise A2AEnvelopeError("payload must be a JSON object")

    message_type = _text(payload.get("message_type") or payload.get("type"), "message_type", max_chars=40).lower()
    if message_type not in _MESSAGE_TYPES:
        raise A2AEnvelopeError(f"message_type must be one of {sorted(_MESSAGE_TYPES)}")

    ownership_mode = _text(
        payload.get("ownership_mode") or _DEFAULT_OWNERSHIP[message_type],
        "ownership_mode",
        max_chars=40,
    )
    if ownership_mode not in _OWNERSHIP_MODES:
        raise A2AEnvelopeError(f"ownership_mode must be one of {sorted(_OWNERSHIP_MODES)}")

    turn_count = _int_range(payload.get("turn_count"), "turn_count", default=1, minimum=1, maximum=_MAX_TURNS_HARD_CAP)
    max_turns = _int_range(payload.get("max_turns"), "max_turns", default=4, minimum=1, maximum=_MAX_TURNS_HARD_CAP)
    if turn_count > max_turns:
        raise A2AEnvelopeError("turn_count must be <= max_turns")

    question_budget = _int_range(payload.get("question_budget"), "question_budget", default=2, minimum=0, maximum=8)
    privacy_scope = _text(payload.get("privacy_scope") or "team", "privacy_scope", max_chars=40)
    if privacy_scope not in _PRIVACY_SCOPES:
        raise A2AEnvelopeError(f"privacy_scope must be one of {sorted(_PRIVACY_SCOPES)}")

    artifacts = payload.get("artifacts") or []
    if not isinstance(artifacts, list):
        raise A2AEnvelopeError("artifacts must be a list")

    requires_ack = payload.get("requires_ack", True)
    if not isinstance(requires_ack, bool):
        raise A2AEnvelopeError("requires_ack must be a boolean")

    return {
        "schema_version": "cortex-a2a-v1",
        "message_id": _text(payload.get("message_id") or "", "message_id", required=False, max_chars=120),
        "topic_id": _text(payload.get("topic_id") or payload.get("thread_id"), "topic_id", max_chars=240),
        "thread_id": _text(payload.get("thread_id") or payload.get("topic_id"), "thread_id", max_chars=240),
        "from": _actor(payload.get("from") or payload.get("sender"), "from"),
        "to": _targets(payload.get("to") or payload.get("targets")),
        "message_type": message_type,
        "ownership_mode": ownership_mode,
        "subject": _text(payload.get("subject"), "subject", max_chars=500),
        "body": _text(payload.get("body"), "body", max_chars=8_000),
        "artifacts": artifacts,
        "expected_output": _text(payload.get("expected_output") or "ack or result", "expected_output", required=False),
        "question_budget": question_budget,
        "turn_count": turn_count,
        "max_turns": max_turns,
        "requires_ack": requires_ack,
        "ack_deadline_seconds": _int_range(
            payload.get("ack_deadline_seconds"),
            "ack_deadline_seconds",
            default=300,
            minimum=0,
            maximum=86_400,
        ),
        "privacy_scope": privacy_scope,
        "idempotency_key": _text(payload.get("idempotency_key") or "", "idempotency_key", required=False, max_chars=300),
        "return_to": _text(payload.get("return_to") or payload.get("from") or "", "return_to", required=False, max_chars=80),
    }
