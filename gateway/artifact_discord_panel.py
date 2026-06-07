"""Discord-native artifact drawer panel renderer.

This is the in-Discord MVP surface: a compact ephemeral/message panel that
feels like a drawer inside the channel even before the artifact index is fully
wired.  It does not send messages by itself; the Discord adapter decides when to
render it.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from gateway.artifacts import ArtifactKind, ArtifactRecord, ArtifactScope


MAX_PANEL_CHARS = 1850
DEFAULT_LOCAL_ARTIFACT_ROOTS = (
    Path("C:/Users/User/artifacts"),
    Path("C:/Users/User/AppData/Local/hermes/profiles/axon/cache/documents"),
)


def _artifact_kind_for_path(path: Path) -> ArtifactKind:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp3", ".wav", ".mp4", ".mov"}:
        return ArtifactKind.MEDIA
    if suffix in {".html", ".svg"}:
        return ArtifactKind.MOCKUP
    if suffix in {".md", ".txt", ".json"}:
        return ArtifactKind.PLAN
    if suffix in {".pdf", ".docx", ".pptx", ".xlsx", ".csv"}:
        return ArtifactKind.HANDOFF
    return ArtifactKind.INDEX


def _artifact_id_for_path(path: Path) -> str:
    digest = hashlib.sha1(str(path).encode("utf-8", errors="ignore")).hexdigest()[:10]
    stem = "".join(ch.lower() if ch.isalnum() else "_" for ch in path.stem).strip("_")[:24] or "file"
    return f"art_{stem}_{digest}"


def _local_artifact_roots() -> list[Path]:
    override = os.getenv("HERMES_DISCORD_ARTIFACT_ROOTS") or os.getenv("HERMES_ARTIFACT_ROOTS")
    if not override:
        return list(DEFAULT_LOCAL_ARTIFACT_ROOTS)
    return [Path(part.strip()) for part in override.split(os.pathsep) if part.strip()]


def discover_local_artifacts(
    *,
    chat_id: str,
    thread_id: str | None = None,
    query: str | None = None,
    limit: int = 12,
    roots: list[Path] | None = None,
) -> list[ArtifactRecord]:
    """Return recent local artifact files as Discord drawer records.

    This is read-only and intentionally conservative: it indexes likely
    deliverables/mockups from known artifact/cache roots, not arbitrary files.
    """
    scope = ArtifactScope(platform="discord", chat_id=str(chat_id), thread_id=str(thread_id) if thread_id else None)
    needles = [part for part in (query or "").lower().split() if part]
    candidates: list[tuple[float, Path]] = []
    for root in roots or _local_artifact_roots():
        if not root.exists() or not root.is_dir():
            continue
        try:
            iterator = root.rglob("*")
            for path in iterator:
                try:
                    if not path.is_file():
                        continue
                    if path.name.startswith(".") or path.suffix.lower() in {".tmp", ".log", ".pyc"}:
                        continue
                    stat = path.stat()
                    if stat.st_size <= 0 or stat.st_size > 25 * 1024 * 1024:
                        continue
                    haystack = f"{path.name} {path.parent.name}".lower()
                    if needles and not all(needle in haystack for needle in needles):
                        continue
                    candidates.append((stat.st_mtime, path))
                except OSError:
                    continue
        except OSError:
            continue

    records: list[ArtifactRecord] = []
    for _, path in sorted(candidates, reverse=True)[:limit]:
        try:
            stat = path.stat()
        except OSError:
            continue
        size_kb = max(1, round(stat.st_size / 1024))
        parent = path.parent.name
        records.append(
            ArtifactRecord(
                artifact_id=_artifact_id_for_path(path),
                kind=_artifact_kind_for_path(path),
                title=path.stem.replace("_", " ").replace("-", " ").strip() or path.name,
                summary=f"Local artifact from {parent}: {path.name} ({size_kb} KB).",
                scope=scope,
                owner="Axon local index",
                local_path=str(path),
                evidence=["local file index", f"{size_kb} KB", path.suffix.lower() or "no extension"],
                tags=["local", "indexed", parent],
            )
        )
    return records


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

    live_mode = any(item.owner == "Axon local index" for item in artifacts)
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
        if selected.local_path:
            lines.append(f"│ Path: `{_line_trim(selected.local_path, 120)}`")
    else:
        lines.append("│ Select or seed an artifact to populate the detail lane.")

    status = "local read-only index" if live_mode else "seeded preview"
    lines.append(f"└─ Status: {status} · no auto-capture · no channel send")
    panel = "\n".join(lines)
    if len(panel) > MAX_PANEL_CHARS:
        panel = panel[: MAX_PANEL_CHARS - 1] + "…"
    return panel
