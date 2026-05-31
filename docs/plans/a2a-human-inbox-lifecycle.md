# A2A Human-Inbox Lifecycle Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make A2A behave like a normal human inbox: sender sends, receiver is alerted, receiver reads/claims, acknowledges, dispositions, and closes or follows up without send-and-forget gaps or lifecycle reply loops.

**Architecture:** A2A remains the actionable work bus. Discord `#agent-comms` is only the compact receipt/audit surface. Receivers must separate actionable packets from lifecycle packets and emit state transitions without treating every state update as new work.

**Tech Stack:** Hermes gateway/API-server receiver code, Valkey/A2A streams, SQLite/local inbox where present, cron receipt watcher, pytest.

---

## Lifecycle contract

Canonical happy path:

```text
sent → delivered → alerted → claimed/read → ack → disposition → final/closed
```

Allowed dispositions:

```text
reply
question / needs_clarification
queued
work_started
fyi_logged
needs_human
final
failed
timeout
```

Packet families:

```text
Actionable: work_request, handoff, consult
Lifecycle/status: ack, nack, read, queued, work_started, needs_clarification,
                  needs_human, work_result, final, failed, timeout, fyi_logged
```

Invariant: lifecycle/status packets update state only. They must never create a new reply loop.

## Phase 1 — Receiver loop safety

1. Add a focused receiver primitive that claims one inbox item and only runs handlers for actionable packet types.
2. Complete lifecycle/status packets as ignored/completed without ACKing or replying.
3. ACK actionable packets exactly once using idempotency keys.
4. Emit `work_started` only when the disposition is active work, not for every handoff.
5. Add unit tests for ACK/work_started/work_result/final loop prevention.

## Phase 2 — Envelope/schema convergence

1. Normalize one envelope/topic path around these message types:
   `consult`, `handoff`, `work_request`, `question`, `answer`, `ack`, `nack`,
   `work_started`, `work_result`, `needs_human`, `final`.
2. Require `message_id`, `topic_id`, `sender`, `target`, `message_type`, `subject`,
   `payload`, `created_at`, and idempotency/correlation fields.
3. Add compatibility shims for older `kind`/`topic`/`summary` names only at boundaries.

## Phase 3 — Bruno-visible receipts

1. Keep A2A private payloads off Discord by default.
2. Emit only compact non-secret lifecycle receipts to `#agent-comms`:

   ```text
   Pons → Axon | delivered | msg-123
   Axon | claimed/read | msg-123
   Axon → Pons | needs_clarification | msg-456
   Pons → Axon | answer | msg-789
   Axon | final | msg-123
   ```

3. Verify existing receipt watcher before adding new channel permissions or routing.
4. Receipt failure must not block A2A finalization; it should be visible as receipt-pipeline degraded.

## Phase 4 — Fleet rollout gates

1. Axon/Pons canary: transport, wake/claim, ACK, final, and receipt status reported separately.
2. Clarification loop canary: receiver asks a question, sender wakes, sender replies, receiver finalizes.
3. Pending/deadletter hygiene: no stale pending, no lifecycle packet churn.
4. Roll out to VAGUS, Synapse, and Thalamus only after canaries pass.

## Safety boundaries

- A2A handoffs do not authorize repo writes, service restarts, production wiring, or destructive actions.
- Receivers may read, ACK, ask clarification, queue, or deny based on missing context/tools/permission.
- Durable tracked work belongs in GitHub/Kanban after explicit scope exists.
