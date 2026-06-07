"""Discord-native artifact drawer panel renderer.

This is the in-Discord MVP surface: a compact ephemeral/message panel that
feels like a drawer inside the channel even before the artifact index is fully
wired.  It does not send messages by itself; the Discord adapter decides when to
render it.
"""
from __future__ import annotations

from gateway.artifacts import ArtifactRecord, ArtifactScope


MAX_PANEL_CHARS = 1850


def sample_discord_drawer_artifacts(*, chat_id: str, thread_id: str | None = None) -> list[ArtifactRecord]:
    scope = ArtifactScope(platform="discord", chat_id=str(chat_id), thread_id=str(thread_id) if thread_id else None)
    return [
        ArtifactRecord(
            artifact_id="art_seeded",
            kind="mockup",
            title="Seeded drawer MVP",
            summary="Manual zero-blast artifact drawer seed; proves list/detail behavior in Discord.",
            scope=scope,
            owner="Axon",
            evidence=["manual seed", "hydrated shell", "Discord panel renderer"],
            tags=["mvp", "zero-blast"],
        ),
        ArtifactRecord(
            artifact_id="art_contract",
            kind="plan",
            title="Artifact contract spike",
            summary="Shared payload contract for Discord receipt cards and hosted drawer JSON.",
            scope=scope,
            owner="Axon",
            evidence=["ArtifactRecord", "ArtifactStore", "focused tests"],
            tags=["contract", "api"],
        ),
    ]


def _line_trim(value: str, limit: int = 96) -> str:
    value = " ".join(str(value or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def render_artifact_drawer_panel(
    artifacts: list[ArtifactRecord], *, selected_id: str | None = None, query: str | None = None
) -> str:
    """Render a low-scroll Discord panel that behaves like a small drawer."""
    selected = None
    if artifacts:
        selected = next((item for item in artifacts if item.artifact_id == selected_id), artifacts[0])

    lines = [
        "▌ Artifact drawer  ·  Discord-native MVP",
        "│ Use `/artifacts`, `/artifacts open <id>`, or `/artifacts search <text>`.",
    ]
    if query:
        lines.append(f"│ Filter: `{_line_trim(query, 80)}`")
    lines.append("├─ Recent")

    if not artifacts:
        lines.append("│ No artifacts in this scope yet. This panel is ready for manual seeds.")
    else:
        for item in artifacts[:5]:
            marker = "▶" if selected and item.artifact_id == selected.artifact_id else "•"
            lines.append(f"│ {marker} `{item.artifact_id}` — {_line_trim(item.title, 54)}  _{item.kind_value}_")

    lines.append("├─ Detail")
    if selected:
        evidence = "; ".join(selected.evidence[:3]) if selected.evidence else "no evidence yet"
        lines.extend([
            f"│ **{_line_trim(selected.title, 80)}**",
            f"│ {_line_trim(selected.summary, 120)}",
            f"│ Evidence: {_line_trim(evidence, 120)}",
            f"│ Open: /artifacts open {selected.artifact_id}  ·  Search: /artifacts search <text>",
        ])
    else:
        lines.append("│ Select or seed an artifact to populate the detail lane.")

    lines.append("└─ Status: zero-blast preview · no auto-capture · no channel send")
    panel = "\n".join(lines)
    if len(panel) > MAX_PANEL_CHARS:
        panel = panel[: MAX_PANEL_CHARS - 1] + "…"
    return panel
