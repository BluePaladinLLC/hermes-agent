"""Tests for the a2a_consult agent tool."""

import json

from gateway.config import Platform, PlatformConfig


def test_a2a_consult_tool_waits_and_marks_api_lane(monkeypatch):
    from tools import a2a_consult_tool
    import gateway.a2a_consult as a2a_module

    async def fake_consult(request, targets):
        assert request.target == "pons"
        assert request.prompt == "review the handoff"
        assert targets["pons"].base_url == "http://127.0.0.1:8643"
        return {
            "object": "hermes.a2a_consult",
            "target": "pons",
            "status": "completed",
            "position": "ship it",
            "confidence": "medium",
            "evidence": ["target run completed", "run_id=run_abc"],
            "risks": [],
            "next_action": "surface the answer",
            "run_id": "run_abc",
        }

    monkeypatch.setattr(a2a_consult_tool, "_load_api_server_a2a_targets", lambda: {"pons": "http://127.0.0.1:8643"})
    monkeypatch.setattr(a2a_module, "consult", fake_consult)

    raw = a2a_consult_tool.a2a_consult_tool(
        {"target": "pons", "prompt": "review the handoff", "confidence": "medium"}
    )
    result = json.loads(raw)

    assert result["status"] == "completed"
    assert result["position"] == "ship it"
    assert result["run_id"] == "run_abc"
    assert result["lane"] == "API/A2A run"
    assert result["delivery"] == "tool:a2a_consult -> api:/v1/a2a/consult -> target:/v1/runs"
    assert "origin channel" in result["origin_delivery"]


def test_a2a_consult_tool_accepts_handoff_fields(monkeypatch):
    from tools import a2a_consult_tool
    import gateway.a2a_consult as a2a_module

    async def fake_consult(request, targets):
        assert request.kind == "state_update"
        assert request.topic == "topic"
        assert request.summary == "summary"
        assert request.decisions == ["decision"]
        assert request.open_questions == ["question"]
        assert request.evidence_links == ["https://example.test/evidence"]
        assert request.ack_required is False
        assert request.expires_at == "2099-01-01T00:00:00+00:00"
        return {
            "object": "hermes.a2a_consult",
            "target": "pons",
            "status": "completed",
            "position": "ACK accepted",
            "confidence": "medium",
            "evidence": ["target run completed", "run_id=run_state"],
            "risks": [],
            "next_action": "post origin summary",
            "run_id": "run_state",
            "ack_status": "accepted",
            "origin_summary": "A2A state_update to pons — topic: topic; summary: summary; ACK: accepted (completed); run_id: run_state",
        }

    monkeypatch.setattr(a2a_consult_tool, "_load_api_server_a2a_targets", lambda: {"pons": "http://127.0.0.1:8643"})
    monkeypatch.setattr(a2a_module, "consult", fake_consult)

    raw = a2a_consult_tool.a2a_consult_tool(
        {
            "target": "pons",
            "kind": "state_update",
            "topic": "topic",
            "summary": "summary",
            "decisions": ["decision"],
            "open_questions": ["question"],
            "evidence_links": ["https://example.test/evidence"],
            "ack_required": False,
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
    )
    result = json.loads(raw)

    assert result["status"] == "completed"
    assert result["ack_status"] == "accepted"
    assert result["run_id"] == "run_state"
    assert result["lane"] == "API/A2A run"
    assert "origin_summary" in result


def test_a2a_consult_tool_reports_missing_targets(monkeypatch):
    from tools import a2a_consult_tool

    monkeypatch.setattr(a2a_consult_tool, "_load_api_server_a2a_targets", lambda: {})

    result = json.loads(a2a_consult_tool.a2a_consult_tool({"target": "pons", "prompt": "review"}))

    assert result == {"error": "No A2A targets configured on the API server platform"}


def test_check_requirements_reads_api_server_targets(monkeypatch):
    from tools import a2a_consult_tool
    import gateway.config as gateway_config

    class Config:
        platforms = {
            Platform.API_SERVER: PlatformConfig(
                enabled=True,
                extra={"a2a_targets": {"pons": "http://127.0.0.1:8643"}},
            )
        }

    monkeypatch.setattr(gateway_config, "load_gateway_config", lambda: Config())

    assert a2a_consult_tool.check_requirements() is True


def test_check_requirements_false_without_api_targets(monkeypatch):
    from tools import a2a_consult_tool
    import gateway.config as gateway_config

    class Config:
        platforms = {Platform.API_SERVER: PlatformConfig(enabled=True, extra={})}

    monkeypatch.setattr(gateway_config, "load_gateway_config", lambda: Config())

    assert a2a_consult_tool.check_requirements() is False
