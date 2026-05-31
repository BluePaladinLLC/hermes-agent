from __future__ import annotations

from gateway.a2a_receipts import format_a2a_receipt, format_a2a_receipts


def test_format_a2a_receipt_keeps_lifecycle_compact():
    text = format_a2a_receipt(
        {
            "event_type": "claimed",
            "sender": "pons",
            "target": "axon",
            "message_id": "a2a_msg_1234567890abcdef",
            "topic_id": "a2a-question-loop/PONS-AXON-1",
            "subject": "Provider smoke check",
            "body": "private body must not appear",
            "payload": {"private": "payload must not appear"},
        }
    )

    assert text == "👀 A2A | pons → axon | claimed | msg `a2a_msg_1234567890ab` | topic `a2a-question-loop/PO` | `Provider smoke check`"
    assert "private body" not in text
    assert "payload" not in text


def test_format_a2a_receipt_redacts_secret_like_subjects():
    text = format_a2a_receipt(
        {
            "event_type": "final",
            "sender": "axon",
            "target": "pons",
            "message_id": "msg-1",
            "subject": "API key rotation result",
        }
    )

    assert "[redacted-sensitive-subject]" in text
    assert "API key" not in text


def test_format_a2a_receipt_sanitizes_unknown_event_names():
    text = format_a2a_receipt(
        {
            "event_type": "custom payload dump",
            "sender": "agent name with spaces",
            "receiver": "target!",
            "id": "abc123",
            "topic": "A normal topic",
        }
    )

    assert text.startswith("💬 A2A | agentnamewithspaces → target | reply")
    assert "custom payload dump" not in text


def test_format_a2a_receipts_formats_multiple_events():
    rows = format_a2a_receipts(
        [
            {"event_type": "ack", "sender": "axon", "target": "pons", "subject": "Got it"},
            {"event_type": "final", "sender": "axon", "target": "pons", "subject": "Done"},
        ]
    )

    assert len(rows) == 2
    assert rows[0].startswith("✅ A2A")
    assert rows[1].startswith("🏁 A2A")


def test_format_a2a_receipt_uses_first_targets_entry_from_enqueued_packets():
    text = format_a2a_receipt(
        {
            "event_type": "ack",
            "sender": "axon",
            "targets": ["pons"],
            "subject": "ACK: Provider smoke",
        }
    )

    assert text.startswith("✅ A2A | axon → pons | ack")
