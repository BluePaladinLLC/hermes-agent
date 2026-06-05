"""Tests for private A2A consult wrapper over /v1/runs."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.a2a_consult import (
    A2AConsultError,
    A2AConsultRequest,
    consult,
    detect_secret,
    normalize_targets,
    redact_secrets,
    request_from_payload,
)
from gateway.config import PlatformConfig
from gateway.platforms import api_server as api_server_module
from gateway.platforms.api_server import APIServerAdapter


class _FakeResponse:
    def __init__(self, status, payload=None, text="", json_exc=None):
        self.status = status
        self._payload = payload or {}
        self._text = text
        self._json_exc = json_exc

    async def json(self):
        if self._json_exc:
            raise self._json_exc
        return self._payload

    async def text(self):
        return self._text


class _FakeSession:
    def __init__(self, *, post_responses=None, get_responses=None, post_delay: float = 0, get_delay: float = 0):
        self.post_responses = list(post_responses or [])
        self.get_responses = list(get_responses or [])
        self.post_delay = post_delay
        self.get_delay = get_delay
        self.posts = []
        self.gets = []

    async def post(self, url, **kwargs):
        if self.post_delay:
            await asyncio.sleep(self.post_delay)
        self.posts.append((url, kwargs))
        if self.post_responses:
            response = self.post_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return _FakeResponse(200, {})

    async def get(self, url, **kwargs):
        if self.get_delay:
            await asyncio.sleep(self.get_delay)
        self.gets.append((url, kwargs))
        if self.get_responses:
            response = self.get_responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return _FakeResponse(200, {"status": "running"})


@pytest.mark.asyncio
async def test_unknown_target_fails_closed_before_remote_call():
    session = _FakeSession()
    with pytest.raises(A2AConsultError, match="Unknown A2A target"):
        await consult(
            A2AConsultRequest(target="missing", prompt="give advice"),
            normalize_targets({"pons": "http://127.0.0.1:8643"}),
            session=session,
        )
    assert session.posts == []
    assert session.gets == []


@pytest.mark.parametrize(
    "secret_text",
    [
        "Authorization: Bearer fakebearertoken123",
        "api_key=supersecretvalue",
        "-----BEGIN FAKE PRIVATE KEY-----",
        "AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP",
        "slack_token=xoxb-123456789012-abcdefghijklmnopqr",
    ],
)
def test_secret_detection_rejects_prompt_or_notes(secret_text):
    assert detect_secret(secret_text)


@pytest.mark.asyncio
async def test_secret_prompt_rejected_without_remote_call():
    session = _FakeSession()
    with pytest.raises(A2AConsultError, match="contains"):
        await consult(
            A2AConsultRequest(target="pons", prompt="please use token=supersecretvalue"),
            normalize_targets({"pons": "http://127.0.0.1:8643"}),
            session=session,
        )
    assert session.posts == []


@pytest.mark.asyncio
async def test_unbounded_timeout_values_are_rejected_without_remote_call():
    session = _FakeSession()
    with pytest.raises(A2AConsultError, match="timeout_seconds must be <="):
        await consult(
            A2AConsultRequest(target="pons", prompt="review this", timeout_seconds=999999),
            normalize_targets({"pons": "http://127.0.0.1:8643"}),
            session=session,
        )
    assert session.posts == []


@pytest.mark.asyncio
async def test_non_finite_timeout_values_are_rejected_without_remote_call():
    session = _FakeSession()
    with pytest.raises(A2AConsultError, match="timeout_seconds must be finite"):
        await consult(
            A2AConsultRequest(target="pons", prompt="review this", timeout_seconds=float("nan")),
            normalize_targets({"pons": "http://127.0.0.1:8643"}),
            session=session,
        )
    assert session.posts == []


def test_payload_rejects_non_finite_timeout_values():
    with pytest.raises(A2AConsultError, match="must be finite"):
        request_from_payload({"target": "pons", "prompt": "review this", "timeout_seconds": "NaN"})


def test_payload_rejects_non_finite_poll_values():
    with pytest.raises(A2AConsultError, match="must be finite"):
        request_from_payload({"target": "pons", "prompt": "review this", "poll_interval_seconds": "Infinity"})


def test_payload_preserves_zero_timeout_for_validation_rejection():
    request = request_from_payload({"target": "pons", "prompt": "review this", "timeout_seconds": 0})
    assert request.timeout_seconds == 0


def test_payload_builds_handoff_request_without_prompt():
    request = request_from_payload(
        {
            "target": "pons",
            "kind": "handoff",
            "topic": "release review",
            "summary": "handoff summary",
            "state": "tests are green",
            "decisions": ["ship via API lane"],
            "context": "sanitized context",
            "open_questions": ["approve?"],
            "evidence_links": ["https://example.test/evidence"],
            "ttl_seconds": 60,
        }
    )

    assert request.prompt == ""
    assert request.is_state_handoff
    assert request.topic == "release review"
    assert request.decisions == ["ship via API lane"]
    assert request.ttl_seconds == 60


@pytest.mark.asyncio
async def test_handoff_requires_topic_and_summary_without_remote_call():
    session = _FakeSession()
    with pytest.raises(A2AConsultError, match="topic is required"):
        await consult(
            A2AConsultRequest(target="pons", prompt="", kind="handoff", summary="missing topic"),
            normalize_targets({"pons": "http://127.0.0.1:8643"}),
            session=session,
        )
    assert session.posts == []


@pytest.mark.asyncio
async def test_plain_consult_still_requires_prompt_without_remote_call():
    session = _FakeSession()
    with pytest.raises(A2AConsultError, match="prompt is required"):
        await consult(
            A2AConsultRequest(target="pons", prompt=""),
            normalize_targets({"pons": "http://127.0.0.1:8643"}),
            session=session,
        )
    assert session.posts == []


@pytest.mark.asyncio
async def test_handoff_evidence_links_reject_secret_like_values_without_remote_call():
    session = _FakeSession()
    with pytest.raises(A2AConsultError, match="contains"):
        await consult(
            A2AConsultRequest(
                target="pons",
                prompt="",
                kind="state_update",
                topic="topic",
                summary="summary",
                evidence_links=["https://example.test/?token=supersecretvalue"],
            ),
            normalize_targets({"pons": "http://127.0.0.1:8643"}),
            session=session,
        )
    assert session.posts == []


def test_payload_rejects_non_object_json():
    with pytest.raises(A2AConsultError, match="payload must be a JSON object"):
        request_from_payload(["not", "an", "object"])


def test_target_base_url_userinfo_credentials_fail_closed():
    with pytest.raises(A2AConsultError, match="base_url must not include credentials"):
        normalize_targets({"pons": "https://user:pass@example.test"})


@pytest.mark.asyncio
async def test_api_route_rejects_malformed_json_with_contract():
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"a2a_targets": {"pons": "http://127.0.0.1:8643"}})
    )
    async with TestClient(TestServer(_create_a2a_app(adapter))) as cli:
        resp = await cli.post(
            "/v1/a2a/consult",
            data="{bad json",
            headers={"Content-Type": "application/json"},
        )
        data = await resp.json()

    assert resp.status == 400
    assert data["delivery"] == "api:/v1/a2a/consult -> target:/v1/runs"
    assert isinstance(data["elapsed_seconds"], float)
    assert data == {
        "object": "hermes.a2a_consult",
        "target": "",
        "status": "rejected",
        "position": "unavailable",
        "confidence": "high",
        "evidence": ["local A2A consult validation failed"],
        "risks": ["consult was not started"],
        "next_action": "send a valid JSON object before retrying",
        "run_id": None,
        "error": "Invalid JSON",
        "failure_reason": "Invalid JSON",
        "delivery": "api:/v1/a2a/consult -> target:/v1/runs",
        "elapsed_seconds": data["elapsed_seconds"],
    }


@pytest.mark.asyncio
async def test_api_route_maps_zero_timeout_to_validation_400():
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"a2a_targets": {"pons": "http://127.0.0.1:8643"}})
    )
    async with TestClient(TestServer(_create_a2a_app(adapter))) as cli:
        resp = await cli.post(
            "/v1/a2a/consult",
            json={"target": "pons", "prompt": "review", "timeout_seconds": 0},
        )
        data = await resp.json()
    assert resp.status == 400
    assert data["status"] == "rejected"
    assert data["run_id"] is None
    assert "timeout_seconds must be positive" in data["error"]


@pytest.mark.asyncio
async def test_api_route_rejects_non_object_payload_with_contract():
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"a2a_targets": {"pons": "http://127.0.0.1:8643"}})
    )
    async with TestClient(TestServer(_create_a2a_app(adapter))) as cli:
        resp = await cli.post("/v1/a2a/consult", json=["not", "an", "object"])
        data = await resp.json()

    assert resp.status == 400
    assert data["object"] == "hermes.a2a_consult"
    assert data["status"] == "rejected"
    assert data["target"] == ""
    assert data["run_id"] is None
    assert "payload must be a JSON object" in data["error"]


@pytest.mark.asyncio
async def test_api_route_redacts_secret_like_target_in_rejection_contract():
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"a2a_targets": {"pons": "http://127.0.0.1:8643"}})
    )
    secret_target = "ghp_ab...3456"
    async with TestClient(TestServer(_create_a2a_app(adapter))) as cli:
        resp = await cli.post("/v1/a2a/consult", json={"target": secret_target, "prompt": "review"})
        data = await resp.json()

    assert resp.status == 404
    assert secret_target not in str(data)
    assert data["target"] == "[REDACTED:github_token]"
    assert "[REDACTED:github_token]" in data["error"]


def test_remote_output_is_redacted_before_contract_position():
    redacted = redact_secrets("position ok but token=supersecretvalue should not leak")
    assert "supersecretvalue" not in redacted
    assert "[REDACTED:assignment_secret]" in redacted


@pytest.mark.asyncio
async def test_completed_consult_returns_required_contract_and_run_id():
    session = _FakeSession(
        post_responses=[_FakeResponse(202, {"run_id": "run_123"})],
        get_responses=[_FakeResponse(200, {"status": "completed", "output": "ship it"})],
    )
    result = await consult(
        A2AConsultRequest(target="pons", prompt="review this", confidence="medium"),
        normalize_targets({"pons": {"base_url": "http://127.0.0.1:8643", "api_key": "secret-key"}}),
        session=session,
    )

    assert result["status"] == "completed"
    assert result["position"] == "ship it"
    assert result["confidence"] == "medium"
    assert result["run_id"] == "run_123"
    assert "evidence" in result
    assert "risks" in result
    assert "next_action" in result
    assert session.posts[0][1]["headers"]["Authorization"] == "Bearer secret-key"


@pytest.mark.asyncio
async def test_completed_handoff_posts_envelope_and_returns_origin_summary():
    session = _FakeSession(
        post_responses=[_FakeResponse(202, {"run_id": "run_handoff"})],
        get_responses=[_FakeResponse(200, {"status": "completed", "output": "ack_status: accepted\nrecord_status: recorded\nrecord_handle: todo:a2a-handoff-topic\nposition: ACK accepted and local todo recorded"})],
    )
    result = await consult(
        A2AConsultRequest(
            target="pons",
            prompt="ack only",
            kind="handoff",
            topic="handoff topic",
            summary="compact summary",
            state="state snapshot",
            decisions=["decision one"],
            context="context note",
            open_questions=["question one"],
            evidence_links=["https://example.test/evidence"],
            origin_channel="discord:#ops",
            confidence="medium",
        ),
        normalize_targets({"pons": "http://127.0.0.1:8643"}),
        session=session,
    )

    start_payload = session.posts[0][1]["json"]
    assert start_payload.keys() == {"input"}
    assert '"object": "hermes.a2a_state_envelope"' in start_payload["input"]
    assert '"kind": "handoff"' in start_payload["input"]
    assert '"topic": "handoff topic"' in start_payload["input"]
    assert "record a lightweight local action/state pointer" in start_payload["input"]
    assert "using the todo tool if available" in start_payload["input"]
    assert "record_status" in start_payload["input"]
    assert "ack only" in start_payload["input"]
    assert result["status"] == "completed"
    assert result["ack_status"] == "accepted"
    assert result["record_status"] == "recorded"
    assert result["record_handle"] == "todo:a2a-handoff-topic"
    assert result["handoff_kind"] == "handoff"
    assert result["topic"] == "handoff topic"
    assert result["summary"] == "compact summary"
    assert result["run_id"] == "run_handoff"
    assert "A2A handoff to pons" in result["origin_summary"]
    assert "record: recorded" in result["origin_summary"]
    assert "state snapshot" not in result["origin_summary"]


@pytest.mark.asyncio
async def test_valid_state_update_envelope_can_skip_ack_and_post_origin_summary_without_relay_or_kanban():
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    session = _FakeSession(
        post_responses=[_FakeResponse(202, {"run_id": "run_state_update"})],
        get_responses=[_FakeResponse(200, {"status": "completed", "output": "received"})],
    )

    result = await consult(
        A2AConsultRequest(
            target="pons",
            kind="state_update",
            topic="handoff topic",
            summary="compact summary",
            state="ephemeral state only",
            ack_required=False,
            expires_at=expires_at,
        ),
        normalize_targets({"pons": "http://127.0.0.1:8643"}),
        session=session,
    )

    start_payload = session.posts[0][1]["json"]
    assert start_payload.keys() == {"input"}
    prompt = start_payload["input"]
    assert '\"kind\": \"state_update\"' in prompt
    assert '\"ack_required\": false' in prompt
    assert f'"expires_at": "{expires_at}"' in prompt
    assert "Semantic ACK is optional" in prompt
    assert "using the todo tool if available" in prompt
    assert "Do not create or mutate Kanban tasks" in prompt
    assert "Bruno" not in prompt
    assert "Chaves" not in prompt

    assert result["status"] == "completed"
    assert result["run_id"] == "run_state_update"
    assert result["ack_status"] == "not_required"
    assert result["origin_summary"].startswith("A2A state_update to pons")
    assert "ACK: not_required" in result["origin_summary"]
    assert "ephemeral state only" not in result["origin_summary"]


@pytest.mark.asyncio
async def test_completed_handoff_without_semantic_ack_reports_received_only():
    session = _FakeSession(
        post_responses=[_FakeResponse(202, {"run_id": "run_handoff"})],
        get_responses=[_FakeResponse(200, {"status": "completed", "output": "stored for later"})],
    )
    result = await consult(
        A2AConsultRequest(
            target="pons",
            kind="state_update",
            topic="handoff topic",
            summary="compact summary",
        ),
        normalize_targets({"pons": "http://127.0.0.1:8643"}),
        session=session,
    )

    assert result["status"] == "completed"
    assert result["ack_status"] == "received"
    assert "ACK: received" in result["origin_summary"]


@pytest.mark.asyncio
async def test_handoff_rejects_missing_summary_without_remote_call():
    session = _FakeSession()
    with pytest.raises(A2AConsultError, match="summary is required"):
        await consult(
            A2AConsultRequest(target="pons", kind="handoff", topic="topic", summary=""),
            normalize_targets({"pons": "http://127.0.0.1:8643"}),
            session=session,
        )
    assert session.posts == []


@pytest.mark.asyncio
async def test_handoff_rejects_unsupported_kind_without_remote_call():
    session = _FakeSession()
    with pytest.raises(A2AConsultError, match="kind must be"):
        await consult(
            A2AConsultRequest(target="pons", kind="durable_task", topic="topic", summary="summary"),
            normalize_targets({"pons": "http://127.0.0.1:8643"}),
            session=session,
        )
    assert session.posts == []


@pytest.mark.asyncio
async def test_handoff_rejects_expired_ttl_and_expires_at_without_remote_call():
    targets = normalize_targets({"pons": "http://127.0.0.1:8643"})

    ttl_session = _FakeSession()
    with pytest.raises(A2AConsultError, match="ttl_seconds must be a positive finite number"):
        await consult(
            A2AConsultRequest(target="pons", kind="handoff", topic="topic", summary="summary", ttl_seconds=0),
            targets,
            session=ttl_session,
        )
    assert ttl_session.posts == []

    expires_session = _FakeSession()
    with pytest.raises(A2AConsultError, match="expires_at must be in the future"):
        await consult(
            A2AConsultRequest(
                target="pons",
                kind="state_update",
                topic="topic",
                summary="summary",
                expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            ),
            targets,
            session=expires_session,
        )
    assert expires_session.posts == []


def test_payload_accepts_ack_required_and_future_expires_at_for_handoff():
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    request = request_from_payload(
        {
            "target": "pons",
            "kind": "handoff",
            "topic": "topic",
            "summary": "summary",
            "ack_required": False,
            "expires_at": expires_at,
        }
    )

    assert request.ack_required is False
    assert request.expires_at == expires_at


def test_target_api_key_can_be_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("PONS_API_SERVER_KEY", "pons-secret-key")
    targets = normalize_targets(
        {"pons": {"base_url": "http://127.0.0.1:8643", "api_key_env": "PONS_API_SERVER_KEY"}}
    )

    assert targets["pons"].request_headers()["Authorization"] == "Bearer pons-secret-key"


def test_target_cannot_mix_inline_key_and_key_env(monkeypatch):
    monkeypatch.setenv("PONS_API_SERVER_KEY", "pons-secret-key")
    with pytest.raises(A2AConsultError, match="must not set both"):
        normalize_targets(
            {
                "pons": {
                    "base_url": "http://127.0.0.1:8643",
                    "api_key": "inline-secret",
                    "api_key_env": "PONS_API_SERVER_KEY",
                }
            }
        )


def test_invalid_target_key_env_name_is_rejected():
    with pytest.raises(A2AConsultError, match="Invalid api_key_env"):
        normalize_targets({"pons": {"base_url": "http://127.0.0.1:8643", "api_key_env": "bad-name"}})


def test_missing_target_api_key_env_fails_closed(monkeypatch):
    monkeypatch.delenv("PONS_API_SERVER_KEY", raising=False)
    with pytest.raises(A2AConsultError, match="is not set"):
        normalize_targets(
            {"pons": {"base_url": "http://127.0.0.1:8643", "api_key_env": "PONS_API_SERVER_KEY"}}
        )


@pytest.mark.asyncio
async def test_unauthorized_is_clean_failure_without_secret_leak_or_retry():
    session = _FakeSession(post_responses=[_FakeResponse(401, text="bad key")])
    result = await consult(
        A2AConsultRequest(target="pons", prompt="review this"),
        normalize_targets({"pons": {"base_url": "http://127.0.0.1:8643", "api_key": "secret-key"}}),
        session=session,
    )

    assert result["status"] == "unauthorized"
    assert result["run_id"] is None
    assert result["next_action"].startswith("fix target API credential")
    assert "secret-key" not in str(result)
    assert len(session.posts) == 1
    assert session.gets == []


@pytest.mark.asyncio
async def test_start_transport_error_returns_structured_failure_without_secret_leak():
    session = _FakeSession(post_responses=[OSError("connect failed token=supersecretvalue")])
    result = await consult(
        A2AConsultRequest(target="pons", prompt="review this"),
        normalize_targets({"pons": {"base_url": "http://127.0.0.1:8643", "api_key": "secret-key"}}),
        session=session,
    )

    assert result["status"] == "failed"
    assert result["run_id"] is None
    assert "target /v1/runs request failed" in result["evidence"][0]
    assert "supersecretvalue" not in str(result)
    assert "secret-key" not in str(result)
    assert result["error"] == "connect failed [REDACTED:assignment_secret]"
    assert session.gets == []


@pytest.mark.asyncio
async def test_start_invalid_json_returns_structured_failure_without_secret_leak():
    session = _FakeSession(
        post_responses=[_FakeResponse(202, json_exc=ValueError("bad json token=supersecretvalue"))]
    )
    result = await consult(
        A2AConsultRequest(target="pons", prompt="review this"),
        normalize_targets({"pons": {"base_url": "http://127.0.0.1:8643", "api_key": "secret-key"}}),
        session=session,
    )

    assert result["status"] == "failed"
    assert result["run_id"] is None
    assert result["evidence"] == ["target /v1/runs response was not valid JSON"]
    assert "supersecretvalue" not in str(result)
    assert "secret-key" not in str(result)
    assert result["error"] == "bad json [REDACTED:assignment_secret]"
    assert session.gets == []


@pytest.mark.asyncio
async def test_poll_transport_error_returns_structured_failure_with_run_id():
    session = _FakeSession(
        post_responses=[_FakeResponse(202, {"run_id": "run_transport"})],
        get_responses=[OSError("status socket closed token=supersecretvalue")],
    )
    result = await consult(
        A2AConsultRequest(target="pons", prompt="review this"),
        normalize_targets({"pons": "http://127.0.0.1:8643"}),
        session=session,
    )

    assert result["status"] == "failed"
    assert result["run_id"] == "run_transport"
    assert result["evidence"] == ["target run status request failed"]
    assert "supersecretvalue" not in str(result)
    assert result["error"] == "status socket closed [REDACTED:assignment_secret]"


@pytest.mark.asyncio
async def test_poll_invalid_json_returns_structured_failure_with_run_id():
    session = _FakeSession(
        post_responses=[_FakeResponse(202, {"run_id": "run_bad_json"})],
        get_responses=[_FakeResponse(200, json_exc=ValueError("status json token=supersecretvalue"))],
    )
    result = await consult(
        A2AConsultRequest(target="pons", prompt="review this"),
        normalize_targets({"pons": "http://127.0.0.1:8643"}),
        session=session,
    )

    assert result["status"] == "failed"
    assert result["run_id"] == "run_bad_json"
    assert result["evidence"] == ["target run status response was not valid JSON"]
    assert "supersecretvalue" not in str(result)
    assert result["error"] == "status json [REDACTED:assignment_secret]"


@pytest.mark.asyncio
async def test_approval_needed_is_propagated_without_auto_approval():
    session = _FakeSession(
        post_responses=[_FakeResponse(202, {"run_id": "run_approval"})],
        get_responses=[_FakeResponse(200, {"status": "waiting_for_approval"})],
    )
    result = await consult(
        A2AConsultRequest(target="pons", prompt="review this"),
        normalize_targets({"pons": "http://127.0.0.1:8643"}),
        session=session,
    )

    assert result["status"] == "approval_needed"
    assert result["run_id"] == "run_approval"
    assert "will not auto-approve" in result["next_action"]
    assert len(session.posts) == 1  # start only, no /approval call


@pytest.mark.asyncio
async def test_timeout_stops_remote_run_and_returns_structured_timeout():
    session = _FakeSession(
        post_responses=[_FakeResponse(202, {"run_id": "run_slow"}), _FakeResponse(200, {"status": "stopping"})],
        get_responses=[_FakeResponse(200, {"status": "running"}) for _ in range(5)],
    )
    result = await consult(
        A2AConsultRequest(
            target="pons",
            prompt="review this",
            timeout_seconds=0.01,
            poll_interval_seconds=0.01,
        ),
        normalize_targets({"pons": "http://127.0.0.1:8643"}),
        session=session,
    )

    assert result["status"] == "timeout"
    assert result["run_id"] == "run_slow"
    assert result["next_action"].startswith("treat consult as unavailable")
    assert "stop requested via /v1/runs/run_slow/stop" in result["evidence"]
    assert session.posts[-1][0].endswith("/v1/runs/run_slow/stop")


@pytest.mark.asyncio
async def test_hung_start_request_is_bounded_by_timeout():
    session = _FakeSession(post_delay=0.05)
    result = await consult(
        A2AConsultRequest(
            target="pons",
            prompt="review this",
            timeout_seconds=0.01,
            poll_interval_seconds=0.01,
        ),
        normalize_targets({"pons": "http://127.0.0.1:8643"}),
        session=session,
    )

    assert result["status"] == "timeout"
    assert result["run_id"] is None
    assert result["error"] == "start timeout"


@pytest.mark.asyncio
async def test_hung_poll_request_stops_remote_run_and_times_out():
    session = _FakeSession(
        post_responses=[_FakeResponse(202, {"run_id": "run_hung"}), _FakeResponse(200, {"status": "stopping"})],
        get_delay=0.05,
    )
    result = await consult(
        A2AConsultRequest(
            target="pons",
            prompt="review this",
            timeout_seconds=0.01,
            poll_interval_seconds=0.01,
        ),
        normalize_targets({"pons": "http://127.0.0.1:8643"}),
        session=session,
    )

    assert result["status"] == "timeout"
    assert result["run_id"] == "run_hung"
    assert "stop requested via /v1/runs/run_hung/stop" in result["evidence"]
    assert session.posts[-1][0].endswith("/v1/runs/run_hung/stop")


def _create_a2a_app(adapter):
    app = web.Application()
    app.router.add_post("/v1/a2a/consult", adapter._handle_a2a_consult)
    return app


@pytest.mark.asyncio
async def test_api_route_adds_delivery_timing_and_failure_reason(monkeypatch):
    async def fake_consult(request, targets):
        assert request.target == "pons"
        return {
            "object": "hermes.a2a_consult",
            "target": "pons",
            "status": "failed",
            "position": "unavailable",
            "confidence": "medium",
            "evidence": ["target /v1/runs returned HTTP 503"],
            "risks": ["consult did not start"],
            "next_action": "inspect target API server status/config",
            "run_id": "run_failed",
            "error": "HTTP 503",
        }

    monkeypatch.setattr(api_server_module, "run_a2a_consult", fake_consult)
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"a2a_targets": {"pons": "http://127.0.0.1:8643"}})
    )
    async with TestClient(TestServer(_create_a2a_app(adapter))) as cli:
        resp = await cli.post("/v1/a2a/consult", json={"target": "pons", "prompt": "review"})
        data = await resp.json()

    assert resp.status == 502
    assert data["delivery"] == "api:/v1/a2a/consult -> target:/v1/runs"
    assert isinstance(data["elapsed_seconds"], float)
    assert data["failure_reason"] == "HTTP 503"
    assert data["run_id"] == "run_failed"


@pytest.mark.asyncio
async def test_api_route_requires_auth_for_private_consult():
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "key": "server-key",
                "a2a_targets": {"pons": "http://127.0.0.1:8643"},
            },
        )
    )
    async with TestClient(TestServer(_create_a2a_app(adapter))) as cli:
        resp = await cli.post("/v1/a2a/consult", json={"target": "pons", "prompt": "review"})
    assert resp.status == 401


@pytest.mark.asyncio
async def test_api_route_maps_unknown_target_to_404():
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"a2a_targets": {"pons": "http://127.0.0.1:8643"}})
    )
    async with TestClient(TestServer(_create_a2a_app(adapter))) as cli:
        resp = await cli.post("/v1/a2a/consult", json={"target": "missing", "prompt": "review"})
        data = await resp.json()
    assert resp.status == 404
    assert data["status"] == "rejected"
    assert data["position"] == "unavailable"
    assert data["run_id"] is None
    assert "Unknown A2A target" in data["error"]


@pytest.mark.asyncio
async def test_api_route_maps_invalid_timeout_to_validation_400():
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"a2a_targets": {"pons": "http://127.0.0.1:8643"}})
    )
    async with TestClient(TestServer(_create_a2a_app(adapter))) as cli:
        resp = await cli.post(
            "/v1/a2a/consult",
            json={"target": "pons", "prompt": "review", "timeout_seconds": "not-a-number"},
        )
        data = await resp.json()
    assert resp.status == 400
    assert data["status"] == "rejected"
    assert data["run_id"] is None
    assert "must be numbers" in data["error"]
