# 001 — Discord Artifact Surface Spike

## Trigger

Bruno approved moving beyond the standalone artifact drawer mock-up toward a real v1 path.

## Core feasibility questions

| # | Spike | Validates | Risk |
|---|-------|-----------|------|
| 001a | Discord-native card | Given Hermes can send Discord embeds/buttons, when an artifact is produced, then Discord can show a compact receipt/card with actions. | Low |
| 001b | Drawer web surface | Given the Discord UI cannot be arbitrarily extended by a normal bot, when Bruno clicks/open-links from Discord, then a Hermes-hosted drawer can provide the glassy artifact browser. | Medium |
| 001c | Data contract | Given artifacts may come from files, runs, plans, media, and handoffs, when they are indexed, then both Discord card and drawer consume one stable artifact payload. | High |

## Current repo evidence

- Active repo: `https://github.com/BluePaladinLLC/hermes-agent.git`.
- Discord adapter already supports embeds and `discord.ui.View` buttons in `plugins/platforms/discord/adapter.py`.
- Hermes API/dashboard already has web serving paths in `hermes_cli/web_server.py` and API server session routes in `gateway/platforms/api_server.py`.
- Standalone prototype exists outside repo at `C:\Users\User\artifacts\discord-artifact-drawer-standalone\`.

## Recommended v1 architecture

**Do not try to make Discord itself render a true side drawer from a normal bot message.** Treat Discord as the radio/inbox:

1. Agent/run emits an `ArtifactRecord`.
2. Gateway posts a compact Discord artifact receipt/card with buttons: `Open drawer`, `Copy link`, `Pin`, `Handoff`.
3. `Open drawer` points to a Hermes-hosted web drawer scoped by platform/chat/thread/session.
4. The web drawer reuses the standalone glassy UI direction and reads artifact records from a local API.
5. Discord remains low-scroll; the drawer carries dense browsing.

## Spike implementation in this folder

`artifact_contract.py` defines a minimal artifact payload and renders:

- a Discord embed/action-card payload shape;
- a drawer JSON payload shape;
- validation checks for field presence, channel scope, and safe preview length.

Run:

```bash
python .planning/spikes/001-discord-artifact-surface/artifact_contract.py
```

The standalone visual prototype is also snapshotted under `prototype/` as inert static reference assets:

- `prototype/index.html`
- `prototype/styles.css`
- `prototype/app.js`
- `prototype/README.md`

These preserve the dark translucent Discord drawer direction in-repo without activating it in the live gateway.

For hands-on MVP review without touching the live gateway, run the local preview server:

```bash
PYTHONPATH=. python .planning/spikes/001-discord-artifact-surface/preview_server.py --host 127.0.0.1 --port 8877
```

Then open:

```text
http://127.0.0.1:8877/artifacts/art_seeded?platform=discord&chat_id=chan-1&thread_id=axon
```

The preview server seeds sample artifacts in-memory only; it does not send Discord messages or enable automatic capture.

## Verdict: PARTIAL / PROCEED

### What worked

- A single artifact contract can feed both Discord cards and drawer UI.
- The native Discord piece should stay compact and action-oriented.
- The drawer should be a Hermes-hosted web surface, not a fake bot-side panel.

### What did not get proven yet

- Live Discord button callback and URL-opening behavior in the target server.
- Auth/session token flow from Discord message to hosted drawer.
- Persistent artifact storage location: likely ledger/API surface, not raw chat transcript.

### Recommendation for real build

Build v1 in thin vertical slices:

1. Add `ArtifactRecord` model + serializer with tests.
2. Add a local `/api/artifacts` read-only endpoint scoped by platform/chat/thread/session.
3. Add Discord artifact receipt/card rendering behind a feature flag.
4. Mount the drawer static app under dashboard/API server and hydrate from `/api/artifacts`.
5. Canary in `#axon` with local/static artifacts before wiring automatic capture from all tool outputs.
