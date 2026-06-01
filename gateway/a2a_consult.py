"""Private agent-to-agent consultation helper over the API server /v1/runs surface.

This module intentionally stores no durable task state.  It starts a bounded
remote run, polls status, and returns a compact consultation contract suitable
for ephemeral A2A advice.  Kanban remains the durable work source of truth.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urljoin, urlparse

from gateway.a2a_envelope import normalize_a2a_envelope

try:  # aiohttp is already an API server dependency, but keep imports testable.
    import aiohttp
except Exception:  # pragma: no cover - exercised only in stripped environments
    aiohttp = None  # type: ignore[assignment]


_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_APPROVAL_STATUSES = frozenset({"waiting_for_approval", "requires_approval"})
_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_POLL_INTERVAL_SECONDS = 0.25
_MAX_NOTE_CHARS = 8_000
_MAX_TIMEOUT_SECONDS = 300.0
_MAX_POLL_INTERVAL_SECONDS = 10.0
_HANDOFF_KINDS = frozenset({"handoff", "state_update"})
_ENVELOPE_VERSION = "0.1"
_MAX_ENVELOPE_LIST_ITEMS = 12
_MAX_ENVELOPE_FIELD_CHARS = 2_000

# Deliberately conservative: false positives are preferable to leaking raw
# credentials into a cross-agent prompt or durable logs.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    ),
    (
        "bearer_token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    ),
    (
        "assignment_secret",
        re.compile(
            r"(?i)\b(api[_-]?key|token|secret|password|credential|access[_-]?key)\b\s*[:=]\s*['\"]?[^\s'\"]{8,}"
        ),
    ),
    (
        "openai_style_key",
        re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    ),
    (
        "github_token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    (
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ),
    (
        "slack_token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}\b"),
    ),
)


class A2AConsultError(ValueError):
    """Raised for local validation failures before any remote run is started."""


@dataclass(frozen=True)
class A2ATarget:
    """Resolved, configured remote Hermes API server target."""

    name: str
    base_url: str
    api_key: Optional[str] = None
    headers: Mapping[str, str] | None = None

    @property
    def runs_url(self) -> str:
        return urljoin(self.base_url.rstrip("/") + "/", "v1/runs")

    def run_url(self, run_id: str) -> str:
        return urljoin(self.base_url.rstrip("/") + "/", f"v1/runs/{run_id}")

    def stop_url(self, run_id: str) -> str:
        return urljoin(self.base_url.rstrip("/") + "/", f"v1/runs/{run_id}/stop")

    def request_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.headers:
            for key, value in self.headers.items():
                if key.lower() == "authorization":
                    # Authorization comes from api_key only so target configs are
                    # deterministic and returned metadata never needs to carry it.
                    continue
                headers[str(key)] = str(value)
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


@dataclass(frozen=True)
class A2AConsultRequest:
    target: str
    prompt: str = ""
    notes: str = ""
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS
    confidence: str = "unknown"
    kind: str = "consult"
    topic: str = ""
    summary: str = ""
    state: str = ""
    decisions: list[str] = field(default_factory=list)
    context: str = ""
    open_questions: list[str] = field(default_factory=list)
    evidence_links: list[str] = field(default_factory=list)
    origin_channel: str = ""
    ttl_seconds: Optional[float] = None
    expires_at: str = ""
    ack_required: bool = True

    @property
    def is_state_handoff(self) -> bool:
        return self.kind in _HANDOFF_KINDS


def detect_secret(text: Any) -> Optional[str]:
    """Return the matching secret class if text appears to contain raw secrets."""

    if text is None:
        return None
    haystack = str(text)
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(haystack):
            return name
    return None


def redact_secrets(text: Any) -> str:
    """Best-effort redaction for remote output before it enters the contract."""

    redacted = "" if text is None else str(text)
    for name, pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(f"[REDACTED:{name}]", redacted)
    return redacted


def normalize_targets(raw_targets: Any) -> Dict[str, A2ATarget]:
    """Normalize configured target mappings and reject malformed entries.

    Accepted shapes:
        {"pons": "http://127.0.0.1:8643"}
        {"pons": {"base_url": "http://...", "api_key": "..."}}
        {"pons": {"base_url": "http://...", "api_key_env": "PONS_API_SERVER_KEY"}}
    """

    if not raw_targets:
        return {}
    if not isinstance(raw_targets, Mapping):
        raise A2AConsultError("a2a_targets must be a mapping of target name to endpoint config")

    targets: Dict[str, A2ATarget] = {}
    for raw_name, raw_value in raw_targets.items():
        name = str(raw_name).strip()
        if not name or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", name):
            raise A2AConsultError(f"Invalid A2A target name: {raw_name!r}")

        if isinstance(raw_value, str):
            base_url = raw_value.strip()
            api_key = None
            headers: Mapping[str, str] | None = None
        elif isinstance(raw_value, Mapping):
            base_url = str(raw_value.get("base_url") or raw_value.get("url") or "").strip()
            api_key_value = raw_value.get("api_key") or raw_value.get("key")
            api_key_env = str(raw_value.get("api_key_env") or raw_value.get("key_env") or "").strip()
            if api_key_value and api_key_env:
                raise A2AConsultError(f"A2A target {name!r} must not set both api_key and api_key_env")
            if api_key_env:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", api_key_env):
                    raise A2AConsultError(f"Invalid api_key_env for A2A target {name!r}")
                api_key_value = os.getenv(api_key_env)
                if not str(api_key_value or "").strip():
                    raise A2AConsultError(f"api_key_env {api_key_env!r} for A2A target {name!r} is not set")
            api_key = str(api_key_value).strip() if api_key_value else None
            raw_headers = raw_value.get("headers")
            headers = raw_headers if isinstance(raw_headers, Mapping) else None
        else:
            raise A2AConsultError(f"Invalid config for A2A target {name!r}")

        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise A2AConsultError(f"Invalid base_url for A2A target {name!r}")
        if parsed.username or parsed.password:
            raise A2AConsultError(f"A2A target {name!r} base_url must not include credentials")
        targets[name] = A2ATarget(name=name, base_url=base_url, api_key=api_key, headers=headers)

    return targets


def resolve_target(target_name: str, targets: Mapping[str, A2ATarget]) -> A2ATarget:
    """Deterministically resolve a configured target; fail closed on unknown."""

    key = str(target_name or "").strip()
    if key not in targets:
        raise A2AConsultError(f"Unknown A2A target: {key or '<empty>'}")
    return targets[key]


def _compact_text(value: Any, *, max_chars: int = _MAX_ENVELOPE_FIELD_CHARS) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def _compact_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    compacted: list[str] = []
    for item in items[:_MAX_ENVELOPE_LIST_ITEMS]:
        text = _compact_text(item)
        if text:
            compacted.append(text)
    return compacted


def _parse_expires_at(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise A2AConsultError("expires_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bool_payload_value(value: Any, *, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    raise A2AConsultError("ack_required must be a boolean")


def _validate_envelope_fields(req: A2AConsultRequest) -> None:
    if req.kind != "consult" and not req.is_state_handoff:
        raise A2AConsultError("kind must be 'consult', 'handoff', or 'state_update'")
    if not req.is_state_handoff:
        return
    if not req.topic.strip():
        raise A2AConsultError("topic is required for handoff/state_update")
    if not req.summary.strip():
        raise A2AConsultError("summary is required for handoff/state_update")
    envelope_text = json.dumps(_state_envelope(req), sort_keys=True)
    if len(envelope_text) > _MAX_NOTE_CHARS:
        raise A2AConsultError(f"handoff/state_update envelope must be <= {_MAX_NOTE_CHARS} characters")
    secret_class = detect_secret(envelope_text)
    if secret_class:
        raise A2AConsultError(f"A2A handoff envelope rejected: contains {secret_class}")
    if req.ttl_seconds is not None:
        if not math.isfinite(req.ttl_seconds) or req.ttl_seconds <= 0:
            raise A2AConsultError("ttl_seconds must be a positive finite number")
    expires_at = _parse_expires_at(req.expires_at)
    if req.ttl_seconds is not None and expires_at is not None:
        raise A2AConsultError("ttl_seconds and expires_at are mutually exclusive")
    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        raise A2AConsultError("expires_at must be in the future")


def _state_envelope(req: A2AConsultRequest) -> Dict[str, Any]:
    envelope: Dict[str, Any] = {
        "object": "hermes.a2a_state_envelope",
        "version": _ENVELOPE_VERSION,
        "kind": req.kind,
        "from": {"agent": "caller"},
        "to": {"agent": req.target.strip()},
        "topic": _compact_text(req.topic),
        "summary": _compact_text(req.summary),
        "state": _compact_text(req.state),
        "decisions": _compact_list(req.decisions),
        "context": _compact_text(req.context or req.notes),
        "open_questions": _compact_list(req.open_questions),
        "evidence_links": _compact_list(req.evidence_links),
        "ack_required": bool(req.ack_required),
    }
    if req.origin_channel.strip():
        envelope["origin_channel"] = _compact_text(req.origin_channel)
    if req.ttl_seconds is not None:
        envelope["ttl_seconds"] = req.ttl_seconds
    if req.expires_at.strip():
        envelope["expires_at"] = _compact_text(req.expires_at)
    return envelope


def _ack_status_for(status: str, position: Any = "") -> str:
    if status == "completed":
        text = str(position or "").lower()
        match = re.search(r"\back[_ -]?status\b\s*[:=]\s*(received|accepted|rejected|expired|failed)\b", text)
        if match:
            return match.group(1)
        match = re.search(r"\back\b[^\n]{0,80}\b(received|accepted|rejected|expired|failed)\b", text)
        if match:
            return match.group(1)
        # The target run completed, so transport delivery is confirmed, but the
        # receiver did not provide a semantic ACK. Keep the visible summary honest.
        return "received"
    if status in {"approval_needed", "timeout"}:
        return "received" if status == "approval_needed" else "expired"
    if status == "rejected":
        return "rejected"
    return "failed"


def _origin_summary(req: A2AConsultRequest, payload: Mapping[str, Any]) -> str:
    ack_status = str(
        payload.get("ack_status")
        or ("not_required" if req.is_state_handoff and not req.ack_required else None)
        or _ack_status_for(str(payload.get("status") or ""), payload.get("position") or "")
    )
    run_id = payload.get("run_id") or "none"
    status = payload.get("status") or "unknown"
    record_status = str(payload.get("record_status") or "unknown") if req.is_state_handoff else ""
    record_clause = f"; record: {record_status}" if record_status else ""
    return (
        f"A2A {req.kind} to {req.target.strip()} — topic: {req.topic.strip()}; "
        f"summary: {req.summary.strip()}; ACK: {ack_status} ({status}){record_clause}; run_id: {run_id}"
    )


def _extract_labeled_field(text: Any, field: str, allowed: set[str] | None = None) -> str:
    haystack = str(text or "")
    match = re.search(rf"\b{re.escape(field)}\b\s*[:=]\s*([^\n,;]+)", haystack, re.IGNORECASE)
    if not match:
        return ""
    value = match.group(1).strip().strip("`'\"")
    if allowed is not None:
        lowered = value.lower()
        if lowered in allowed:
            return lowered
        return ""
    return value[:200]


def _record_status_for(status: str, position: Any = "") -> str:
    if status != "completed":
        return "unable"
    explicit = _extract_labeled_field(str(position or ""), "record_status", {"recorded", "unable", "not_needed"})
    if explicit:
        return explicit
    text = str(position or "").lower()
    if "todo" in text and any(word in text for word in ("created", "recorded", "added", "saved")):
        return "recorded"
    return "unknown"


def _record_handle_for(position: Any = "") -> str:
    return _extract_labeled_field(position, "record_handle")


def _annotate_handoff_contract(payload: Dict[str, Any], req: A2AConsultRequest) -> Dict[str, Any]:
    if not req.is_state_handoff:
        return payload
    payload.setdefault("handoff_kind", req.kind)
    payload.setdefault("topic", req.topic.strip())
    payload.setdefault("summary", req.summary.strip())
    payload.setdefault(
        "ack_status",
        "not_required" if not req.ack_required else _ack_status_for(str(payload.get("status") or ""), payload.get("position") or ""),
    )
    payload.setdefault("record_status", _record_status_for(str(payload.get("status") or ""), payload.get("position") or ""))
    record_handle = _record_handle_for(payload.get("position") or "")
    if record_handle:
        payload.setdefault("record_handle", record_handle)
    payload.setdefault("origin_summary", _origin_summary(req, payload))
    payload.setdefault(
        "origin_delivery",
        "post exactly one visible summary using origin_summary; do not dump envelope state",
    )
    return payload


def _consult_topic_id(req: A2AConsultRequest) -> str:
    if req.topic.strip():
        return req.topic.strip()
    return f"consult/{_compact_text(req.target, max_chars=80)}/{uuid.uuid4().hex[:12]}"


def _consult_body(req: A2AConsultRequest) -> str:
    if req.is_state_handoff:
        pieces = [req.summary.strip(), req.state.strip(), req.context.strip()]
    else:
        pieces = [req.prompt.strip(), req.notes.strip()]
    return "\n\n".join(piece for piece in pieces if piece)


def enqueue_consult_envelope(
    request: A2AConsultRequest,
    inbox_store: Any,
    *,
    sender: str = "caller",
) -> Dict[str, Any]:
    """Compatibility path: turn legacy consult input into one unified A2A inbox message."""

    if inbox_store is None:
        raise A2AConsultError("inbox_store is required for unified A2A consult delivery")
    _validate_request(request)
    message_type = "handoff" if request.is_state_handoff else "consult"
    topic_id = _consult_topic_id(request)
    subject = request.topic.strip() or f"Consult request for {request.target.strip()}"
    envelope = normalize_a2a_envelope(
        {
            "from": sender,
            "to": request.target,
            "message_type": message_type,
            "topic_id": topic_id,
            "subject": subject,
            "body": _consult_body(request),
            "expected_output": "ack + concise result",
            "requires_ack": bool(request.ack_required),
            "idempotency_key": f"{sender}:{request.target}:{message_type}:{topic_id}",
            "return_to": sender,
        }
    )
    enqueue_kwargs = {
        "sender": envelope["from"],
        "targets": envelope["to"],
        "message_type": envelope["message_type"],
        "topic_id": envelope["topic_id"],
        "subject": envelope["subject"],
        "body": envelope["body"],
        "payload": envelope,
        "idempotency_key": envelope.get("idempotency_key", ""),
    }
    if envelope.get("message_id"):
        enqueue_kwargs["message_id"] = envelope["message_id"]
    enqueue_params = inspect.signature(inbox_store.enqueue).parameters
    accepts_kwargs = any(param.kind is inspect.Parameter.VAR_KEYWORD for param in enqueue_params.values())
    if accepts_kwargs or "targets" in enqueue_params:
        queued = inbox_store.enqueue(**enqueue_kwargs)
    else:
        queued = inbox_store.enqueue(
            target=envelope["to"][0],
            sender=envelope["from"],
            kind=envelope["message_type"],
            topic=envelope["topic_id"],
            summary=envelope["subject"],
            payload=envelope,
            message_id=envelope.get("message_id") or None,
        )
    message_id = queued.get("message_id") or queued.get("id")
    return {
        "object": "hermes.a2a_consult",
        "target": request.target.strip(),
        "status": queued.get("status", "queued"),
        "position": "queued for receiver via unified A2A inbox",
        "confidence": request.confidence,
        "evidence": ["consult normalized as cortex-a2a-v1 envelope", f"message_id={message_id}"],
        "risks": [],
        "next_action": "receiver watcher should ACK/claim the inbox message before any /v1/runs execution",
        "run_id": None,
        "message_id": message_id,
        "delivery": "api:/v1/a2a/consult -> unified-a2a-inbox",
        "envelope": envelope,
    }


def _validate_request(req: A2AConsultRequest) -> None:
    if not req.target.strip():
        raise A2AConsultError("target is required")
    _validate_envelope_fields(req)
    if not req.is_state_handoff and not req.prompt.strip():
        raise A2AConsultError("prompt is required")
    if len(req.prompt) + len(req.notes or "") > _MAX_NOTE_CHARS:
        raise A2AConsultError(f"prompt and notes must be <= {_MAX_NOTE_CHARS} characters")
    secret_class = detect_secret(req.prompt) or detect_secret(req.notes)
    if secret_class:
        raise A2AConsultError(f"A2A consult prompt/notes rejected: contains {secret_class}")
    if not math.isfinite(req.timeout_seconds):
        raise A2AConsultError("timeout_seconds must be finite")
    if req.timeout_seconds <= 0:
        raise A2AConsultError("timeout_seconds must be positive")
    if req.timeout_seconds > _MAX_TIMEOUT_SECONDS:
        raise A2AConsultError(f"timeout_seconds must be <= {_MAX_TIMEOUT_SECONDS:g}")
    if not math.isfinite(req.poll_interval_seconds):
        raise A2AConsultError("poll_interval_seconds must be finite")
    if req.poll_interval_seconds <= 0:
        raise A2AConsultError("poll_interval_seconds must be positive")
    if req.poll_interval_seconds > _MAX_POLL_INTERVAL_SECONDS:
        raise A2AConsultError(f"poll_interval_seconds must be <= {_MAX_POLL_INTERVAL_SECONDS:g}")


def _consult_prompt(req: A2AConsultRequest) -> str:
    if req.is_state_handoff:
        envelope_json = json.dumps(_state_envelope(req), indent=2, sort_keys=True)
        caller_prompt = req.prompt.strip()
        ack_instruction = (
            "Acknowledge receipt and return concise fields: ack_status "
            "(received|accepted|rejected|expired|failed), record_status "
            "(recorded|unable|not_needed), record_handle, position, confidence, "
            "evidence, risks, next_action."
            if req.ack_required
            else "Semantic ACK is optional for this envelope; return record_status, record_handle, position, confidence, evidence, risks, and next_action."
        )
        return (
            "Hermes A2A state envelope received via the private API/A2A lane. "
            "Treat this as a compact handoff/state_update, not a public chat message. "
            "This handoff must result in information transfer, not just an ACK. "
            "Before replying, record a lightweight local action/state pointer for yourself "
            "using the todo tool if available. Use a title like 'A2A handoff: <topic>' "
            "and include the envelope summary plus any next_action/open_questions in the todo content. "
            "If the todo tool is unavailable, say record_status=unable and explain why. "
            "Do not create or mutate Kanban tasks unless explicitly asked; Kanban is for durable multi-step work. "
            f"{ack_instruction}\n\n"
            f"Envelope JSON:\n{envelope_json}"
            + (f"\n\nCaller instruction:\n{caller_prompt}" if caller_prompt else "")
        )
    notes = req.notes.strip()
    return (
        "Private Hermes A2A consultation request. Do not create or mutate Kanban "
        "tasks; Kanban is the durable source of truth. Return concise advice using "
        "these fields: position, confidence, evidence, risks, next_action.\n\n"
        f"Prompt:\n{req.prompt.strip()}"
        + (f"\n\nContext notes (sanitized, no secrets):\n{notes}" if notes else "")
    )


def _base_contract(
    *,
    target: str,
    status: str,
    run_id: Optional[str] = None,
    position: str = "unavailable",
    confidence: str = "low",
    evidence: Optional[list[str]] = None,
    risks: Optional[list[str]] = None,
    next_action: str = "none",
    error: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "object": "hermes.a2a_consult",
        "target": target,
        "status": status,
        "position": position,
        "confidence": confidence,
        "evidence": evidence or [],
        "risks": risks or [],
        "next_action": next_action,
        "run_id": run_id,
    }
    if error:
        payload["error"] = redact_secrets(error)
    return payload


async def consult(
    request: A2AConsultRequest,
    targets: Mapping[str, A2ATarget],
    *,
    session: Any = None,
    inbox_store: Any = None,
    sender: str = "caller",
) -> Dict[str, Any]:
    """Run or enqueue a bounded private consultation.

    When ``inbox_store`` is provided, this is a compatibility wrapper over the
    unified A2A inbox path and does not start a remote /v1/runs job. Without a
    store, it preserves the legacy direct /v1/runs behavior for older callers.
    """

    _validate_request(request)
    target = resolve_target(request.target, targets)
    if inbox_store is not None:
        return enqueue_consult_envelope(request, inbox_store, sender=sender)
    owns_session = session is None
    if session is None:
        if aiohttp is None:
            raise RuntimeError("aiohttp is required for A2A consults")
        session = aiohttp.ClientSession()

    run_id: Optional[str] = None
    deadline = asyncio.get_running_loop().time() + request.timeout_seconds

    try:
        try:
            start_resp = await asyncio.wait_for(
                session.post(
                    target.runs_url,
                    json={"input": _consult_prompt(request)},
                    headers=target.request_headers(),
                ),
                timeout=max(0.001, deadline - asyncio.get_running_loop().time()),
            )
            start_status = getattr(start_resp, "status", None)
            if start_status == 401:
                return _annotate_handoff_contract(_base_contract(
                    target=target.name,
                    status="unauthorized",
                    confidence="high",
                    evidence=["target /v1/runs returned 401"],
                    risks=["consult target credentials are invalid or missing"],
                    next_action="fix target API credential; do not retry with alternate credentials",
                    error="unauthorized",
                ), request)
            if start_status is None or start_status >= 400:
                body = await _safe_response_text(start_resp)
                return _annotate_handoff_contract(_base_contract(
                    target=target.name,
                    status="failed",
                    confidence="medium",
                    evidence=[f"target /v1/runs returned HTTP {start_status}"],
                    risks=["consult did not start"],
                    next_action="inspect target API server status/config",
                    error=body or f"HTTP {start_status}",
                ), request)
            try:
                start_body = await start_resp.json()
            except Exception as exc:
                return _annotate_handoff_contract(_base_contract(
                    target=target.name,
                    status="failed",
                    confidence="medium",
                    evidence=["target /v1/runs response was not valid JSON"],
                    risks=["cannot poll or stop remote consult without run_id"],
                    next_action="verify target /v1/runs response contract",
                    error=str(exc),
                ), request)
            if not isinstance(start_body, Mapping):
                return _annotate_handoff_contract(_base_contract(
                    target=target.name,
                    status="failed",
                    confidence="medium",
                    evidence=["target /v1/runs response was not a JSON object"],
                    risks=["cannot poll or stop remote consult without run_id"],
                    next_action="verify target /v1/runs response contract",
                    error="invalid start response",
                ), request)
            run_id = str(start_body.get("run_id") or "") or None
            if not run_id:
                return _annotate_handoff_contract(_base_contract(
                    target=target.name,
                    status="failed",
                    confidence="medium",
                    evidence=["target /v1/runs response did not include run_id"],
                    risks=["cannot poll or stop remote consult without run_id"],
                    next_action="verify target /v1/runs response contract",
                    error="missing run_id",
                ), request)
        except asyncio.TimeoutError:
            return _annotate_handoff_contract(_base_contract(
                target=target.name,
                status="timeout",
                confidence="medium",
                evidence=["timed out while starting target /v1/runs"],
                risks=["remote run may not have been created"],
                next_action="retry later after checking target health",
                error="start timeout",
            ), request)
        except Exception as exc:
            return _annotate_handoff_contract(_base_contract(
                target=target.name,
                status="failed",
                confidence="medium",
                evidence=["target /v1/runs request failed before run_id was returned"],
                risks=["consult did not start"],
                next_action="inspect target API server reachability/config before retrying",
                error=str(exc),
            ), request)

        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await _stop_run(session, target, run_id)
                return _annotate_handoff_contract(_base_contract(
                    target=target.name,
                    status="timeout",
                    run_id=run_id,
                    confidence="high",
                    evidence=[
                        f"bounded wait exceeded {request.timeout_seconds:g}s",
                        f"stop requested via /v1/runs/{run_id}/stop",
                    ],
                    risks=["remote target may still be unwinding current step"],
                    next_action="treat consult as unavailable; check run status before reusing advice",
                    error="consult timeout",
                ), request)

            try:
                poll_resp = await asyncio.wait_for(
                    session.get(
                        target.run_url(run_id),
                        headers=target.request_headers(),
                    ),
                    timeout=max(0.001, remaining),
                )
            except asyncio.TimeoutError:
                await _stop_run(session, target, run_id)
                return _annotate_handoff_contract(_base_contract(
                    target=target.name,
                    status="timeout",
                    run_id=run_id,
                    confidence="high",
                    evidence=[
                        f"bounded wait exceeded {request.timeout_seconds:g}s",
                        f"stop requested via /v1/runs/{run_id}/stop",
                    ],
                    risks=["remote target may still be unwinding current step"],
                    next_action="treat consult as unavailable; check run status before reusing advice",
                    error="consult timeout",
                ), request)
            except Exception as exc:
                return _annotate_handoff_contract(_base_contract(
                    target=target.name,
                    status="failed",
                    run_id=run_id,
                    confidence="medium",
                    evidence=["target run status request failed"],
                    risks=["consult status unavailable"],
                    next_action="inspect target run/API server reachability before retrying",
                    error=str(exc),
                ), request)
            poll_status = getattr(poll_resp, "status", None)
            if poll_status == 401:
                return _annotate_handoff_contract(_base_contract(
                    target=target.name,
                    status="unauthorized",
                    run_id=run_id,
                    confidence="high",
                    evidence=["target run status returned 401"],
                    risks=["consult status unavailable"],
                    next_action="fix target API credential; do not retry with alternate credentials",
                    error="unauthorized",
                ), request)
            if poll_status is None or poll_status >= 400:
                body = await _safe_response_text(poll_resp)
                return _annotate_handoff_contract(_base_contract(
                    target=target.name,
                    status="failed",
                    run_id=run_id,
                    confidence="medium",
                    evidence=[f"target run status returned HTTP {poll_status}"],
                    risks=["consult status unavailable"],
                    next_action="inspect target run state manually",
                    error=body or f"HTTP {poll_status}",
                ), request)

            try:
                status_body = await poll_resp.json()
            except Exception as exc:
                return _annotate_handoff_contract(_base_contract(
                    target=target.name,
                    status="failed",
                    run_id=run_id,
                    confidence="medium",
                    evidence=["target run status response was not valid JSON"],
                    risks=["consult status unavailable"],
                    next_action="inspect target run/API server response before retrying",
                    error=str(exc),
                ), request)
            if not isinstance(status_body, Mapping):
                return _annotate_handoff_contract(_base_contract(
                    target=target.name,
                    status="failed",
                    run_id=run_id,
                    confidence="medium",
                    evidence=["target run status response was not a JSON object"],
                    risks=["consult status unavailable"],
                    next_action="inspect target run/API server response before retrying",
                    error="invalid status response",
                ), request)
            state = str(status_body.get("status") or "unknown")
            if state in _APPROVAL_STATUSES:
                return _annotate_handoff_contract(_base_contract(
                    target=target.name,
                    status="approval_needed",
                    run_id=run_id,
                    position="approval required before consult can continue",
                    confidence="high",
                    evidence=["target status is waiting_for_approval"],
                    risks=["dangerous or sensitive action was requested by the target agent"],
                    next_action="human must review and approve or deny on the target; wrapper will not auto-approve",
                ), request)
            if state == "completed":
                output = redact_secrets(status_body.get("output") or "")
                return _annotate_handoff_contract(_base_contract(
                    target=target.name,
                    status="completed",
                    run_id=run_id,
                    position=output,
                    confidence=request.confidence,
                    evidence=["target run completed", f"run_id={run_id}"],
                    risks=[],
                    next_action="use as private advice only; record durable decisions in Kanban if needed",
                ), request)
            if state in _TERMINAL_STATUSES:
                return _annotate_handoff_contract(_base_contract(
                    target=target.name,
                    status=state,
                    run_id=run_id,
                    confidence="medium",
                    evidence=[f"target run ended with status {state}"],
                    risks=["consult did not produce usable advice"],
                    next_action="inspect target run logs/status before retrying",
                    error=status_body.get("error") or state,
                ), request)

            await asyncio.sleep(min(request.poll_interval_seconds, max(0.0, remaining)))
    finally:
        if owns_session:
            await session.close()


async def _safe_response_text(response: Any) -> str:
    try:
        return redact_secrets(await response.text())
    except Exception:
        return ""


async def _stop_run(session: Any, target: A2ATarget, run_id: str) -> None:
    try:
        await asyncio.wait_for(
            session.post(target.stop_url(run_id), headers=target.request_headers()),
            timeout=2.0,
        )
    except Exception:
        # Timeout cleanup is best-effort; the caller still receives a structured
        # timeout state and must not rely on the consult result.
        return


def _float_payload_value(payload: Mapping[str, Any], field: str, default: float) -> float:
    """Coerce optional numeric payload fields without treating 0 as missing."""

    raw_value = payload.get(field, default)
    if raw_value is None:
        raw_value = default
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise A2AConsultError("timeout_seconds and poll_interval_seconds must be numbers") from exc
    if not math.isfinite(value):
        raise A2AConsultError("timeout_seconds and poll_interval_seconds must be finite")
    return value


def _optional_float_payload_value(payload: Mapping[str, Any], field: str) -> Optional[float]:
    if field not in payload or payload.get(field) is None or payload.get(field) == "":
        return None
    return _float_payload_value(payload, field, 0.0)


def request_from_payload(payload: Mapping[str, Any]) -> A2AConsultRequest:
    """Build a validated request object from an API JSON payload."""

    if not isinstance(payload, Mapping):
        raise A2AConsultError("A2A consult payload must be a JSON object")

    timeout_seconds = _float_payload_value(payload, "timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    poll_interval_seconds = _float_payload_value(
        payload,
        "poll_interval_seconds",
        _DEFAULT_POLL_INTERVAL_SECONDS,
    )

    return A2AConsultRequest(
        target=str(payload.get("target") or ""),
        prompt=str(payload.get("prompt") or payload.get("input") or ""),
        notes=str(payload.get("notes") or ""),
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        confidence=str(payload.get("confidence") or "unknown"),
        kind=str(payload.get("kind") or "consult"),
        topic=str(payload.get("topic") or ""),
        summary=str(payload.get("summary") or ""),
        state=str(payload.get("state") or ""),
        decisions=_compact_list(payload.get("decisions")),
        context=str(payload.get("context") or ""),
        open_questions=_compact_list(payload.get("open_questions")),
        evidence_links=_compact_list(payload.get("evidence_links") or payload.get("links")),
        origin_channel=str(payload.get("origin_channel") or ""),
        ttl_seconds=_optional_float_payload_value(payload, "ttl_seconds"),
        expires_at=str(payload.get("expires_at") or ""),
        ack_required=_bool_payload_value(payload.get("ack_required"), default=True),
    )
