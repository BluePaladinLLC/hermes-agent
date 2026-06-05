#!/usr/bin/env python3
"""Validate and summarize an exported Pons A2A recovery bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import tempfile
from pathlib import Path


def _safe_extract(tf: tarfile.TarFile, root: Path) -> None:
    """Extract tar members into root after rejecting unsafe paths/links."""

    root = root.resolve()
    for member in tf.getmembers():
        member_name = member.name
        target = (root / member_name).resolve()
        if not str(target).startswith(str(root) + "/"):
            raise SystemExit(f"unsafe archive member path: {member_name}")
        if member.issym() or member.islnk():
            link_target = Path(member.linkname)
            if link_target.is_absolute():
                raise SystemExit(f"unsafe archive link target: {member_name}")
            resolved_link = (target.parent / link_target).resolve()
            if not str(resolved_link).startswith(str(root) + "/"):
                raise SystemExit(f"unsafe archive link target: {member_name}")
        if member.isdev():
            raise SystemExit(f"unsafe archive device entry: {member_name}")
    tf.extractall(root)

REQUIRED_RUNTIME_FILES = [
    "gateway/a2a_valkey.py",
    "gateway/a2a_envelope.py",
    "gateway/a2a_actionable_receiver.py",
    "gateway/a2a_receiver_tick.py",
    "gateway/a2a_consult.py",
    "tools/a2a_consult_tool.py",
]
REQUIRED_EVIDENCE = [
    "a2a_v2_final_checkpoint.md",
    "a2a_v2_assessment_and_peer_rollout.md",
    "a2a_bundle_a_acceptance_summary.md",
    "a2a_team_debrief_summary.md",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("archive", type=Path)
    ap.add_argument("--out", type=Path, default=Path("/tmp/a2a-pons-bundle-summary.json"))
    args = ap.parse_args()

    archive = args.archive.resolve()
    if not archive.exists():
        raise SystemExit(f"archive not found: {archive}")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        with tarfile.open(archive, "r:gz") as tf:
            _safe_extract(tf, root)
        dirs = [p for p in root.iterdir() if p.is_dir()]
        if len(dirs) != 1:
            raise SystemExit(f"expected one top-level directory, found {len(dirs)}")
        bundle = dirs[0]

        runtime_missing = [f for f in REQUIRED_RUNTIME_FILES if not (bundle / "runtime" / f).exists()]
        evidence_missing = [f for f in REQUIRED_EVIDENCE if not (bundle / "evidence" / "kanban_t_9d0028e4" / f).exists()]
        test_outputs = bundle / "meta" / "a2a-test-output.txt"
        test_text = test_outputs.read_text(errors="replace") if test_outputs.exists() else ""

        files = [p for p in bundle.rglob("*") if p.is_file()]
        summary = {
            "archive": str(archive),
            "archive_sha256": sha256(archive),
            "top_level": bundle.name,
            "file_count": len(files),
            "runtime_missing": runtime_missing,
            "evidence_missing": evidence_missing,
            "has_test_output": test_outputs.exists(),
            "test_output_tail": "\n".join(test_text.splitlines()[-20:]),
        }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if not summary["runtime_missing"] and not summary["evidence_missing"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
