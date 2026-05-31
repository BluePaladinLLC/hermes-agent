"""Read-only Graffiti v2 tools for DVA temporal graph recall.

These tools intentionally expose only safe read/status operations against the
clean Graffiti v2 REST API. They do not know the ingest token and do not call
write/delete endpoints.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from tools.registry import registry

DEFAULT_API_BASE = "http://10.1.1.162:8895"
DEFAULT_GROUPS = [
    "graphiti-v2-clean-current-vagus",
    "graphiti-v2-clean-current",
]
HISTORICAL_GROUPS = [
    "graphiti-v2-clean-backfill-vagus-selected",
    "graphiti-v2-clean-ramp-phase-1",
]

CURRENTNESS_FACTORS = {
    "current": 1.25,
    "validated": 1.15,
    "unknown": 0.85,
    "stale": 0.6,
    "legacy": 0.5,
    "decommissioned": 0.35,
    "invalidated": 0.3,
}
ERA_FACTORS = {
    "hermes": 1.15,
    "github-current": 1.1,
    "claude_code": 0.75,
    "openclaw": 0.6,
}


def _api_base() -> str:
    return os.getenv("GRAFFITI_API_BASE_URL", DEFAULT_API_BASE).rstrip("/")


def _json_request(path: str, *, method: str = "GET", payload: Dict[str, Any] | None = None, timeout: int = 25) -> Any:
    url = _api_base() + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - configured LAN API
        raw = resp.read().decode("utf-8", errors="replace")
    if not raw:
        return None
    return json.loads(raw)


def _groups(value: Any) -> List[str]:
    if value is None or value == "":
        return list(DEFAULT_GROUPS)
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _effective_groups(args: Dict[str, Any]) -> List[str]:
    groups = _groups(args.get("group_ids"))
    if args.get("include_historical"):
        for group in HISTORICAL_GROUPS:
            if group not in groups:
                groups.append(group)
    return groups


def _limit(value: Any, default: int = 8, max_limit: int = 25) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(1, min(max_limit, n))


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _result_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("edges", "facts", "results", "nodes", "episodes"):
        value = data.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def _extract_text(item: dict[str, Any]) -> str:
    return str(item.get("fact") or item.get("content") or item.get("text") or item.get("name") or item.get("summary") or "")


def _metadata(item: dict[str, Any]) -> dict[str, Any]:
    attrs = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    merged: dict[str, Any] = {}
    merged.update(attrs)
    merged.update(meta)
    merged.update(item)
    return merged


def _infer_currentness(item: dict[str, Any], *, historical_mode: bool = False) -> str:
    meta = _metadata(item)
    explicit = str(meta.get("currentness") or meta.get("trust_label") or "").lower().replace("-", "_").strip()
    if explicit in CURRENTNESS_FACTORS:
        return explicit
    invalid = _parse_dt(meta.get("invalid_at") or meta.get("valid_until") or meta.get("expired_at"))
    if invalid and invalid <= datetime.now(timezone.utc):
        return "decommissioned"
    group = str(meta.get("group_id") or "").lower()
    if any(g in group for g in ("backfill", "ramp", "archive", "legacy")):
        return "legacy"
    text = _extract_text(item).lower()
    if re.search(r"\b(decommissioned|retired|sunset|superseded|obsolete|no longer live|not live)\b", text):
        return "decommissioned"
    if re.search(r"\b(smoke|canary|runtime verification|test artifact|stale|legacy|old corpus|old fleet|claude code era|openclaw era)\b", text):
        return "legacy"
    if historical_mode:
        return "validated"
    return "current"


def _infer_era(item: dict[str, Any]) -> str | None:
    meta = _metadata(item)
    era = str(meta.get("era") or "").lower().replace("-", "_").strip()
    if era in ERA_FACTORS:
        return era
    text = _extract_text(item).lower()
    if "openclaw" in text:
        return "openclaw"
    if "claude code" in text or "cloud code" in text:
        return "claude_code"
    if "hermes" in text:
        return "hermes"
    if "github" in text and "bluepaladinllc" in text:
        return "github-current"
    return None


def _temporal_dt(item: dict[str, Any]) -> datetime | None:
    meta = _metadata(item)
    for key in ("valid_at", "valid_from", "reference_time", "observed_at", "created_at", "updated_at"):
        dt = _parse_dt(meta.get(key))
        if dt:
            return dt
    return None


def _decay_factor(item: dict[str, Any], *, include_historical: bool = False) -> tuple[float, dict[str, Any]]:
    currentness = _infer_currentness(item, historical_mode=include_historical)
    era = _infer_era(item)
    factor = CURRENTNESS_FACTORS.get(currentness, CURRENTNESS_FACTORS["unknown"])
    if era:
        factor *= ERA_FACTORS.get(era, 1.0)

    dt = _temporal_dt(item)
    age_days = None
    if dt:
        age_days = max(0, (datetime.now(timezone.utc) - dt).days)
        if not include_historical:
            if age_days > 365:
                factor *= 0.45
            elif age_days > 180:
                factor *= 0.6
            elif age_days > 90:
                factor *= 0.75
            elif age_days > 30:
                factor *= 0.9

    warning = None
    if currentness in {"stale", "legacy", "decommissioned", "invalidated", "unknown"}:
        warning = f"{currentness}; use as historical/advisory unless cross-checked"

    return factor, {
        "currentness": currentness,
        "era": era,
        "age_days": age_days,
        "decay_factor": round(factor, 3),
        "warning": warning,
    }


def _apply_decay(data: Any, *, include_historical: bool = False) -> Any:
    items = _result_items(data)
    if not items:
        return data
    ranked = []
    for index, item in enumerate(items):
        factor, note = _decay_factor(item, include_historical=include_historical)
        base = item.get("score") or item.get("distance") or item.get("rank") or 1.0
        try:
            base_score = float(base)
        except (TypeError, ValueError):
            base_score = 1.0
        enriched = dict(item)
        enriched["decay"] = note
        enriched["decayed_score"] = round(base_score * factor, 6)
        ranked.append((base_score * factor, -index, enriched))
    ranked_items = [item for _, _, item in sorted(ranked, key=lambda x: (x[0], x[1]), reverse=True)]

    if isinstance(data, list):
        return ranked_items
    if isinstance(data, dict):
        out = dict(data)
        for key in ("edges", "facts", "results", "nodes", "episodes"):
            if isinstance(out.get(key), list):
                out[key] = ranked_items
                break
        out["decay_policy"] = {
            "mode": "read_time_soft_rerank_non_destructive",
            "default_groups": DEFAULT_GROUPS,
            "historical_opt_in": include_historical,
            "note": "Decay changes ranking/annotations only; it does not delete or rewrite Graffiti facts.",
        }
        return out
    return data


def _pack_result(data: Any, *, note: str = "") -> str:
    out = {
        "ok": True,
        "api_base": _api_base(),
        "data": data,
        "safety": "read_only; no write/delete endpoints exposed; default searches current groups only",
        "currentness_note": "Graffiti is temporal evidence. Decay is read-time, soft, non-destructive ranking/annotation; cross-check GitHub/canon for source-truth actions.",
    }
    if note:
        out["note"] = note
    return json.dumps(out, ensure_ascii=False, sort_keys=True)


def _error(message: str) -> str:
    return json.dumps({"ok": False, "error": message, "api_base": _api_base()}, ensure_ascii=False, sort_keys=True)


def graffiti_get_status(args: Dict[str, Any] | None = None, **_: Any) -> str:
    """Return Graffiti v2 API health/status."""
    try:
        return _pack_result(_json_request("/healthz", method="GET", timeout=10))
    except Exception as exc:  # noqa: BLE001 - tool errors should be structured
        return _error(f"Graffiti status failed: {type(exc).__name__}: {exc}")


def graffiti_get_episodes(args: Dict[str, Any] | None = None, **_: Any) -> str:
    """List recent episodes from clean Graffiti groups."""
    args = args or {}
    try:
        params = {"limit": str(_limit(args.get("limit"), default=10, max_limit=50))}
        group_ids = _effective_groups(args)
        if group_ids:
            params["group_ids"] = ",".join(group_ids)
        path = "/v1/episodes?" + urllib.parse.urlencode(params)
        return _pack_result(_json_request(path, method="GET"), note="Default groups are current-only. Set include_historical=true or explicit group_ids for backfill/ramp debug recall.")
    except Exception as exc:  # noqa: BLE001
        return _error(f"Graffiti episode listing failed: {type(exc).__name__}: {exc}")


def graffiti_search_facts(args: Dict[str, Any] | None = None, **_: Any) -> str:
    """Search Graffiti v2 facts/edges in clean groups."""
    args = args or {}
    query = str(args.get("query") or "").strip()
    if not query:
        return _error("query is required")
    try:
        include_historical = bool(args.get("include_historical"))
        payload = {
            "query": query,
            "limit": _limit(args.get("limit"), default=8, max_limit=25),
            "group_ids": _effective_groups(args),
        }
        data = _json_request("/v1/search", method="POST", payload=payload)
        return _pack_result(_apply_decay(data, include_historical=include_historical), note="Default groups are current-only. Set include_historical=true or explicit group_ids for backfill/ramp debug recall. Use for temporal/provenance recall; not broad semantic memory.")
    except Exception as exc:  # noqa: BLE001
        return _error(f"Graffiti fact search failed: {type(exc).__name__}: {exc}")


def graffiti_search_nodes(args: Dict[str, Any] | None = None, **_: Any) -> str:
    """Search Graffiti v2 entity/node matches in clean groups."""
    args = args or {}
    query = str(args.get("query") or "").strip()
    if not query:
        return _error("query is required")
    try:
        include_historical = bool(args.get("include_historical"))
        payload = {
            "query": query,
            "limit": _limit(args.get("limit"), default=8, max_limit=25),
            "group_ids": _effective_groups(args),
        }
        data = _json_request("/v1/search/nodes", method="POST", payload=payload)
        return _pack_result(_apply_decay(data, include_historical=include_historical), note="Default groups are current-only. Set include_historical=true or explicit group_ids for backfill/ramp debug recall. Use for entity lookup before fact/edge search.")
    except Exception as exc:  # noqa: BLE001
        return _error(f"Graffiti node search failed: {type(exc).__name__}: {exc}")


def check_requirements() -> bool:
    return True


registry.register(
    name="graffiti_get_status",
    toolset="memory",
    schema={
        "name": "graffiti_get_status",
        "description": "Read-only health/status check for clean DVA Graffiti v2 temporal graph API.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    handler=lambda args, **kw: graffiti_get_status(args, **kw),
    check_fn=check_requirements,
    emoji="🕸️",
)

registry.register(
    name="graffiti_get_episodes",
    toolset="memory",
    schema={
        "name": "graffiti_get_episodes",
        "description": "Read-only list of recent clean Graffiti v2 episodes. Defaults to current groups; historical/backfill groups are opt-in.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Maximum episodes to return, 1-50", "default": 10},
                "group_ids": {"type": ["array", "string"], "items": {"type": "string"}, "description": "Optional group IDs; default uses current Graffiti groups only"},
                "include_historical": {"type": "boolean", "description": "Include selected backfill/ramp groups for debug/provenance recall", "default": False},
            },
            "additionalProperties": False,
        },
    },
    handler=lambda args, **kw: graffiti_get_episodes(args, **kw),
    check_fn=check_requirements,
    emoji="🕸️",
)

registry.register(
    name="graffiti_search_facts",
    toolset="memory",
    schema={
        "name": "graffiti_search_facts",
        "description": "Read-only search of clean Graffiti v2 facts/edges for temporal/provenance recall. Applies read-time, non-destructive decay/reranking: current facts are boosted; legacy/decommissioned facts are dampened and annotated. Cross-check GitHub/canon before source-truth actions.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Maximum facts to return, 1-25", "default": 8},
                "group_ids": {"type": ["array", "string"], "items": {"type": "string"}, "description": "Optional group IDs; default uses current Graffiti groups only"},
                "include_historical": {"type": "boolean", "description": "Include selected backfill/ramp groups for debug/provenance recall", "default": False},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    handler=lambda args, **kw: graffiti_search_facts(args, **kw),
    check_fn=check_requirements,
    emoji="🕸️",
)

registry.register(
    name="graffiti_search_nodes",
    toolset="memory",
    schema={
        "name": "graffiti_search_nodes",
        "description": "Read-only search of clean Graffiti v2 entity/node matches. Applies read-time, non-destructive decay/reranking annotations. Use for entity lookup before fact/edge search.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Node/entity search query"},
                "limit": {"type": "integer", "description": "Maximum nodes to return, 1-25", "default": 8},
                "group_ids": {"type": ["array", "string"], "items": {"type": "string"}, "description": "Optional group IDs; default uses current Graffiti groups only"},
                "include_historical": {"type": "boolean", "description": "Include selected backfill/ramp groups for debug/provenance recall", "default": False},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    handler=lambda args, **kw: graffiti_search_nodes(args, **kw),
    check_fn=check_requirements,
    emoji="🕸️",
)
