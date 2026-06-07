"""Throwaway contract spike for Discord artifact drawer v1.

This is not production code. It validates the data shape that can feed both:
1. a compact Discord artifact receipt/card, and
2. a Hermes-hosted glass drawer UI.

Run from repo root:
    python .planning/spikes/001-discord-artifact-surface/artifact_contract.py
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
import json


MAX_DISCORD_FIELD = 1024
MAX_DISCORD_DESCRIPTION = 4096


class ArtifactKind(str, Enum):
    MOCKUP = "mockup"
    PLAN = "plan"
    RUN = "run"
    MEDIA = "media"
    HANDOFF = "handoff"
    CANARY = "canary"


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
        return ":".join(parts)


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    kind: ArtifactKind
    title: str
    summary: str
    scope: ArtifactScope
    owner: str = "Axon"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    preview_url: str | None = None
    local_path: str | None = None
    evidence: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=lambda: ["open", "copy_link", "pin", "handoff"])
    tags: list[str] = field(default_factory=list)

    def validate(self) -> None:
        required = {
            "artifact_id": self.artifact_id,
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
        data["kind"] = self.kind.value
        data["scope"]["label"] = self.scope.label()
        return data

    def to_discord_card(self, drawer_base_url: str) -> dict[str, Any]:
        """Return a discord.py-friendly embed/buttons payload shape.

        Production code would map this into discord.Embed + discord.ui.View.
        The spike keeps it JSON-serializable so it can be tested without discord.py.
        """
        self.validate()
        drawer_url = f"{drawer_base_url.rstrip('/')}/artifacts/{self.artifact_id}"
        fields = [
            {"name": "Type", "value": self.kind.value, "inline": True},
            {"name": "Scope", "value": self.scope.label(), "inline": True},
        ]
        if self.evidence:
            fields.append({"name": "Evidence", "value": "\n".join(f"• {e}" for e in self.evidence[:5]), "inline": False})
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


def sample_record() -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id="art_axon_drawer_001",
        kind=ArtifactKind.MOCKUP,
        title="Discord artifact drawer standalone",
        summary="Glassy right-side artifact drawer prototype with rail-only closed state, channel-scoped list, and detail view.",
        scope=ArtifactScope(platform="discord", chat_id="1486408178009637097", thread_id="axon"),
        preview_url="http://127.0.0.1:9120/artifacts/art_axon_drawer_001",
        local_path="C:/Users/User/artifacts/discord-artifact-drawer-standalone/index.html",
        evidence=[
            "node --check app.js passed",
            "browser console clean",
            "desktop/mobile screenshots generated",
        ],
        tags=["discord", "artifacts", "prototype", "drawer"],
    )


def _assert_contract() -> None:
    record = sample_record()
    drawer = record.to_drawer_payload()
    card = record.to_discord_card("http://127.0.0.1:9120")
    assert drawer["artifact_id"] == "art_axon_drawer_001"
    assert drawer["scope"]["label"].startswith("discord:1486408178009637097")
    assert card["embed"]["title"].startswith("◆")
    assert card["components"][0]["style"] == "link"
    assert card["components"][0]["url"].endswith("/artifacts/art_axon_drawer_001")
    assert len(json.dumps(card)) < 6000


def main() -> None:
    _assert_contract()
    record = sample_record()
    print("=== Discord card payload ===")
    print(json.dumps(record.to_discord_card("http://127.0.0.1:9120"), indent=2))
    print("\n=== Drawer payload ===")
    print(json.dumps(record.to_drawer_payload(), indent=2))
    print("\nVERDICT: PARTIAL / PROCEED — shared artifact contract feeds both Discord card and hosted drawer.")


if __name__ == "__main__":
    main()
