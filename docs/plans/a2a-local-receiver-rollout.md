# A2A Local Receiver Rollout Runbook

Status: local/non-production lane. This runbook documents the current A2A receiver lifecycle work in `BluePaladinLLC/hermes-agent` and the gates before live fleet enablement.

## Current contract

A2A is the actionable work bus. Human-visible channels such as `#agent-comms` are receipt/audit surfaces only.

Happy path:

```text
send → claim/read → ack → disposition → final/closed
```

Supported work-bearing packet types:

```text
work_request, handoff, consult, question, answer, reply_required
```

Lifecycle/status packets are state updates only and must not wake a reply loop:

```text
ack, nack, read, queued, work_started, needs_clarification,
needs_human, work_result, final, failed, timeout, fyi_logged
```

## Local lane components

- `gateway/a2a_receipts.py` formats compact payload-safe receipt lines.
- `gateway/a2a_actionable_receiver.py` claims one packet, ACKs actionable work, runs a handler, and closes the original packet.
- `gateway/a2a_receiver_tick.py` provides a bounded tick wrapper around the receiver primitive.
- `gateway/a2a_canary.py` provides local in-process canaries for Axon/Pons and the fleet role matrix.
- `gateway/a2a_receipt_journal.py` appends compact receipt lines to a local file sink.

## Safety boundaries

The current lane intentionally does **not** do any of the following:

- Discord or `#agent-comms` posting.
- Gateway or service restarts.
- Daemon ownership or scheduled live receiver enablement.
- Kanban mutation.
- Production wiring.
- Network calls from canaries.
- Destructive changes.
- Secret or payload body exposure in receipts.

Receipt sink failure must not block A2A completion once a live sink is introduced.

## QA gates before live enablement

Before any live receiver process, cron, daemon, or `#agent-comms` posting is enabled:

1. Unit tests pass for receipts, actionable receiver, receiver tick, receipt journal, and canaries.
2. Axon/Pons final canary passes: `work_request → ack/work_started/final`, then lifecycle packets are consumed without reply loops.
3. Axon/Pons clarification canary passes: `question → answer → final`.
4. Role matrix canaries pass for Axon, Pons, VAGUS, Synapse, and Thalamus.
5. Receipt output is payload-safe and contains no body/payload text.
6. Pending queue is empty after canaries.
7. Live enablement scope is explicitly approved by Bruno.

## Manual local verification

From the repo root:

```bash
python -m py_compile \
  gateway/a2a_receipts.py \
  gateway/a2a_actionable_receiver.py \
  gateway/a2a_receiver_tick.py \
  gateway/a2a_canary.py \
  gateway/a2a_receipt_journal.py

PYTHONPATH=$(pwd) py -m pytest \
  tests/gateway/test_a2a_receipts.py \
  tests/gateway/test_a2a_actionable_receiver.py \
  tests/gateway/test_a2a_receiver_tick.py \
  tests/gateway/test_a2a_canary.py \
  tests/gateway/test_a2a_receipt_journal.py \
  -q -o 'addopts='
```

## Enablement sequence, when approved

1. Start with local file receipt journaling only.
2. Run one receiver tick manually for a single target profile.
3. Inspect receipts and pending/complete state.
4. Run Axon ↔ Pons live canary.
5. Extend to VAGUS, Synapse, and Thalamus only after Axon/Pons passes.
6. Add `#agent-comms` posting only behind an explicit switch.
7. Treat posting failure as degraded observability, not A2A task failure.

## Kill switches

Until live wiring exists, the kill switch is simply not invoking the receiver tick or journal sink.

For future live wiring, provide separate switches for:

- receiver tick execution;
- receipt sink selection;
- Discord/`#agent-comms` posting.

Disabling Discord receipts must not disable A2A packet completion.
