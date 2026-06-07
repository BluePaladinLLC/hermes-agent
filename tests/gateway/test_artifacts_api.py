import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.artifacts import ArtifactKind, ArtifactRecord, ArtifactScope, ArtifactStore


def test_artifact_record_renders_discord_card_and_drawer_payload():
    record = ArtifactRecord(
        artifact_id="art_123",
        kind=ArtifactKind.MOCKUP,
        title="Discord artifact drawer",
        summary="Glassy drawer prototype with rail-only closed state.",
        scope=ArtifactScope(platform="discord", chat_id="1486408178009637097", thread_id="axon"),
        owner="Axon",
        evidence=["node --check passed", "browser console clean"],
        preview_url="http://127.0.0.1:9120/artifacts/art_123",
    )

    drawer = record.to_drawer_payload()
    card = record.to_discord_card("http://127.0.0.1:9120")

    assert drawer["kind"] == "mockup"
    assert drawer["scope"]["label"] == "discord:1486408178009637097:axon"
    assert card["embed"]["title"] == "◆ Discord artifact drawer"
    assert card["components"][0] == {
        "type": "button",
        "style": "link",
        "label": "Open drawer",
        "url": "http://127.0.0.1:9120/artifacts/art_123",
    }
    assert card["components"][1]["custom_id"] == "artifact:copy:art_123"


def test_artifact_record_rejects_missing_scope_chat_id():
    record = ArtifactRecord(
        artifact_id="art_bad",
        kind=ArtifactKind.RUN,
        title="Bad artifact",
        summary="Missing chat scope should fail.",
        scope=ArtifactScope(platform="discord", chat_id=""),
    )

    with pytest.raises(ValueError, match="scope.chat_id"):
        record.validate()


def test_artifact_store_filters_by_discord_scope_and_kind():
    store = ArtifactStore()
    axon = ArtifactRecord(
        artifact_id="art_axon",
        kind=ArtifactKind.MOCKUP,
        title="Axon drawer",
        summary="Axon scoped artifact.",
        scope=ArtifactScope(platform="discord", chat_id="chan-1", thread_id="axon"),
    )
    canary = ArtifactRecord(
        artifact_id="art_canary",
        kind=ArtifactKind.CANARY,
        title="Canary result",
        summary="Canary scoped artifact.",
        scope=ArtifactScope(platform="discord", chat_id="chan-2", thread_id="canaries"),
    )
    store.upsert(axon)
    store.upsert(canary)

    results = store.list(platform="discord", chat_id="chan-1", kind="mockup")

    assert [item.artifact_id for item in results] == ["art_axon"]


def test_api_server_artifacts_endpoint_returns_scoped_json():
    async def run_case():
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        adapter.artifacts.upsert(
            ArtifactRecord(
                artifact_id="art_axon",
                kind=ArtifactKind.MOCKUP,
                title="Axon drawer",
                summary="Axon scoped artifact.",
                scope=ArtifactScope(platform="discord", chat_id="chan-1", thread_id="axon"),
            )
        )
        adapter.artifacts.upsert(
            ArtifactRecord(
                artifact_id="art_other",
                kind=ArtifactKind.RUN,
                title="Other run",
                summary="Other channel artifact.",
                scope=ArtifactScope(platform="discord", chat_id="chan-2"),
            )
        )
        app = web.Application()
        app.router.add_get("/api/artifacts", adapter._handle_list_artifacts)
        app.router.add_get("/api/artifacts/{artifact_id}", adapter._handle_get_artifact)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/api/artifacts?platform=discord&chat_id=chan-1")
            assert resp.status == 200
            body = await resp.json()
            assert [item["artifact_id"] for item in body["artifacts"]] == ["art_axon"]
            assert body["artifacts"][0]["scope"]["label"] == "discord:chan-1:axon"

            resp = await cli.get("/api/artifacts/art_axon")
            assert resp.status == 200
            detail = await resp.json()
            assert detail["artifact"]["title"] == "Axon drawer"

            resp = await cli.get("/api/artifacts/missing")
            assert resp.status == 404

    import asyncio
    asyncio.run(run_case())


def test_api_server_serves_artifact_drawer_html_shell():
    async def run_case():
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        app = web.Application()
        app.router.add_get("/artifacts", adapter._handle_artifact_drawer)
        app.router.add_get("/artifacts/{artifact_id}", adapter._handle_artifact_drawer)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/artifacts/art_123?platform=discord&chat_id=chan-1")
            assert resp.status == 200
            assert resp.content_type == "text/html"
            body = await resp.text()
            assert "Discord Artifact Drawer" in body
            assert "window.__HERMES_ARTIFACT_DRAWER__" in body
            assert '"artifactId": "art_123"' in body
            assert '"chatId": "chan-1"' in body

    import asyncio
    asyncio.run(run_case())


def test_api_server_can_seed_artifact_and_render_discord_card_payload():
    async def run_case():
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        app = web.Application()
        app.router.add_post("/api/artifacts", adapter._handle_create_artifact)
        app.router.add_get("/api/artifacts/{artifact_id}", adapter._handle_get_artifact)
        app.router.add_get("/api/artifacts/{artifact_id}/discord-card", adapter._handle_get_artifact_discord_card)

        payload = {
            "artifact_id": "art_seeded",
            "kind": "mockup",
            "title": "Seeded drawer MVP",
            "summary": "Manual zero-blast seed artifact.",
            "scope": {"platform": "discord", "chat_id": "chan-1", "thread_id": "axon"},
            "owner": "Axon",
            "evidence": ["manual seed route"],
        }

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/api/artifacts", json=payload)
            assert resp.status == 201
            created = await resp.json()
            assert created["artifact"]["artifact_id"] == "art_seeded"

            resp = await cli.get("/api/artifacts/art_seeded/discord-card?drawer_base_url=http://127.0.0.1:8642")
            assert resp.status == 200
            card = await resp.json()
            assert card["artifact_id"] == "art_seeded"
            assert card["discord"]["embed"]["title"] == "◆ Seeded drawer MVP"
            assert card["discord"]["components"][0]["url"] == "http://127.0.0.1:8642/artifacts/art_seeded"

    import asyncio
    asyncio.run(run_case())


def test_artifact_drawer_shell_contains_client_hydration_code():
    async def run_case():
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        app = web.Application()
        app.router.add_get("/artifacts/{artifact_id}", adapter._handle_artifact_drawer)

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.get("/artifacts/art_123?platform=discord&chat_id=chan-1&thread_id=axon")
            assert resp.status == 200
            body = await resp.text()
            assert "artifact-list" in body
            assert "artifact-detail" in body
            assert "fetch(artifactApiUrl)" in body
            assert "renderArtifacts" in body
            assert '"threadId": "axon"' in body

    import asyncio
    asyncio.run(run_case())
