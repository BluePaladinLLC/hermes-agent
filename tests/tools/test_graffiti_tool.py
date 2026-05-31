from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tools import graffiti_tool


def test_default_groups_are_current_only() -> None:
    assert graffiti_tool._effective_groups({}) == [
        "graphiti-v2-clean-current-vagus",
        "graphiti-v2-clean-current",
    ]


def test_historical_groups_are_opt_in() -> None:
    groups = graffiti_tool._effective_groups({"include_historical": True})
    assert "graphiti-v2-clean-current-vagus" in groups
    assert "graphiti-v2-clean-backfill-vagus-selected" in groups
    assert "graphiti-v2-clean-ramp-phase-1" in groups


def test_decay_reranks_current_before_legacy() -> None:
    current = {
        "fact": "Current Hermes A2A receipt bridge is active",
        "score": 1.0,
        "group_id": "graphiti-v2-clean-current-vagus",
        "reference_time": datetime.now(timezone.utc).isoformat(),
    }
    legacy = {
        "fact": "Old Claude Code era smoke canary",
        "score": 1.0,
        "group_id": "graphiti-v2-clean-ramp-phase-1",
        "reference_time": (datetime.now(timezone.utc) - timedelta(days=120)).isoformat(),
    }

    out = graffiti_tool._apply_decay({"facts": [legacy, current]})

    assert out["facts"][0]["fact"] == current["fact"]
    assert out["facts"][0]["decay"]["currentness"] == "current"
    assert out["facts"][1]["decay"]["currentness"] == "legacy"
    assert out["facts"][1]["decay"]["warning"]


def test_expired_fact_is_marked_decommissioned() -> None:
    expired = {
        "fact": "Former service endpoint",
        "valid_until": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
    }

    assert graffiti_tool._infer_currentness(expired) == "decommissioned"
