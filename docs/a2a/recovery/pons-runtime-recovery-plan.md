# A2A recovery from Pons runtime state

## Status

This branch is a recovery/migration staging branch for Cortex A2A. It is **not** a runtime rollout branch yet.

Pons confirmed the original A2A Git/Forgejo branch is not the canonical pickup point anymore. The branch/worktree appears gone. The surviving source of truth is the deployed/untracked Pons runtime copy plus Kanban evidence.

## Canonical pickup sources from Pons

### Runtime source

Pons runtime copy:

```text
/usr/local/lib/hermes-agent/
```

Key files named by Pons:

```text
gateway/a2a_valkey.py
gateway/a2a_envelope.py
gateway/a2a_actionable_receiver.py
gateway/a2a_receiver_tick.py
gateway/a2a_consult.py
tools/a2a_consult_tool.py
tests/gateway/test_a2a_*
tests/tools/test_a2a_consult_tool.py
```

### Kanban/evidence source

Original Kanban item:

```text
t_9d0028e4 — A2A v1: Unified Valkey Streams topic inbox MVP
```

Original worktree as logged by Pons, now missing:

```text
/root/.hermes/kanban/boards/hermes-migration-master/workspaces/t_9d0028e4/hermes-agent-a2a-valkey
branch: cortex/a2a-valkey-topic-inbox-mvp
```

Evidence directory:

```text
/root/.hermes/kanban/boards/hermes-migration-master/evidence/t_9d0028e4/
```

Best starting docs:

```text
a2a_v2_final_checkpoint.md
a2a_v2_assessment_and_peer_rollout.md
a2a_bundle_a_acceptance_summary.md
a2a_team_debrief_summary.md
```

### Legacy/reference source

Forgejo-related reference material:

```text
/root/cortexos-hermes-profiles/shared/a2a-evidence/
```

Branches named by Pons:

```text
pons/a2a-inbox-thread-enforcement
docs/a2a-state-handoff-contract
docs/a2a-state-transfer-boundaries
```

Treat Forgejo/corpus material as reference/provenance only unless explicitly promoted.

## Important caveat

Pons reported current `/usr/local/lib/hermes-agent` A2A tests are **not fully green**:

```text
48/56 passed
```

Known failing area:

```text
APIServerAdapter._handle_a2a_consult route glue missing
```

Therefore this is recovery/migration state, not PR-ready product state.

## Synapse access note

At recovery start, Synapse could not SSH into Pons:

```text
root@10.1.1.120: Permission denied (publickey,password)
synapse@10.1.1.120: Permission denied (publickey,password)
```

The Pons paths are not visible from Synapse's local filesystem. The next required artifact is an exported Pons bundle generated on Pons or via restored SSH credentials.

## Intended end-state preserved

- **Valkey** is the machine coordination bus: agent inbox streams, topic events, claim/reclaim, retry/dead-letter signals.
- **Durable ledger** is the audit/source-of-truth record of lifecycle outcomes and terminal state.
- **Discord `#agent-comms`** is only a compact human-visible mirror, never the A2A bus.
- **`/v1/runs`** is execution after claim, not a blind side-channel coordination rail.

## Recovery plan

1. Export Pons runtime/evidence bundle with `scripts/a2a/export_pons_a2a_bundle.sh` from Pons.
2. Import bundle into this branch as `docs/a2a/source-trace/pons-runtime-<timestamp>/`.
3. Compare Pons runtime files against current GitHub files and Synapse local hardening.
4. Reproduce Pons-reported `48/56` state.
5. Fix missing `APIServerAdapter._handle_a2a_consult` glue.
6. Add/verify tests for:
   - lifecycle suppression;
   - A2A consult tool path;
   - API route glue;
   - Valkey topic/inbox behavior;
   - receipt/ledger semantics.
7. Only after tests are green, prepare implementation PR.
8. Runtime rollout remains a separate approval: no restarts, stream cleanup, firewall/listener changes, or live A2A config mutation from this branch.
