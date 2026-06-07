"""Zero-blast local preview server for the Discord artifact drawer MVP.

This runs outside the live Hermes gateway and seeds a few sample artifacts so the
hosted drawer can be reviewed from a browser/Discord link before any automatic
artifact capture or Discord sending is activated.
"""
from __future__ import annotations

import argparse

from aiohttp import web

from gateway.artifacts import ArtifactRecord, ArtifactScope
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


def build_app() -> web.Application:
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter.artifacts.upsert(
        ArtifactRecord(
            artifact_id="art_seeded",
            kind="mockup",
            title="Seeded drawer MVP",
            summary="Manual zero-blast seed artifact rendered through the hosted drawer.",
            scope=ArtifactScope(platform="discord", chat_id="chan-1", thread_id="axon"),
            owner="Axon",
            evidence=["manual seed route", "hydrated drawer shell", "Discord-card JSON available"],
            tags=["mvp", "zero-blast"],
        )
    )
    adapter.artifacts.upsert(
        ArtifactRecord(
            artifact_id="art_contract",
            kind="plan",
            title="Artifact contract spike",
            summary="Shared payload contract feeding both compact Discord receipt cards and the hosted drawer.",
            scope=ArtifactScope(platform="discord", chat_id="chan-1", thread_id="axon"),
            owner="Axon",
            evidence=["ArtifactRecord serializer", "ArtifactStore scoped filtering", "7 focused tests passed"],
            tags=["contract", "api"],
        )
    )

    app = web.Application()
    app.router.add_get("/artifacts", adapter._handle_artifact_drawer)
    app.router.add_get("/artifacts/{artifact_id}", adapter._handle_artifact_drawer)
    app.router.add_get("/api/artifacts", adapter._handle_list_artifacts)
    app.router.add_post("/api/artifacts", adapter._handle_create_artifact)
    app.router.add_get("/api/artifacts/{artifact_id}/discord-card", adapter._handle_get_artifact_discord_card)
    app.router.add_get("/api/artifacts/{artifact_id}", adapter._handle_get_artifact)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the artifact drawer MVP preview server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args()
    web.run_app(build_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
