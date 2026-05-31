"""Compact A2A receipt batch formatting.

Discord is a human-visible receipt surface, not the coordination bus. This
module turns already-sanitized A2A lifecycle events into short, non-secret
status lines that a watcher/cron can post later.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from gateway.a2a_consult import redact_secrets

_HEADER = "⚡ **A2A receipt batch**"
_EVENT_ICONS = {
    "queued": "▶️",
    "claimed": "🟡",
    "work_started": "⚙️",
    "completed": "✅",
    "final": "✅",
    "needs_human": "⚠️",
    "failed": "❌",
    "cancelled": "❌",
    "dead_lettered": "❌",
}


def _clip(value: Any, *, max_chars: int = 90) -> str:
    text = redact_secrets("" if value is None else str(value)).strip()
    if len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def _fields(event: Any) -> Mapping[str, Any]:
    if isinstance(event, tuple) and len(event) == 2 and isinstance(event[1], Mapping):
        return event[1]
    if isinstance(event, Mapping):
        return event
    return {}


def _targets(value: Any) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = [value]
    elif isinstance(value, (list, tuple, set)):
        parsed = list(value)
    else:
        parsed = []
    names = [_clip(item, max_chars=40) for item in parsed if _clip(item, max_chars=40)]
    return ",".join(names) or "unknown"


def _message_id_suffix(value: Any) -> str:
    text = _clip(value, max_chars=80)
    if not text:
        return ""
    return f" · `{text[-8:]}`"


def _line(fields: Mapping[str, Any]) -> str:
    event_type = _clip(fields.get("event_type"), max_chars=40).lower() or "event"
    icon = _EVENT_ICONS.get(event_type, "•")
    message_type = _clip(fields.get("message_type"), max_chars=40) or "message"
    subject = _clip(fields.get("subject"), max_chars=80)
    suffix = _message_id_suffix(fields.get("message_id"))

    if event_type == "queued":
        actor = _clip(fields.get("sender"), max_chars=40) or "unknown"
        target = _targets(fields.get("targets") or fields.get("target"))
        core = f"{icon} {actor} → {target} queued `{message_type}`"
    elif event_type == "claimed":
        target = _clip(fields.get("target"), max_chars=40) or "unknown"
        claimed_by = _clip(fields.get("claimed_by"), max_chars=40) or "worker"
        core = f"{icon} {target} claimed by {claimed_by} `{message_type}`"
    else:
        target = _clip(fields.get("target"), max_chars=40)
        if not target:
            target = _targets(fields.get("targets")) if fields.get("targets") else _clip(fields.get("sender"), max_chars=40)
        target = target or "unknown"
        core = f"{icon} {target} {event_type} `{message_type}`"

    if subject:
        core = f"{core} · {subject}"
    return f"{core}{suffix}"


def format_receipt_batch(events: Iterable[Any], *, max_lines: int = 10, header: str = _HEADER) -> str:
    """Render compact A2A lifecycle receipt lines.

    Returns an empty string when there are no renderable events so a watcher can
    remain silent on unchanged ticks.
    """

    lines = []
    for event in events:
        fields = _fields(event)
        if not fields:
            continue
        lines.append(_line(fields))
        if len(lines) >= max(1, int(max_lines or 1)):
            break
    if not lines:
        return ""
    return "\n".join([header, *lines])
