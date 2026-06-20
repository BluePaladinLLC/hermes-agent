"""A2A sender authentication and action-class authorization helpers.

The A2A receiver treats packets as data from a shared coordination bus. This
module verifies sender identity with a small canonical HMAC envelope, rejects
replay, and authorizes the packet by sender -> target -> action class before any
runtime handler runs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
import time
from typing import Any, Callable, ClassVar, Mapping, MutableMapping, Protocol

LOW_BLAST_ACTION_CLASSES = frozenset({"ack", "status", "read_only", "coordination", "canary"})
SENSITIVE_ACTION_CLASSES = frozenset(
    {
        "work_request",
        "repo_mutation",
        "service_mutation",
        "credential_action",
        "security_audit",
        "project_handoff",
    }
)


@dataclass(frozen=True)
class AuthDecision:
    """Decision returned by A2A auth/policy verification."""

    status: str
    reason: str = ""
    action_class: str = ""
    sender: str = ""
    target: str = ""

    ALLOW: ClassVar["AuthDecision"]
    REJECT: ClassVar["AuthDecision"]
    NEEDS_HUMAN: ClassVar["AuthDecision"]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, AuthDecision):
            return self.status == other.status
        if isinstance(other, str):
            return self.status == other
        return False

    def __bool__(self) -> bool:
        return self.status == "allow"


AuthDecision.ALLOW = AuthDecision("allow")  # type: ignore[attr-defined]
AuthDecision.REJECT = AuthDecision("reject")  # type: ignore[attr-defined]
AuthDecision.NEEDS_HUMAN = AuthDecision("needs_human")  # type: ignore[attr-defined]


class ReplayStore(Protocol):
    def seen_or_record(self, key: str, *, ttl_seconds: int) -> bool:
        """Return True if key was already seen; otherwise record and return False."""
        ...

@dataclass
class MemoryReplayStore:
    """In-memory replay cache for tests and short-lived receiver ticks."""

    now: Callable[[], float] = time.time
    _seen: dict[str, float] = field(default_factory=dict)

    def seen_or_record(self, key: str, *, ttl_seconds: int) -> bool:
        now = float(self.now())
        for item in [k for k, expires_at in self._seen.items() if expires_at <= now]:
            self._seen.pop(item, None)
        if key in self._seen:
            return True
        self._seen[key] = now + max(1, int(ttl_seconds))
        return False


@dataclass(frozen=True)
class A2AAuthPolicy:
    """Identity + action-class policy for A2A packets.

    trusted_keys: {sender: {key_id: secret}}
    grants: {sender: {target: {action_class, ...}}}
    """

    trusted_keys: Mapping[str, Mapping[str, str]]
    grants: Mapping[str, Mapping[str, set[str] | frozenset[str] | list[str] | tuple[str, ...]]]
    now: Callable[[], float] = time.time
    replay_store: ReplayStore = field(default_factory=MemoryReplayStore)
    max_skew_seconds: int = 300
    replay_ttl_seconds: int = 900

    def secret_for(self, sender: str, key_id: str) -> str:
        return str((self.trusted_keys.get(sender) or {}).get(key_id) or "")

    def allowed_actions(self, sender: str, target: str) -> set[str]:
        raw = (self.grants.get(sender) or {}).get(target) or set()
        return {str(item).strip().lower() for item in raw if str(item).strip()}


def action_class_for(packet: Mapping[str, Any]) -> str:
    metadata_obj = packet.get("metadata")
    metadata = metadata_obj if isinstance(metadata_obj, Mapping) else {}
    payload_obj = packet.get("payload")
    payload = payload_obj if isinstance(payload_obj, Mapping) else {}
    for source in (metadata, payload, packet):
        value = source.get("action_class") or source.get("task_class") or source.get("message_type")
        if value:
            return str(value).strip().lower()
    return "unspecified"


def body_hash(packet: Mapping[str, Any]) -> str:
    payload_obj = packet.get("payload")
    payload = payload_obj if isinstance(payload_obj, Mapping) else {}
    body = payload.get("body") or payload.get("prompt") or packet.get("body") or packet.get("summary") or ""
    if not isinstance(body, str):
        body = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def canonical_fields(packet: Mapping[str, Any], *, timestamp: int, nonce: str, key_id: str) -> dict[str, Any]:
    return {
        "sender": str(packet.get("sender") or ""),
        "target": str(packet.get("target") or ""),
        "message_id": str(packet.get("message_id") or packet.get("id") or ""),
        "topic": str(packet.get("topic") or packet.get("topic_id") or ""),
        "message_type": str(packet.get("message_type") or packet.get("kind") or ""),
        "action_class": action_class_for(packet),
        "body_sha256": body_hash(packet),
        "timestamp": int(timestamp),
        "nonce": str(nonce),
        "key_id": str(key_id),
    }


def canonical_string(packet: Mapping[str, Any], *, timestamp: int, nonce: str, key_id: str) -> str:
    return json.dumps(
        canonical_fields(packet, timestamp=timestamp, nonce=nonce, key_id=key_id),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def signature_for(packet: Mapping[str, Any], *, secret: str, timestamp: int, nonce: str, key_id: str) -> str:
    canonical = canonical_string(packet, timestamp=timestamp, nonce=nonce, key_id=key_id)
    return "sha256=" + hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_packet(
    packet: MutableMapping[str, Any],
    *,
    key_id: str,
    secret: str,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> MutableMapping[str, Any]:
    """Attach A2A auth metadata to a packet in place and return it."""

    ts = int(timestamp if timestamp is not None else time.time())
    nonce_value = str(nonce or hashlib.sha256(f"{time.time_ns()}:{id(packet)}".encode()).hexdigest()[:24])
    metadata = packet.setdefault("metadata", {})
    if not isinstance(metadata, MutableMapping):
        raise TypeError("packet.metadata must be a mutable mapping")
    metadata["auth"] = {
        "alg": "hmac-sha256-v1",
        "key_id": str(key_id),
        "timestamp": ts,
        "nonce": nonce_value,
        "signature": signature_for(packet, secret=secret, timestamp=ts, nonce=nonce_value, key_id=str(key_id)),
    }
    return packet


def _decision(status: AuthDecision, *, reason: str, packet: Mapping[str, Any], action_class: str) -> AuthDecision:
    return AuthDecision(
        status.status,
        reason=reason,
        action_class=action_class,
        sender=str(packet.get("sender") or ""),
        target=str(packet.get("target") or ""),
    )


def verify_packet(packet: Mapping[str, Any], policy: A2AAuthPolicy) -> AuthDecision:
    """Verify signature/replay and authorize the packet action class."""

    sender = str(packet.get("sender") or "")
    action_class = action_class_for(packet)
    metadata_obj = packet.get("metadata")
    metadata = metadata_obj if isinstance(metadata_obj, Mapping) else {}
    auth_obj = metadata.get("auth")
    auth = auth_obj if isinstance(auth_obj, Mapping) else None
    if not auth:
        return _decision(AuthDecision.REJECT, reason="missing_signature", packet=packet, action_class=action_class)

    key_id = str(auth.get("key_id") or "")
    nonce = str(auth.get("nonce") or "")
    signature = str(auth.get("signature") or "")
    try:
        timestamp = int(auth.get("timestamp") or "")
    except (TypeError, ValueError):
        return _decision(AuthDecision.REJECT, reason="invalid_timestamp", packet=packet, action_class=action_class)
    if not key_id or not nonce or not signature:
        return _decision(AuthDecision.REJECT, reason="incomplete_signature", packet=packet, action_class=action_class)

    now = int(policy.now())
    if abs(now - timestamp) > max(1, int(policy.max_skew_seconds)):
        return _decision(AuthDecision.REJECT, reason="signature_expired", packet=packet, action_class=action_class)

    secret = policy.secret_for(sender, key_id)
    if not secret:
        return _decision(AuthDecision.REJECT, reason=f"unknown_key:{sender}:{key_id}", packet=packet, action_class=action_class)

    expected = signature_for(packet, secret=secret, timestamp=timestamp, nonce=nonce, key_id=key_id)
    if not hmac.compare_digest(signature, expected):
        return _decision(AuthDecision.REJECT, reason="invalid_signature", packet=packet, action_class=action_class)

    replay_key = f"{sender}:{key_id}:{nonce}:{packet.get('message_id') or packet.get('id') or ''}"
    if policy.replay_store.seen_or_record(replay_key, ttl_seconds=policy.replay_ttl_seconds):
        return _decision(AuthDecision.REJECT, reason="replay_detected", packet=packet, action_class=action_class)

    allowed = policy.allowed_actions(sender, str(packet.get("target") or ""))
    if action_class in allowed or "*" in allowed:
        return _decision(AuthDecision.ALLOW, reason="authorized", packet=packet, action_class=action_class)
    return _decision(
        AuthDecision.NEEDS_HUMAN,
        reason=f"action_class_not_authorized:{action_class}",
        packet=packet,
        action_class=action_class,
    )
