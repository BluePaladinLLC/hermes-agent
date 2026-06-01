from __future__ import annotations

from gateway.a2a_canary import run_axon_pons_clarification_canary, run_axon_pons_final_canary


def _events(summary):
    return [line.split(" | ")[2].split()[0] for line in summary["receipts"]]


def test_axon_pons_final_canary_completes_without_receipt_leak_or_churn():
    summary = run_axon_pons_final_canary()

    assert summary["ok"] is True
    assert summary["pending"] == 0
    assert summary["tick_processed"] == [1, 3]
    assert _events(summary) == [
        "claimed",
        "ack",
        "work_started",
        "final",
        "closed",
        "claimed",
        "fyi_logged",
        "claimed",
        "fyi_logged",
        "claimed",
        "fyi_logged",
    ]
    assert "private canary body" not in "\n".join(summary["receipts"])


def test_axon_pons_clarification_canary_round_trips_question_answer_final():
    summary = run_axon_pons_clarification_canary()

    assert summary["ok"] is True
    assert summary["pending"] == 0
    assert summary["tick_processed"] == [1, 3, 2, 2]
    events = _events(summary)
    assert events.count("question") == 1
    assert events.count("answer") == 1
    assert events.count("final") == 1
    assert events.count("work_started") == 1
    assert "private canary body" not in "\n".join(summary["receipts"])
