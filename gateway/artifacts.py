"""Artifact records shared by Discord cards and Hermes-hosted drawer UIs.

The artifact surface is intentionally inert until callers add records to an
``ArtifactStore``.  It provides a small, serializable contract that can feed both
low-scroll Discord receipts and richer web drawer payloads without coupling the
model to discord.py or any frontend framework.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable

MAX_DISCORD_FIELD = 1024
MAX_DISCORD_DESCRIPTION = 4096


class ArtifactKind(str, Enum):
    MOCKUP = "mockup"
    PLAN = "plan"
    RUN = "run"
    MEDIA = "media"
    HANDOFF = "handoff"
    CANARY = "canary"
    INDEX = "index"


@dataclass(frozen=True)
class ArtifactScope:
    platform: str
    chat_id: str
    thread_id: str | None = None
    session_id: str | None = None

    def label(self) -> str:
        parts = [self.platform, self.chat_id]
        if self.thread_id:
            parts.append(self.thread_id)
        if self.session_id:
            parts.append(self.session_id)
        return ":".join(parts)


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    kind: ArtifactKind | str
    title: str
    summary: str
    scope: ArtifactScope
    owner: str = "Hermes"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    preview_url: str | None = None
    local_path: str | None = None
    evidence: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=lambda: ["open", "copy_link", "pin", "handoff"])
    tags: list[str] = field(default_factory=list)

    @property
    def kind_value(self) -> str:
        if isinstance(self.kind, ArtifactKind):
            return self.kind.value
        return str(self.kind).strip().lower()

    def validate(self) -> None:
        required = {
            "artifact_id": self.artifact_id,
            "kind": self.kind_value,
            "title": self.title,
            "summary": self.summary,
            "scope.platform": self.scope.platform,
            "scope.chat_id": self.scope.chat_id,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError(f"missing required artifact fields: {', '.join(missing)}")
        if len(self.summary) > MAX_DISCORD_DESCRIPTION:
            raise ValueError("summary exceeds Discord embed description limit")
        for item in self.evidence:
            if len(item) > MAX_DISCORD_FIELD:
                raise ValueError("evidence item exceeds Discord embed field limit")

    def to_drawer_payload(self) -> dict[str, Any]:
        self.validate()
        data = asdict(self)
        data["kind"] = self.kind_value
        data["scope"]["label"] = self.scope.label()
        return data

    def to_discord_card(self, drawer_base_url: str) -> dict[str, Any]:
        """Return a JSON-serializable Discord embed/button payload shape."""
        self.validate()
        drawer_url = self.preview_url or f"{drawer_base_url.rstrip('/')}/artifacts/{self.artifact_id}"
        fields = [
            {"name": "Type", "value": self.kind_value, "inline": True},
            {"name": "Scope", "value": self.scope.label(), "inline": True},
        ]
        if self.evidence:
            fields.append({
                "name": "Evidence",
                "value": "\n".join(f"• {item}" for item in self.evidence[:5]),
                "inline": False,
            })
        return {
            "embed": {
                "title": f"◆ {self.title}",
                "description": self.summary,
                "color": 0x8F6DFF,
                "fields": fields,
                "footer": {"text": f"Artifact {self.artifact_id} · {self.owner}"},
            },
            "components": [
                {"type": "button", "style": "link", "label": "Open drawer", "url": drawer_url},
                {"type": "button", "style": "secondary", "label": "Copy link", "custom_id": f"artifact:copy:{self.artifact_id}"},
                {"type": "button", "style": "secondary", "label": "Pin", "custom_id": f"artifact:pin:{self.artifact_id}"},
                {"type": "button", "style": "primary", "label": "Handoff", "custom_id": f"artifact:handoff:{self.artifact_id}"},
            ],
        }


class ArtifactStore:
    """Small in-memory artifact index used by API/dashboard surfaces.

    Persistence is deliberately out of scope for this first zero-blast-radius
    slice; callers can seed this from ledger/session/file records later.
    """

    def __init__(self, records: Iterable[ArtifactRecord] | None = None):
        self._records: dict[str, ArtifactRecord] = {}
        for record in records or []:
            self.upsert(record)

    def upsert(self, record: ArtifactRecord) -> ArtifactRecord:
        record.validate()
        self._records[record.artifact_id] = record
        return record

    def get(self, artifact_id: str) -> ArtifactRecord | None:
        return self._records.get(artifact_id)

    def list(
        self,
        *,
        platform: str | None = None,
        chat_id: str | None = None,
        thread_id: str | None = None,
        session_id: str | None = None,
        kind: str | None = None,
        query: str | None = None,
    ) -> list[ArtifactRecord]:
        records = list(self._records.values())
        if platform:
            records = [r for r in records if r.scope.platform == platform]
        if chat_id:
            records = [r for r in records if r.scope.chat_id == chat_id]
        if thread_id:
            records = [r for r in records if r.scope.thread_id == thread_id]
        if session_id:
            records = [r for r in records if r.scope.session_id == session_id]
        if kind and kind.lower() != "all":
            wanted = kind.strip().lower()
            records = [r for r in records if r.kind_value == wanted]
        if query:
            needle = query.strip().lower()
            if needle:
                records = [
                    r for r in records
                    if needle in " ".join([r.title, r.summary, r.owner, " ".join(r.tags), " ".join(r.evidence)]).lower()
                ]
        return sorted(records, key=lambda r: r.created_at, reverse=True)
