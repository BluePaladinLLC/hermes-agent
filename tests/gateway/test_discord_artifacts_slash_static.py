from pathlib import Path


def test_discord_adapter_registers_artifacts_slash_panel():
    source = Path("plugins/platforms/discord/adapter.py").read_text(encoding="utf-8")

    assert '@tree.command(name="artifacts"' in source
    assert "_handle_artifacts_slash" in source
    assert "render_artifact_drawer_panel" in source
    assert "sample_discord_drawer_artifacts" in source
