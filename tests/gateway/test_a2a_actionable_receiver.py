from __future__ import annotations

from gateway.a2a_actionable_receiver import A2AActionResult, process_actionable_once
from gateway.a2a_auth_policy import A2AAuthPolicy, MemoryReplayStore, sign_packet


class FakeA2AStore:
    def __init__(self, message=None):
        self.message = message
        self.enqueued = []
        self.completed = []

    def claim(self, *, target, consumer, block_ms):
        return self.message

    def enqueue(self, **kwargs):
        packet = {"message_id": f"reply-{len(self.enqueued) + 1}", **kwargs}
        self.enqueued.append(packet)
        return packet

    def complete(self, message_id, **kwargs):
        record = {"message_id": message_id, **kwargs}
        self.completed.append(record)
        return record


def _message(message_type="work_request", **overrides):
    message = {
        "message_id": "msg-1",
        "sender": "pons",
        "target": "axon",
        "message_type": message_type,
        "topic_id": "topic-1",
        "subject": "Provider smoke",
        "payload": {},
    }
    message.update(overrides)
    return message


def test_lifecycle_packets_are_completed_without_ack_or_reply_loop():
    for message_type in ("ack", "work_started", "needs_human", "work_result", "final", "timeout"):
        store = FakeA2AStore(_message(message_type))

        result = process_actionable_once(store=store, target="axon", consumer="worker-1", handlers={})

        assert result["ignored"] is True
        assert result["ack"] is None
        assert result["started"] is None
        assert result["reply"] is None
        assert store.enqueued == []
        assert store.completed == [
            {
                "message_id": "msg-1",
                "status": "completed",
                "result": f"Ignored non-actionable A2A lifecycle/status packet: {message_type}",
            }
        ]


def test_actionable_work_request_acks_starts_runs_handler_and_finalizes():
    store = FakeA2AStore(_message("work_request"))

    result = process_actionable_once(
        store=store,
        target="axon",
        consumer="worker-1",
        handlers={"work_request": lambda message: A2AActionResult(message_type="final", body="done")},
    )

    assert result["ack"]["message_type"] == "ack"
    assert result["started"]["message_type"] == "work_started"
    assert result["reply"]["message_type"] == "final"
    assert [packet["message_type"] for packet in store.enqueued] == ["ack", "work_started", "final"]
    assert store.completed == [{"message_id": "msg-1", "status": "completed", "result": "done", "evidence_links": []}]


def test_no_reply_actionable_runs_handler_and_completes_without_enqueue_loop():
    store = FakeA2AStore(_message("work_request", payload={"no_reply": True}))

    result = process_actionable_once(
        store=store,
        target="axon",
        consumer="worker-1",
        handlers={"work_request": lambda message: A2AActionResult(message_type="final", body="done")},
    )

    assert result["ack"] is None
    assert result["started"] is None
    assert result["reply"] is None
    assert store.enqueued == []
    assert store.completed == [{"message_id": "msg-1", "status": "completed", "result": "done", "evidence_links": []}]


def test_no_reply_marker_in_subject_suppresses_responses():
    store = FakeA2AStore(_message("handoff", subject="final-no-reply: receipt closeout"))

    result = process_actionable_once(
        store=store,
        target="axon",
        consumer="worker-1",
        handlers={"handoff": lambda message: "filed"},
    )

    assert result["ack"] is None
    assert result["reply"] is None
    assert store.enqueued == []
    assert store.completed[0]["status"] == "completed"


def test_handoff_acks_and_finalizes_without_work_started_noise():
    store = FakeA2AStore(_message("handoff"))

    result = process_actionable_once(
        store=store,
        target="axon",
        consumer="worker-1",
        handlers={"handoff": lambda message: "filed"},
    )

    assert result["ack"]["message_type"] == "ack"
    assert result["started"] is None
    assert result["reply"]["message_type"] == "final"
    assert [packet["message_type"] for packet in store.enqueued] == ["ack", "final"]
    assert store.completed[0]["status"] == "completed"


def test_question_packets_are_actionable_without_work_started_noise():
    store = FakeA2AStore(_message("question"))

    result = process_actionable_once(
        store=store,
        target="pons",
        consumer="worker-1",
        handlers={"question": lambda message: {"message_type": "answer", "body": "yes"}},
    )

    assert result["ack"]["message_type"] == "ack"
    assert result["started"] is None
    assert result["reply"]["message_type"] == "answer"
    assert [packet["message_type"] for packet in store.enqueued] == ["ack", "answer"]
    assert store.completed == [{"message_id": "msg-1", "status": "completed", "result": "yes", "evidence_links": []}]


def test_answer_packets_are_actionable_to_resume_clarification_threads():
    store = FakeA2AStore(_message("answer"))

    result = process_actionable_once(
        store=store,
        target="axon",
        consumer="worker-1",
        handlers={"answer": lambda message: A2AActionResult(message_type="final", body="thread closed")},
    )

    assert result["ack"]["message_type"] == "ack"
    assert result["started"] is None
    assert result["reply"]["message_type"] == "final"
    assert [packet["message_type"] for packet in store.enqueued] == ["ack", "final"]
    assert store.completed == [
        {"message_id": "msg-1", "status": "completed", "result": "thread closed", "evidence_links": []}
    ]


