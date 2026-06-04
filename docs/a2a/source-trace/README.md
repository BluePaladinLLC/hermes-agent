# Pons A2A source trace intake

This directory is reserved for the exported Pons A2A runtime/evidence bundle.

## Expected intake artifact

Archive produced on Pons by:

```bash
scripts/a2a/export_pons_a2a_bundle.sh
```

Expected archive shape:

```text
a2a-pons-runtime-export-<timestamp>/
  runtime/
    gateway/a2a_valkey.py
    gateway/a2a_envelope.py
    gateway/a2a_actionable_receiver.py
    gateway/a2a_receiver_tick.py
    gateway/a2a_consult.py
    tools/a2a_consult_tool.py
    tests/gateway/test_a2a_*.py
    tests/tools/test_a2a_consult_tool.py
  evidence/
    kanban_t_9d0028e4/
      a2a_v2_final_checkpoint.md
      a2a_v2_assessment_and_peer_rollout.md
      a2a_bundle_a_acceptance_summary.md
      a2a_team_debrief_summary.md
    cortexos_profiles/
      ...
  meta/
    copy-manifest.tsv
    source-metadata.txt
    a2a-test-output.txt
    sha256sums.txt
```

## Intake rule

Do not copy runtime files directly into production code paths until:

1. archive SHA is recorded;
2. bundle validates with `scripts/a2a/validate_pons_a2a_bundle.py`;
3. tests are reproduced locally;
4. source differences are reviewed against current GitHub A2A files and Synapse local hardening;
5. missing API route glue is fixed;
6. runtime rollout plan is approved separately.

## Why source trace first

Pons confirmed the surviving source is deployed/untracked runtime state, not a clean branch. This directory preserves provenance before code promotion.
