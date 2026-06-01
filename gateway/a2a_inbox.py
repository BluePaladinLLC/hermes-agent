"""Durable lightweight A2A inbox storage.

The inbox is intentionally smaller than Kanban. It stores agent-to-agent
messages that should survive target busyness/offline windows, then lets a
receiver claim/process/complete them later. Kanban remains the durable work
source of truth for multi-step tasks.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from hermes_constants import get_hermes_home
from gateway.a2a_consult import redact_secrets

_SCHEMA_VERSION = 1
_VALID_STATUSES = {"queued", "claimed", "completed", "failed", "expired", "cancelled"}
_TERMINAL_STATUSES = {"completed", "failed", "expired", "cancelled"}
_VALID_KINDS = {
    "consult",
    "handoff",
    "work_request",
    "question",
    "answer",
    "ack",
    "nack",
    "work_started",
    "work_result",
    "needs_human",
    "final",
    # Back-compat alias from the earlier A2A prototype.
    "state_update",
}
_MAX_FIELD_CHARS = 4_000
_MAX_PAYLOAD_CHARS = 32_000


def _now() -> float:
    return time.time()


def _compact(value: Any, *, max_chars: int = _MAX_FIELD_CHARS) -> str:
    text = "" if value is None else str(value).strip()
    if len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def _clean_actor(value: Any, *, default: str = "unknown") -> str:
    text = _compact(value, max_chars=80) or default
    text = re.sub(r"[^A-Za-z0-9_.:@#-]", "-", text)
    return text[:80] or default


def _json_dump(value: Any) -> str:
    text = json.dumps(value or {}, sort_keys=True, ensure_ascii=False)
    if len(text) > _MAX_PAYLOAD_CHARS:
        text = text[: _MAX_PAYLOAD_CHARS - 1].rstrip() + "…"
    return redact_secrets(text)


class A2AInboxError(ValueError):
    """Raised for invalid inbox operations."""


class A2AInboxStore:
    """SQLite-backed A2A inbox for one Hermes profile/home."""

    def __init__(self, path: Optional[Path | str] = None):
        self.path = Path(path) if path else get_hermes_home() / "a2a_inbox.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    target TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    claim_by TEXT,
                    claim_until REAL,
                    run_id TEXT,
                    record_handle TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    due_at REAL NOT NULL,
                    expires_at REAL,
                    completed_at REAL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_target_status_due ON messages(target, status, due_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_status_updated ON messages(status, updated_at)")
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )

    def enqueue(
        self,
        *,
        target: str,
        sender: str,
        kind: str,
        topic: str,
        summary: str,
        payload: Optional[Mapping[str, Any]] = None,
        due_at: Optional[float] = None,
        expires_at: Optional[float] = None,
        max_attempts: int = 3,
        message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        kind = _compact(kind or "handoff", max_chars=40).lower()
        if kind not in _VALID_KINDS:
            raise A2AInboxError(f"kind must be one of {sorted(_VALID_KINDS)}")
        target = _clean_actor(target, default="local")
        sender = _clean_actor(sender, default="unknown")
        topic = _compact(topic, max_chars=200)
        summary = _compact(summary, max_chars=2_000)
        if not topic:
            raise A2AInboxError("topic is required")
        if not summary:
            raise A2AInboxError("summary is required")
        try:
            max_attempts = int(max_attempts)
        except (TypeError, ValueError) as exc:
            raise A2AInboxError("max_attempts must be an integer") from exc
        max_attempts = max(1, min(max_attempts, 25))
        now = _now()
        msg_id = message_id or f"a2a_msg_{uuid.uuid4().hex[:24]}"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages (
                    id, target, sender, kind, topic, summary, payload_json,
                    status, attempts, max_attempts, created_at, updated_at, due_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?, ?)
                """,
                (
                    msg_id,
                    target,
                    sender,
                    kind,
                    topic,
                    summary,
                    _json_dump(payload or {}),
                    max_attempts,
                    now,
                    now,
                    float(due_at if due_at is not None else now),
                    float(expires_at) if expires_at else None,
                ),
            )
        return self.get(msg_id) or {"id": msg_id, "status": "queued"}

    def get(self, message_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def list(
        self,
        *,
        target: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[Dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if target:
            clauses.append("target=?")
            args.append(_clean_actor(target, default="local"))
        if status:
            status = _compact(status, max_chars=40).lower()
            if status not in _VALID_STATUSES:
                raise A2AInboxError(f"status must be one of {sorted(_VALID_STATUSES)}")
            clauses.append("status=?")
            args.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        limit = max(1, min(int(limit or 50), 200))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM messages{where} ORDER BY created_at DESC LIMIT ?",
                (*args, limit),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def claim(
        self,
        *,
        target: str,
        claimed_by: str,
        lease_seconds: float = 300,
    ) -> Optional[Dict[str, Any]]:
        target = _clean_actor(target, default="local")
        claimed_by = _clean_actor(claimed_by, default="unknown")
        now = _now()
        lease_until = now + max(1.0, min(float(lease_seconds or 300), 3600.0))
        with self._connect() as conn:
            self._expire_locked(conn, now)
            row = conn.execute(
                """
                SELECT * FROM messages
                WHERE target=?
                  AND status='queued'
                  AND due_at<=?
                  AND (expires_at IS NULL OR expires_at>?)
                ORDER BY due_at ASC, created_at ASC
                LIMIT 1
                """,
                (target, now, now),
            ).fetchone()
            if not row:
                return None
            attempts = int(row["attempts"] or 0) + 1
            conn.execute(
                """
                UPDATE messages
                SET status='claimed', attempts=?, claim_by=?, claim_until=?, updated_at=?
                WHERE id=? AND status='queued'
                """,
                (attempts, claimed_by, lease_until, now, row["id"]),
            )
        return self.get(str(row["id"]))

    def complete(
        self,
        message_id: str,
        *,
        status: str = "completed",
        run_id: str = "",
        record_handle: str = "",
        error: str = "",
        requeue_delay_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        status = _compact(status, max_chars=40).lower()
        if status not in _VALID_STATUSES:
            raise A2AInboxError(f"status must be one of {sorted(_VALID_STATUSES)}")
        if status == "queued":
            raise A2AInboxError("complete status cannot be queued")
        now = _now()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row:
                raise A2AInboxError("message not found")
            final_status = status
            due_at = float(row["due_at"] or now)
            completed_at = now if status in _TERMINAL_STATUSES else None
            claim_by = None if status in _TERMINAL_STATUSES else row["claim_by"]
            claim_until = None if status in _TERMINAL_STATUSES else row["claim_until"]
            if status == "failed" and requeue_delay_seconds is not None and int(row["attempts"] or 0) < int(row["max_attempts"] or 1):
                final_status = "queued"
                due_at = now + max(0.0, min(float(requeue_delay_seconds), 86_400.0))
                completed_at = None
                claim_by = None
                claim_until = None
            conn.execute(
                """
                UPDATE messages
                SET status=?, run_id=?, record_handle=?, error=?, updated_at=?, due_at=?,
                    completed_at=?, claim_by=?, claim_until=?
                WHERE id=?
                """,
                (
                    final_status,
                    _compact(run_id, max_chars=160),
                    _compact(record_handle, max_chars=500),
                    redact_secrets(_compact(error, max_chars=2_000)),
                    now,
                    due_at,
                    completed_at,
                    claim_by,
                    claim_until,
                    message_id,
                ),
            )
        result = self.get(message_id)
        if result is None:
            raise A2AInboxError("message not found")
        return result

    def sweep(self) -> Dict[str, int]:
        now = _now()
        with self._connect() as conn:
            expired = self._expire_locked(conn, now)
            reclaimed = conn.execute(
                """
                UPDATE messages
                SET status='queued', claim_by=NULL, claim_until=NULL, updated_at=?
                WHERE status='claimed' AND claim_until IS NOT NULL AND claim_until<=?
                """,
                (now, now),
            ).rowcount
        return {"expired": int(expired), "reclaimed": int(reclaimed)}

    def _expire_locked(self, conn: sqlite3.Connection, now: float) -> int:
        return int(conn.execute(
            """
            UPDATE messages
            SET status='expired', updated_at=?, completed_at=?
            WHERE status NOT IN ('completed', 'failed', 'expired', 'cancelled')
              AND expires_at IS NOT NULL
              AND expires_at<=?
            """,
            (now, now, now),
        ).rowcount)


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    payload_text = row["payload_json"] or "{}"
    try:
        payload = json.loads(payload_text)
    except Exception:
        payload = {}
    return {
        "id": row["id"],
        "target": row["target"],
        "sender": row["sender"],
        "kind": row["kind"],
        "topic": row["topic"],
        "summary": row["summary"],
        "payload": payload,
        "status": row["status"],
        "attempts": int(row["attempts"] or 0),
        "max_attempts": int(row["max_attempts"] or 0),
        "claim_by": row["claim_by"],
        "claim_until": row["claim_until"],
        "run_id": row["run_id"],
        "record_handle": row["record_handle"],
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "due_at": row["due_at"],
        "expires_at": row["expires_at"],
        "completed_at": row["completed_at"],
    }
