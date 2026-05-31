"""Valkey/Redis Streams-backed A2A topic inbox storage.

This adapter is the production-oriented A2A coordination backend. It keeps
agent execution separate from delivery: streams hold durable handoff state,
while receivers may later call /v1/runs when a message requires work.
"""

from __future__ import annotations
import importlib
import json
import os
import time
import uuid
from typing import Any, Mapping, Sequence

from gateway.a2a_inbox import A2AInboxError
from gateway.a2a_consult import redact_secrets

_VALID_MESSAGE_TYPES = {
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


class A2AValkeyConfigError(A2AInboxError):
    """Raised when Valkey backend config is requested but unavailable."""


def _config_url(config: Mapping[str, Any] | None) -> str:
    config = config or {}
    return str(
        config.get("url")
        or config.get("redis_url")
        or os.getenv("A2A_VALKEY_URL")
        or os.getenv("VALKEY_URL")
        or os.getenv("REDIS_URL")
        or ""
    ).strip()


def create_valkey_client(
    config: Mapping[str, Any] | None,
    *,
    import_module=importlib.import_module,
) -> Any:
    """Create a real Valkey/Redis client only when config asks for one.

    The dependency is optional: importing this module never imports valkey/redis.
    """

    url = _config_url(config)
    if not url:
        raise A2AValkeyConfigError("Valkey URL is required")
    module = None
    for module_name in ("valkey", "redis"):
        try:
            module = import_module(module_name)
            break
        except ImportError:
            continue
    if module is None:
        raise A2AValkeyConfigError("Install valkey or redis to enable A2A Valkey Streams backend")
    if not hasattr(module, "from_url"):
        raise A2AValkeyConfigError("Valkey/Redis client module does not expose from_url")
    return module.from_url(url, decode_responses=True)


def store_from_config(config: Mapping[str, Any] | None, *, import_module=importlib.import_module) -> "A2AValkeyInboxStore | None":
    """Return a Valkey store when enabled/configured, else None for fallback paths."""

    config = config or {}
    enabled = config.get("enabled", None)
    if enabled is False:
        return None
    if not _config_url(config):
        return None
    return A2AValkeyInboxStore(client=create_valkey_client(config, import_module=import_module))


def _now() -> float:
    return time.time()


def _clean_text(value: Any, *, max_chars: int = 4_000) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def _clean_actor(value: Any) -> str:
    text = _clean_text(value, max_chars=80)
    if not text:
        raise A2AInboxError("agent name is required")
    allowed = []
    for char in text:
        if char.isalnum() or char in "_.:@#-":
            allowed.append(char)
        else:
            allowed.append("-")
    return "".join(allowed)[:80]


def _topic_key(topic_id: str) -> str:
    return f"a2a:topic:{topic_id}:events"


def _json(value: Any) -> str:
    return redact_secrets(json.dumps(value or {}, sort_keys=True, ensure_ascii=False))


class A2AValkeyInboxStore:
    """A small Valkey Streams adapter for Cortex-style A2A topic handoffs."""

    def __init__(self, *, client: Any):
        if client is None:
            raise A2AInboxError("client is required")
        self.client = client

    def enqueue(
        self,
        *,
        sender: str,
        targets: Sequence[str],
        message_type: str,
        topic_id: str,
        subject: str,
        body: str,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str = "",
        message_id: str = "",
    ) -> dict[str, Any]:
        sender = _clean_actor(sender)
        cleaned_targets = [_clean_actor(target) for target in targets]
        if not cleaned_targets:
            raise A2AInboxError("at least one target is required")
        message_type = _clean_text(message_type, max_chars=40).lower()
        if message_type not in _VALID_MESSAGE_TYPES:
            raise A2AInboxError(f"message_type must be one of {sorted(_VALID_MESSAGE_TYPES)}")
        topic_id = _clean_text(topic_id, max_chars=240)
        if not topic_id:
            raise A2AInboxError("topic_id is required")
        subject = _clean_text(subject, max_chars=500)
        body = _clean_text(body, max_chars=8_000)
        if not subject:
            raise A2AInboxError("subject is required")
        if not body:
            raise A2AInboxError("body is required")

        msg_id = message_id or f"a2a_msg_{uuid.uuid4().hex[:24]}"
        if idempotency_key:
            dedupe_key = f"a2a:idempotency:{_clean_text(idempotency_key, max_chars=300)}"
            created = self.client.set(dedupe_key, msg_id, nx=True, ex=86_400)
            if not created:
                existing_id = None
                if hasattr(self.client, "get"):
                    existing_id = self.client.get(dedupe_key)
                if existing_id is None and hasattr(self.client, "sets"):
                    existing_id = self.client.sets.get(dedupe_key)
                if isinstance(existing_id, bytes):
                    existing_id = existing_id.decode()
                if existing_id:
                    existing = self._state(str(existing_id))
                    if existing:
                        return existing

        now = _now()
        state = {
            "message_id": msg_id,
            "schema_version": "cortex-a2a-v1",
            "sender": sender,
            "targets": json.dumps(cleaned_targets, ensure_ascii=False),
            "message_type": message_type,
            "topic_id": topic_id,
            "subject": subject,
            "body": redact_secrets(body),
            "payload_json": _json(payload or {}),
            "stream_ids": "{}",
            "attempts": "0",
            "status": "queued",
            "created_at": str(now),
            "updated_at": str(now),
        }
        self.client.hset(f"a2a:msg:{msg_id}", mapping=state)

        event = {
            "event_type": "queued",
            "message_id": msg_id,
            "topic_id": topic_id,
            "sender": sender,
            "targets": json.dumps(cleaned_targets, ensure_ascii=False),
            "message_type": message_type,
            "subject": subject,
        }
        self.client.xadd(_topic_key(topic_id), event)
        self.client.xadd("cortex:coordination", event)
        stream_ids = {}
        for target in cleaned_targets:
            stream_ids[target] = self.client.xadd(f"stream:agent:{target}", event)
        self.client.hset(f"a2a:msg:{msg_id}", mapping={"stream_ids": _json(stream_ids)})
        return self._state(msg_id) or {"message_id": msg_id, "status": "queued"}

    def claim(
        self,
        *,
        target: str,
        consumer: str,
        count: int = 1,
        block_ms: int = 0,
    ) -> dict[str, Any] | None:
        target = _clean_actor(target)
        consumer = _clean_actor(consumer)
        stream = f"stream:agent:{target}"
        group = f"a2a:{target}"
        try:
            self.client.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        messages = self.client.xreadgroup(group, consumer, {stream: ">"}, count=max(1, int(count or 1)), block=block_ms)
        for _stream_name, entries in messages or []:
            for stream_id, fields in entries:
                message_id = str(fields.get("message_id") or "")
                if not message_id:
                    continue
                self.client.hset(
                    f"a2a:msg:{message_id}",
                    mapping={"status": "claimed", "claimed_by": consumer, "updated_at": str(_now())},
                )
                state = self._state(message_id)
                if state:
                    event = {
                        "event_type": "claimed",
                        "message_id": message_id,
                        "topic_id": state["topic_id"],
                        "message_type": state["message_type"],
                        "subject": state["subject"],
                        "target": target,
                        "claimed_by": consumer,
                    }
                    self.client.xadd(_topic_key(state["topic_id"]), event)
                    self.client.xadd("cortex:coordination", event)
                    state["stream_id"] = stream_id
                    state["consumer_group"] = group
                    return state
        return None

    def reclaim_stale(
        self,
        *,
        target: str,
        consumer: str,
        min_idle_ms: int,
        start_id: str = "0-0",
        count: int = 10,
        max_attempts: int = 3,
    ) -> list[dict[str, Any]]:
        target = _clean_actor(target)
        consumer = _clean_actor(consumer)
        stream = f"stream:agent:{target}"
        group = f"a2a:{target}"
        try:
            self.client.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        raw = self.client.xautoclaim(
            stream,
            group,
            consumer,
            int(min_idle_ms),
            start_id=start_id,
            count=max(1, int(count or 1)),
        )
        entries = raw[1] if isinstance(raw, (tuple, list)) and len(raw) >= 2 else raw
        reclaimed: list[dict[str, Any]] = []
        for stream_id, fields in entries or []:
            message_id = str(fields.get("message_id") or "")
            if not message_id:
                continue
            state = self._state(message_id) or {}
            attempts = int(state.get("attempts") or 0) + 1
            if attempts >= max(1, int(max_attempts or 1)):
                update = {"status": "dead_lettered", "attempts": str(attempts), "claimed_by": consumer, "updated_at": str(_now())}
                self.client.hset(f"a2a:msg:{message_id}", mapping=update)
                event = {"event_type": "dead_lettered", "message_id": message_id, "target": target, "attempts": str(attempts)}
                self.client.xadd("a2a:deadletter", event)
                if state.get("topic_id"):
                    self.client.xadd(_topic_key(state["topic_id"]), event)
                self.client.xack(stream, group, str(stream_id))
            else:
                self.client.hset(
                    f"a2a:msg:{message_id}",
                    mapping={"status": "claimed", "attempts": str(attempts), "claimed_by": consumer, "updated_at": str(_now())},
                )
            updated = self._state(message_id)
            if updated:
                updated["stream_id"] = stream_id
                updated["consumer_group"] = group
                reclaimed.append(updated)
        return reclaimed

    def complete(
        self,
        message_id: str,
        *,
        status: str = "completed",
        result: str = "",
        evidence_links: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        state = self._state(message_id)
        if not state:
            raise A2AInboxError("message not found")
        status = _clean_text(status, max_chars=40).lower()
        if status not in {"completed", "failed", "cancelled", "needs_human"}:
            raise A2AInboxError("invalid terminal status")
        update = {
            "status": status,
            "result": redact_secrets(_clean_text(result, max_chars=8_000)),
            "evidence_links": _json(list(evidence_links or [])),
            "updated_at": str(_now()),
        }
        self.client.hset(f"a2a:msg:{message_id}", mapping=update)
        event = {
            "event_type": status,
            "message_id": message_id,
            "topic_id": state["topic_id"],
            "message_type": state["message_type"],
            "subject": state.get("subject", ""),
            "targets": _json(state.get("targets") or []),
        }
        self.client.xadd(_topic_key(state["topic_id"]), event)
        self.client.xadd("cortex:coordination", event)
        stream_ids = state.get("stream_ids") if isinstance(state.get("stream_ids"), dict) else {}
        for target, stream_id in stream_ids.items():
            if stream_id:
                self.client.xack(f"stream:agent:{target}", f"a2a:{target}", str(stream_id))
        return self._state(message_id) or {"message_id": message_id, "status": status}

    def _state(self, message_id: str) -> dict[str, Any] | None:
        raw = self.client.hgetall(f"a2a:msg:{message_id}")
        if not raw:
            return None
        decoded = {str(k): v.decode() if isinstance(v, bytes) else v for k, v in raw.items()}
        targets_raw = decoded.get("targets") or "[]"
        payload_raw = decoded.get("payload_json") or "{}"
        stream_ids_raw = decoded.get("stream_ids") or "{}"
        try:
            targets = json.loads(targets_raw)
        except Exception:
            targets = []
        try:
            payload = json.loads(payload_raw)
        except Exception:
            payload = {}
        try:
            stream_ids = json.loads(stream_ids_raw)
        except Exception:
            stream_ids = {}
        return {
            "message_id": decoded.get("message_id", message_id),
            "status": decoded.get("status", ""),
            "attempts": int(decoded.get("attempts") or 0),
            "claimed_by": decoded.get("claimed_by", ""),
            "sender": decoded.get("sender", ""),
            "targets": targets,
            "message_type": decoded.get("message_type", ""),
            "topic_id": decoded.get("topic_id", ""),
            "subject": decoded.get("subject", ""),
            "body": decoded.get("body", ""),
            "payload": payload,
            "stream_ids": stream_ids if isinstance(stream_ids, dict) else {},
            "result": decoded.get("result", ""),
        }
