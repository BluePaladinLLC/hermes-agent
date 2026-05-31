from __future__ import annotations

from gateway.a2a_actionable_receiver import A2AActionResult
from gateway.a2a_receiver_tick import run_receiver_tick


class FakeA2AStore:
    def __init__(self, messages):
        self.messages = list(messages)
        self.enqueued = []
        self.completed = []

    def claim(self, *, target, consumer, block_ms):
        if not self.messages:
            return None
        return self.messages.pop(0)

    def enqueue(self, **kwargs):
        packet = {"message_id": f"reply-{len(self.enqueued) + 1}", **kwargs}
        self.enqueued.append(packet)
        return packet

    def complete(self, message_id, **kwargs):
        record = {"message_id": message_id, **kwargs}
        self.completed.append(record)
        return record


def _message(message_id="msg-1", message_type="work_request", **overrides):
    message = {
        "message_id": message_id,
        "sender": "pons",
        "target": "axon",
        "message_type": message_type,
        "topic_id": "topic-1",
        "subject": "Provider smoke",
        "payload": {},
    }
    message.update(overrides)
    return message


def test_receiver_tick_processes_bounded_messages_and_journals_receipts():
    journal = []
    store = FakeA2AStore([_message("msg-1"), _message("msg-2")])

    summary = run_receiver_tick(
        store=store,
        target="axon",
        consumer="worker-1",
        handlers={"work_request": lambda message: A2AActionResult(message_type="final", body="done")},
        receipt_journal=journal.append,
        max_messages=1,
    )

    assert summary["processed"] == 1
    assert summary["stopped_reason"] == "max_messages"
    assert len(summary["results"]) == 1
    assert len(store.messages) == 1
    assert ["claimed", "ack", "work_started", "final", "closed"] == [
        line.split(" | ")[2].split()[0] for line in journal
    ]


def test_receiver_tick_stops_after_idle_ticks_without_journaling_noise():
    journal = []
    store = FakeA2AStore([])

    summary = run_receiver_tick(
        store=store,
        target="axon",
        consumer="worker-1",
        handlers={},
        receipt_journal=journal.append,
        max_idle_ticks=2,
    )

    assert summary == {"processed": 0, "idle_ticks": 2, "stopped_reason": "idle", "results": []}
    assert journal == []


def test_receiver_tick_lifecycle_packets_remain_non_actionable_but_receipted():
    journal = []
    store = FakeA2AStore([_message("msg-1", "ack")])

    summary = run_receiver_tick(
        store=store,
        target="axon",
        consumer="worker-1",
        handlers={},
        receipt_journal=journal.append,
    )

    assert summary["processed"] == 1
    assert summary["results"][0]["ignored"] is True
    assert store.enqueued == []
    assert ["claimed", "fyi_logged"] == [line.split(" | ")[2].split()[0] for line in journal]
