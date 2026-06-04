#!/usr/bin/env bash
set -euo pipefail

# Run this on Pons. It creates a redacted source/evidence bundle for A2A recovery.
# It does not restart services, mutate streams, edit config, or print secrets.

STAMP="${A2A_EXPORT_STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT_ROOT="${A2A_EXPORT_OUT:-/tmp/a2a-pons-runtime-export-$STAMP}"
ARCHIVE="${A2A_EXPORT_ARCHIVE:-/tmp/a2a-pons-runtime-export-$STAMP.tar.gz}"

RUNTIME_ROOT="/usr/local/lib/hermes-agent"
KANBAN_EVIDENCE="/root/.hermes/kanban/boards/hermes-migration-master/evidence/t_9d0028e4"
CORPUS_EVIDENCE="/root/cortexos-hermes-profiles/shared/a2a-evidence"

mkdir -p "$OUT_ROOT"/{runtime,gateway,tools,tests_gateway,tests_tools,evidence/kanban_t_9d0028e4,evidence/cortexos_profiles,meta}

copy_if_exists() {
  local src="$1"
  local dst="$2"
  if [ -e "$src" ]; then
    mkdir -p "$(dirname "$dst")"
    cp -a "$src" "$dst"
    printf 'copied\t%s\t%s\n' "$src" "$dst" >> "$OUT_ROOT/meta/copy-manifest.tsv"
  else
    printf 'missing\t%s\t%s\n' "$src" "$dst" >> "$OUT_ROOT/meta/copy-manifest.tsv"
  fi
}

# Runtime A2A files named by Pons.
for f in \
  gateway/a2a_valkey.py \
  gateway/a2a_envelope.py \
  gateway/a2a_actionable_receiver.py \
  gateway/a2a_receiver_tick.py \
  gateway/a2a_consult.py \
  gateway/a2a_ack_worker.py \
  gateway/a2a_inbox.py \
  gateway/a2a_receipts.py \
  gateway/a2a_receipt_watcher.py \
  gateway/a2a_receipt_journal.py \
  gateway/a2a_canary.py \
  tools/a2a_consult_tool.py
  do
    copy_if_exists "$RUNTIME_ROOT/$f" "$OUT_ROOT/runtime/$f"
  done

# Runtime tests.
if [ -d "$RUNTIME_ROOT/tests/gateway" ]; then
  find "$RUNTIME_ROOT/tests/gateway" -maxdepth 1 -type f -name 'test_a2a_*.py' -print0 \
    | while IFS= read -r -d '' f; do copy_if_exists "$f" "$OUT_ROOT/runtime/tests/gateway/$(basename "$f")"; done
fi
copy_if_exists "$RUNTIME_ROOT/tests/tools/test_a2a_consult_tool.py" "$OUT_ROOT/runtime/tests/tools/test_a2a_consult_tool.py"

# Evidence docs.
for f in \
  a2a_v2_final_checkpoint.md \
  a2a_v2_assessment_and_peer_rollout.md \
  a2a_bundle_a_acceptance_summary.md \
  a2a_team_debrief_summary.md
  do
    copy_if_exists "$KANBAN_EVIDENCE/$f" "$OUT_ROOT/evidence/kanban_t_9d0028e4/$f"
  done

if [ -d "$CORPUS_EVIDENCE" ]; then
  cp -a "$CORPUS_EVIDENCE/." "$OUT_ROOT/evidence/cortexos_profiles/"
  printf 'copied_dir\t%s\t%s\n' "$CORPUS_EVIDENCE" "$OUT_ROOT/evidence/cortexos_profiles" >> "$OUT_ROOT/meta/copy-manifest.tsv"
else
  printf 'missing_dir\t%s\t%s\n' "$CORPUS_EVIDENCE" "$OUT_ROOT/evidence/cortexos_profiles" >> "$OUT_ROOT/meta/copy-manifest.tsv"
fi

# Metadata: no secret values.
{
  printf 'timestamp_utc=%s\n' "$STAMP"
  printf 'hostname=%s\n' "$(hostname)"
  printf 'whoami=%s\n' "$(whoami)"
  printf 'runtime_root=%s\n' "$RUNTIME_ROOT"
  printf 'kanban_evidence=%s\n' "$KANBAN_EVIDENCE"
  printf 'corpus_evidence=%s\n' "$CORPUS_EVIDENCE"
  if [ -d "$RUNTIME_ROOT/.git" ]; then
    git -C "$RUNTIME_ROOT" remote -v 2>/dev/null | sed -E 's#(https://)[^/@]+:[^/@]+@#\1[REDACTED]@#' || true
    git -C "$RUNTIME_ROOT" status --short 2>/dev/null || true
    git -C "$RUNTIME_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || true
    git -C "$RUNTIME_ROOT" rev-parse HEAD 2>/dev/null || true
  else
    printf 'runtime_git=absent\n'
  fi
} > "$OUT_ROOT/meta/source-metadata.txt"

# Optional tests: record result only, don't fail export if test deps are missing/failing.
if [ -x "$RUNTIME_ROOT/venv/bin/python" ]; then
  PY="$RUNTIME_ROOT/venv/bin/python"
elif [ -x "$RUNTIME_ROOT/.venv/bin/python" ]; then
  PY="$RUNTIME_ROOT/.venv/bin/python"
else
  PY="$(command -v python3 || true)"
fi

if [ -n "$PY" ]; then
  (
    cd "$RUNTIME_ROOT"
    "$PY" -m pytest tests/gateway/test_a2a_*.py tests/tools/test_a2a_consult_tool.py -q
  ) > "$OUT_ROOT/meta/a2a-test-output.txt" 2>&1 || true
else
  printf 'python_not_found\n' > "$OUT_ROOT/meta/a2a-test-output.txt"
fi

(
  cd "$OUT_ROOT"
  find . -type f -print0 | sort -z | xargs -0 sha256sum
) > "$OUT_ROOT/meta/sha256sums.txt"

tar -C "$(dirname "$OUT_ROOT")" -czf "$ARCHIVE" "$(basename "$OUT_ROOT")"
sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"

printf 'A2A_PONS_EXPORT_ARCHIVE=%s\n' "$ARCHIVE"
printf 'A2A_PONS_EXPORT_SHA256=%s\n' "$(cut -d' ' -f1 "$ARCHIVE.sha256")"
printf 'A2A_PONS_EXPORT_MANIFEST=%s\n' "$OUT_ROOT/meta/copy-manifest.tsv"
