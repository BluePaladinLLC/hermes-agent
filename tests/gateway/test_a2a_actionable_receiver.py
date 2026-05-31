from __future__ import annotations

from gateway.a2a_actionable_receiver import A2AActionResult, process_actionable_once


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

    assert ran is False
    assert result["ack"]["message_type"] == "ack"
    assert result["reply"]["message_type"] == "needs_human"
    assert store.completed[0]["status"] == "needs_human"
    assert "missing capability xai" in store.completed[0]["result"]
