from __future__ import annotations

from gateway.a2a_receipt_journal import A2AReceiptJournal


def test_a2a_receipt_journal_appends_sanitized_lines(tmp_path):
    path = tmp_path / "receipts" / "a2a.log"
    journal = A2AReceiptJournal(path)

    journal.append("✅ A2A | axon → pons | ack\nextra should be same line")
    journal.extend(["", "🏁 A2A | axon → pons | final"])

    assert journal.read_lines() == [
        "✅ A2A | axon → pons | ack extra should be same line",
        "🏁 A2A | axon → pons | final",
    ]
    assert path.read_text(encoding="utf-8").count("\n") == 2
