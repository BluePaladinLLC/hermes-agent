from gateway.artifacts import ArtifactRecord, ArtifactScope
from gateway.artifact_discord_panel import render_artifact_drawer_panel, sample_discord_drawer_artifacts


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
