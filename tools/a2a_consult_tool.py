"""Agent-to-agent consultation tool backed by the API server /v1/a2a/consult wrapper.

This is intentionally an ephemeral consultation lane, not a durable task queue.
Kanban remains the source of truth for multi-step or interruptible work.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

from tools.registry import registry, tool_error


A2A_CONSULT_SCHEMA = {
    "name": "a2a_consult",
    "description": (
        "Ask a configured Hermes co-agent for a private bounded consultation via the "
        "API/A2A lane. The tool starts a remote /v1/runs job, polls until terminal "
        "completion or timeout, and returns the final contract to this agent so it can "
        "surface exactly one summary in the current origin channel. Use kind='handoff' "
        "or kind='state_update' to package compact state, decisions, context, open "
        "questions, and evidence links into the agreed A2A envelope; the receiver is "
        "asked to record a lightweight local todo/state pointer before ACKing. Use this for "
        "quick advice/review or compact state transfer; use Kanban for durable "
        "multi-step work. Never include secrets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Configured A2A target name, e.g. 'pons', 'thalamus', or another configured co-agent.",
            },
            "prompt": {
                "type": "string",
                "description": "The bounded question or review request for consults. Optional caller instruction for handoff/state_update. Do not include secrets.",
            },
            "notes": {
                "type": "string",
                "description": "Optional sanitized context notes. Do not include secrets.",
            },
            "timeout_seconds": {
                "type": "number",
                "description": "Maximum seconds to wait for terminal completion. Defaults to 60, max 300.",
            },
            "poll_interval_seconds": {
                "type": "number",
                "description": "Polling interval in seconds. Defaults to 0.5.",
            },
            "confidence": {
                "type": "string",
                "description": "Caller confidence hint to attach to successful advice, e.g. low/medium/high/unknown.",
            },
            "kind": {
                "type": "string",
                "enum": ["consult", "handoff", "state_update"],
                "description": "Use 'handoff' or 'state_update' to package a compact A2A state envelope instead of a plain consult.",
            },
            "topic": {
                "type": "string",
                "description": "Required for handoff/state_update: short topic title for the receiving agent and origin summary.",
            },
            "summary": {
                "type": "string",
                "description": "Required for handoff/state_update: high-level compact summary, not raw transcript/state dump.",
            },
            "state": {
                "type": "string",
                "description": "Optional compact current state for handoff/state_update.",
            },
            "decisions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional compact decisions to include in the handoff/state_update envelope.",
            },
            "context": {
                "type": "string",
                "description": "Optional sanitized context for handoff/state_update. Do not include secrets.",
            },
            "open_questions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional unresolved questions for the target agent.",
            },
            "evidence_links": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional links or artifact references that support the handoff/state_update.",
            },
            "origin_channel": {
                "type": "string",
                "description": "Optional non-secret origin channel label to echo in the envelope.",
            },
            "ttl_seconds": {
                "type": "number",
                "description": "Optional positive TTL for the handoff/state_update envelope.",
            },
            "expires_at": {
                "type": "string",
                "description": "Optional ISO-8601 expiry timestamp for the handoff/state_update envelope. Mutually exclusive with ttl_seconds.",
            },
            "ack_required": {
                "type": "boolean",
                "description": "Whether the target must provide a semantic ACK. Defaults to true for handoff/state_update.",
            },
        },
        "required": ["target"],
    },
}


def _load_api_server_a2a_targets() -> Mapping[str, Any]:
    """Load configured A2A targets from the API server platform config."""

    from gateway.config import Platform, load_gateway_config

    config = load_gateway_config()
    api_config = config.platforms.get(Platform.API_SERVER)
    if not api_config or not api_config.enabled:
        return {}
    return (api_config.extra or {}).get("a2a_targets", {}) or {}


def check_requirements() -> bool:
    try:
        raw_targets = _load_api_server_a2a_targets()
        return bool(raw_targets)
    except Exception:
        return False


def _run_coro_sync(coro):
    """Run an async consult from the synchronous tool interface."""

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    if loop.is_running():
        # Tool handlers normally run outside an event loop, but keep a safe path
        # for embedded callers by using a short-lived helper thread.
        import threading

        box: dict[str, Any] = {}

        def _worker() -> None:
            try:
                box["result"] = asyncio.run(coro)
            except BaseException as exc:  # pragma: no cover - defensive path
                box["error"] = exc

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        thread.join()
        if "error" in box:
            raise box["error"]
        return box.get("result")

    return loop.run_until_complete(coro)


def a2a_consult_tool(args, **kw):
    """Run a bounded A2A consultation and return a user-visible contract."""

    try:
        from gateway.a2a_consult import consult, normalize_targets, request_from_payload

        payload = dict(args or {})
        # Tool default is a little friendlier than the low-level wrapper default,
        # because humans expect a co-agent to finish short advice in one turn.
        payload.setdefault("timeout_seconds", 60)
        payload.setdefault("poll_interval_seconds", 0.5)

        raw_targets = _load_api_server_a2a_targets()
        targets = normalize_targets(raw_targets)
        if not targets:
            return tool_error("No A2A targets configured on the API server platform")

        request = request_from_payload(payload)
        result = _run_coro_sync(consult(request, targets))
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("delivery", "tool:a2a_consult -> api:/v1/a2a/consult -> target:/v1/runs")
            result.setdefault(
                "origin_delivery",
                "returned to current agent; include the summary in the final response to the origin channel",
            )
            result.setdefault("lane", "API/A2A run")
        return json.dumps(result)
    except Exception as exc:
        return tool_error(f"A2A consult failed: {exc}")


registry.register(
    name="a2a_consult",
    toolset="a2a",
    schema=A2A_CONSULT_SCHEMA,
    handler=lambda args, **kw: a2a_consult_tool(args, **kw),
    check_fn=check_requirements,
    emoji="🔁",
)
