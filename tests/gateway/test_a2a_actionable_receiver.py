from __future__ import annotations

from typing import Any, Mapping

import pytest

from gateway.a2a_actionable_receiver import A2AActionResult, process_actionable_once


class FakeA2AStore:
    def __init__(self, message: Mapping[str, Any] | None) -> None:
        self.message = dict(message) if message else None
        self.enqueued: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []

    def claim(self, *, target: str, consumer: str, block_ms: int = 1) -> dict[str, Any] | None:
        message = self.message
        self.message = None
        return message

    def enqueue(self, **kwargs: Any) -> dict[str, Any]:
        self.enqueued.append(dict(kwargs))
        return {"message_id": f"reply-{len(self.enqueued)}", **kwargs}

    def complete(
        self,
        message_id: str,
        *,
        status: str = "completed",
        result: str = "",
        evidence_links: list[str] | None = None,
    ) -> dict[str, Any]:
        completed = {
            "message_id": message_id,
            "status": status,
            "result": result,
            "evidence_links": list(evidence_links or []),
        }
        self.completed.append(completed)
        return completed


def _message(message_type: str, *, subject: str = "A2A packet") -> dict[str, Any]:
    return {
        "message_id": "msg-1",
        "sender": "thalamus",
        "topic_id": "topic-1",
        "subject": subject,
        "message_type": message_type,
    }


@pytest.mark.parametrize("message_type", ["ack", "nack", "work_started", "work_result", "needs_human", "final", "answer"])
def test_lifecycle_messages_are_observed_without_reply_storms(message_type: str) -> None:
    store = FakeA2AStore(_message(message_type))

    result = process_actionable_once(store=store, target="synapse", consumer="worker", handlers={})

    assert result is not None
    assert store.enqueued == []
    assert store.completed == [
        {
            "message_id": "msg-1",
            "status": "completed",
            "result": f"Observed non-actionable A2A message_type={message_type}; no reply sent.",
            "evidence_links": [],
        }
    ]
    assert result["ack"] is None
    assert result["started"] is None
    assert result["reply"] is None


def test_final_no_reply_work_request_completes_without_ack_started_or_reply() -> None:
    store = FakeA2AStore(_message("work_request", subject="FINAL-NO-REPLY: repo destinations"))

    result = process_actionable_once(
        store=store,
        target="synapse",
        consumer="worker",
        handlers={"work_request": lambda message: A2AActionResult(body="accepted")},
    )

    assert result is not None
    assert store.enqueued == []
    assert store.completed == [
        {
            "message_id": "msg-1",
            "status": "completed",
            "result": "accepted",
            "evidence_links": [],
        }
    ]
    assert result["ack"] is None
    assert result["started"] is None
    assert result["reply"] is None


def test_final_no_reply_without_handler_records_needs_human_without_reply() -> None:
    store = FakeA2AStore(_message("consult", subject="FINAL-NO-REPLY: missing handler"))

    result = process_actionable_once(store=store, target="synapse", consumer="worker", handlers={})

    assert result is not None
    assert store.enqueued == []
    assert store.completed == [
        {
            "message_id": "msg-1",
            "status": "needs_human",
            "result": "No handler registered for message_type=consult",
            "evidence_links": [],
        }
    ]
    assert result["ack"] is None
    assert result["started"] is None
    assert result["reply"] is None


def test_work_request_still_sends_ack_started_and_final_by_default() -> None:
    store = FakeA2AStore(_message("work_request", subject="regular handoff"))

    result = process_actionable_once(
        store=store,
        target="synapse",
        consumer="worker",
        handlers={"work_request": lambda message: A2AActionResult(body="done")},
    )

    assert result is not None
    assert [entry["message_type"] for entry in store.enqueued] == ["ack", "work_started", "final"]
    assert store.completed == [
        {"message_id": "msg-1", "status": "completed", "result": "done", "evidence_links": []}
    ]
