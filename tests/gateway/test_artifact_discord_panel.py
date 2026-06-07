from pathlib import Path

from gateway.artifacts import ArtifactRecord, ArtifactScope
from gateway.artifact_discord_panel import discover_local_artifacts, render_artifact_drawer_panel, sample_discord_drawer_artifacts


def test_render_artifact_drawer_panel_feels_like_discord_drawer():
    artifacts = [
        ArtifactRecord(
            artifact_id="art_seeded",
            kind="mockup",
            title="Seeded drawer MVP",
            summary="Manual zero-blast artifact drawer seed.",
            scope=ArtifactScope(platform="discord", chat_id="chan-1", thread_id="axon"),
            evidence=["manual seed", "hydrated shell"],
        )
    ]

    panel = render_artifact_drawer_panel(artifacts, selected_id="art_seeded")

    assert "▌ Artifact drawer" in panel
    assert "Seeded drawer MVP" in panel
    assert "manual seed" in panel
    assert "Open: /artifacts open art_seeded" in panel
    assert len(panel) < 1900


def test_sample_discord_drawer_artifacts_are_scoped_and_reviewable():
    artifacts = sample_discord_drawer_artifacts(chat_id="chan-1", thread_id="axon")

    assert len(artifacts) >= 2
    assert all(item.scope.platform == "discord" for item in artifacts)
    assert all(item.scope.chat_id == "chan-1" for item in artifacts)
    assert "Artifact contract" in render_artifact_drawer_panel(artifacts)


def test_discover_local_artifacts_indexes_recent_files(tmp_path: Path):
    root = tmp_path / "artifacts"
    mockup_dir = root / "discord-artifact-drawer"
    mockup_dir.mkdir(parents=True)
    html = mockup_dir / "index.html"
    html.write_text("<html><body>drawer</body></html>", encoding="utf-8")
    png = mockup_dir / "contact-sheet.png"
    png.write_bytes(b"not really png but enough for an index test")

    artifacts = discover_local_artifacts(chat_id="chan-1", thread_id="axon", query="drawer", roots=[root])

    assert {item.local_path for item in artifacts} == {str(html), str(png)}
    assert all(item.scope.chat_id == "chan-1" for item in artifacts)
    assert any(item.kind_value == "mockup" for item in artifacts)
    assert any(item.kind_value == "media" for item in artifacts)
    panel = render_artifact_drawer_panel(artifacts)
    assert "local read-only index" in panel
    assert "Path:" in panel
