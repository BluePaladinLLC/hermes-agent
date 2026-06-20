from __future__ import annotations

from gateway.a2a_auth_policy import (
    A2AAuthPolicy,
    AuthDecision,
    MemoryReplayStore,
    sign_packet,
    verify_packet,
)


def base_packet(action_class="status", sender="pons", target="thalamus-sidecar"):
    return {
        "message_id": "msg-123",
        "sender": sender,
        "target": target,
        "topic_id": "topic-1",
        "message_type": "work_request",
        "payload": {"body": "hello"},
        "metadata": {"action_class": action_class},
    }


def policy(now=lambda: 1_780_000_010, replay_store=None):
    return A2AAuthPolicy(
        trusted_keys={"pons": {"pons-main": "test-secret"}},
        grants={"pons": {"thalamus-sidecar": {"status", "read_only", "coordination"}}},
        now=now,
        replay_store=replay_store or MemoryReplayStore(),
    )


def test_valid_signed_low_blast_status_is_allowed():
    packet = base_packet(action_class="status")
    sign_packet(packet, key_id="pons-main", secret="test-secret", timestamp=1_780_000_000, nonce="n1")

    decision = verify_packet(packet, policy())

    assert decision == AuthDecision.ALLOW
    assert decision.reason == "authorized"


def test_missing_signature_rejected_before_runtime_for_enforced_classes():
    decision = verify_packet(base_packet(action_class="status"), policy())

    assert decision == AuthDecision.REJECT
    assert decision.reason == "missing_signature"


def test_invalid_signature_rejected():
    packet = base_packet(action_class="status")
    sign_packet(packet, key_id="pons-main", secret="right-secret", timestamp=1_780_000_000, nonce="n1")
    packet["metadata"]["auth"]["signature"] = "bad"

    decision = verify_packet(packet, policy())

    assert decision == AuthDecision.REJECT
    assert decision.reason == "invalid_signature"


def test_valid_signature_but_unauthorized_sensitive_action_needs_human():
    packet = base_packet(action_class="service_mutation")
    sign_packet(packet, key_id="pons-main", secret="test-secret", timestamp=1_780_000_000, nonce="n1")

    decision = verify_packet(packet, policy())

    assert decision == AuthDecision.NEEDS_HUMAN
    assert decision.reason == "action_class_not_authorized:service_mutation"


def test_replay_of_valid_signature_is_rejected():
    packet = base_packet(action_class="status")
    sign_packet(packet, key_id="pons-main", secret="test-secret", timestamp=1_780_000_000, nonce="n1")
    store = MemoryReplayStore()
    auth_policy = policy(replay_store=store)

    assert verify_packet(packet, auth_policy) == AuthDecision.ALLOW
    assert verify_packet(packet, auth_policy) == AuthDecision.REJECT
    assert verify_packet(packet, auth_policy).reason == "replay_detected"


def test_expired_signature_rejected():
    packet = base_packet(action_class="status")
    sign_packet(packet, key_id="pons-main", secret="test-secret", timestamp=1_780_000_000, nonce="n1")
    auth_policy = A2AAuthPolicy(
        trusted_keys={"pons": {"pons-main": "test-secret"}},
        grants={"pons": {"thalamus-sidecar": {"status"}}},
        now=lambda: 1_780_001_000,
        replay_store=MemoryReplayStore(),
        max_skew_seconds=300,
    )

    decision = verify_packet(packet, auth_policy)

    assert decision == AuthDecision.REJECT
    assert decision.reason == "signature_expired"