def test_handler_status_final_is_normalized_to_completed_terminal_status():
    store = FakeA2AStore(_message("work_request"))

    result = process_actionable_once(
        store=store,
        target="axon",
        consumer="worker-1",
        handlers={"work_request": lambda message: {"message_type": "final", "status": "final", "body": "done"}},
    )

    assert result["reply"]["message_type"] == "final"
    assert store.completed == [{"message_id": "msg-1", "status": "completed", "result": "done", "evidence_links": []}]



def test_missing_capability_returns_needs_human_without_running_handler():
    ran = False
    store = FakeA2AStore(_message("work_request", payload={"required_capabilities": ["xai"]}))

    def handler(message):
        nonlocal ran
        ran = True
        return "should not run"

    result = process_actionable_once(
        store=store,
        target="axon",
        consumer="worker-1",
        handlers={"work_request": handler},
        capabilities=["openai"],
    )

    assert result is not None
    assert ran is False
    assert result["ack"]["message_type"] == "ack"
    assert result["reply"]["message_type"] == "needs_human"
    assert store.completed[0]["status"] == "needs_human"
    assert "missing capability xai" in store.completed[0]["result"]


def test_actionable_receiver_returns_payload_safe_receipts_and_calls_sink():
    journal = []
    store = FakeA2AStore(
        _message(
            "work_request",
            subject="Provider smoke",
            body="private body must not leak",
            payload={"private": "payload must not leak"},
        )
    )

    result = process_actionable_once(
        store=store,
        target="axon",
        consumer="worker-1",
        handlers={"work_request": lambda message: A2AActionResult(message_type="final", body="done")},
        receipt_journal=journal.append,
    )

    assert result["receipts"] == journal
    assert ["claimed", "ack", "work_started", "final", "closed"] == [
        line.split(" | ")[2].split()[0] for line in journal
    ]
    joined = "\n".join(journal)
    assert "private body" not in joined
    assert "payload" not in joined


def test_lifecycle_ignore_journals_claim_and_fyi_without_reply_loop():
    journal = []
    store = FakeA2AStore(_message("ack"))

    result = process_actionable_once(
        store=store,
        target="axon",
        consumer="worker-1",
        handlers={},
        receipt_journal=journal.append,
    )

    assert result["ignored"] is True
    assert store.enqueued == []
    assert ["claimed", "fyi_logged"] == [line.split(" | ")[2].split()[0] for line in journal]


def _auth_policy():
    return A2AAuthPolicy(
        trusted_keys={"pons": {"pons-main": "test-secret"}},
        grants={"pons": {"axon": {"status", "read_only", "coordination"}}},
        now=lambda: 1_780_000_010,
        replay_store=MemoryReplayStore(),
    )


def _signed_message(action_class="status", nonce="n1"):
    message = _message("work_request", payload={"metadata": {"action_class": action_class}, "body": "hello"})
    packet = {
        "message_id": message["message_id"],
        "sender": message["sender"],
        "target": message["target"],
        "topic_id": message["topic_id"],
        "message_type": message["message_type"],
        "payload": message["payload"],
        "metadata": {"action_class": action_class},
    }
    sign_packet(packet, key_id="pons-main", secret="test-secret", timestamp=1_780_000_000, nonce=nonce)
    message["payload"]["metadata"]["auth"] = packet["metadata"]["auth"]
    return message


def test_auth_policy_missing_signature_rejects_before_ack_or_handler():
    ran = False
    store = FakeA2AStore(_message("work_request", payload={"metadata": {"action_class": "status"}, "body": "hello"}))

    def handler(message):
        nonlocal ran
        ran = True
        return "should not run"

    result = process_actionable_once(
        store=store,
        target="axon",
        consumer="worker-1",
        handlers={"work_request": handler},
        auth_policy=_auth_policy(),
    )

    assert result is not None
    assert ran is False
    assert result["ack"] is None
    assert result["reply"] is None
    assert store.enqueued == []
    assert store.completed == [{"message_id": "msg-1", "status": "failed", "result": "A2A auth rejected: missing_signature"}]


def test_auth_policy_valid_signature_allows_handler():
    store = FakeA2AStore(_signed_message("status"))

    result = process_actionable_once(
        store=store,
        target="axon",
        consumer="worker-1",
        handlers={"work_request": lambda message: A2AActionResult(message_type="final", body="done")},
        auth_policy=_auth_policy(),
    )

    assert result["ack"]["message_type"] == "ack"
    assert result["started"]["message_type"] == "work_started"
    assert result["reply"]["message_type"] == "final"
    assert store.completed[-1] == {"message_id": "msg-1", "status": "completed", "result": "done", "evidence_links": []}


def test_auth_policy_signed_but_unauthorized_action_needs_human_before_handler():
    ran = False
    store = FakeA2AStore(_signed_message("service_mutation"))

    def handler(message):
        nonlocal ran
        ran = True
        return "should not run"

    result = process_actionable_once(
        store=store,
        target="axon",
        consumer="worker-1",
        handlers={"work_request": handler},
        auth_policy=_auth_policy(),
    )

    assert result is not None
    assert ran is False
    assert result["ack"] is None
    assert result["reply"] is None
    assert store.enqueued == []
    assert store.completed == [
        {
            "message_id": "msg-1",
            "status": "needs_human",
            "result": "A2A policy requires human: action_class_not_authorized:service_mutation",
        }
    ]
