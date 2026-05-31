# Hermes Desktop

The native desktop app for [Hermes Agent](../../README.md) — the self-improving AI agent from [Nous Research](https://nousresearch.com). Same agent, same skills, same memory as the CLI and gateway, in a polished native window: chat with streaming tool output, side-by-side previews, a file browser, voice, and settings — no terminal required.

Available for **macOS, Windows, and Linux**.

## Install

### Download an installer (easiest)

Grab the build for your OS from the [latest release](https://github.com/NousResearch/hermes-agent/releases/latest):

| OS | File |
|----|------|
| macOS | `.dmg` — open it, drag **Hermes** to Applications |
| Windows | `.exe` — run the installer (`.msi` also available for managed deploys) |
| Linux | `.AppImage` (portable), or `.deb` / `.rpm` |

On first launch Hermes sets itself up automatically — it installs the agent runtime, then walks you through picking a provider and model. Nothing else to configure.

### Already have the Hermes CLI?

If you've installed Hermes via the [one-line installer](../../README.md#quick-install), just run:

```bash
hermes desktop
```

It builds and launches the GUI against your existing install — same config, keys, sessions, and skills.

To get the desktop alongside a fresh CLI install, add `--include-desktop` to the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --include-desktop
```

## What you get

- **Chat with the full agent** — streaming responses, live tool activity, structured tool summaries, and the same conversation history as every other Hermes surface.
- **Side-by-side previews** — render web pages, files, and outputs in a right-hand pane while you keep chatting.
- **File browser & editor previews** — explore the working directory without leaving the app.
- **Voice** — talk to Hermes and hear it back.
- **Settings & onboarding** — manage providers, models, tools, and credentials from a real UI; first-run setup gets you to your first message in seconds.
- **Stays current** — built-in updates pull the latest agent and rebuild the app in place.

## Updating

The app checks for updates in the background and offers a one-click update when one is ready. You can also update any time from the CLI:

```bash
hermes update
```

## Requirements

The installer handles everything for you (Python 3.11+, a portable Git, ripgrep). The only thing worth knowing:

- **Windows:** the installer bundles its own Git and Python — no admin rights or system changes required.
- **macOS / Linux:** uses your system Python 3.11+ (installed automatically if missing).

---

## For contributors

Want to hack on the app itself? Install workspace deps from the repo root once, then run the dev server from this directory:

```bash
npm install          # from repo root — links apps/desktop, web, apps/shared
cd apps/desktop
npm run dev          # Vite renderer + Electron, which boots the Python backend
```

Point the app at a specific source checkout, or sandbox it away from your real config:

```bash
HERMES_DESKTOP_HERMES_ROOT=/path/to/clone npm run dev
HERMES_HOME=/tmp/throwaway npm run dev
npm run dev:fake-boot   # exercise the startup overlay with deterministic delays
```

### Building installers

```bash
npm run dist:mac     # DMG + zip
npm run dist:win     # NSIS + MSI
npm run dist:linux   # AppImage + deb + rpm
npm run pack         # unpacked app under release/ (no installer)
```

Installers are built and uploaded to GitHub Releases manually. macOS/Windows signing & notarization happen automatically when the relevant credentials are present in the environment (`CSC_LINK` / `CSC_KEY_PASSWORD` / `APPLE_*` for macOS, `WIN_CSC_*` for Windows).

### How it works

The packaged app ships only the Electron shell. On first launch it installs the Hermes Agent runtime into `HERMES_HOME` (`~/.hermes`, or `%LOCALAPPDATA%\hermes` on Windows) — the **same layout a CLI install uses**, so the two are interchangeable. The renderer (React, in `src/`) talks to a `hermes dashboard --tui` backend over the standard gateway APIs and reuses the embedded TUI rather than reimplementing chat. The install, backend-resolution, and self-update logic all live in `electron/main.cjs`.

### Verification

Run before opening a PR (lint may surface pre-existing warnings but must exit cleanly):

```bash
npm run fix
npm run type-check
npm run lint
npm run test:desktop:all
```

### Troubleshooting

Boot logs land in `HERMES_HOME/logs/desktop.log` (includes backend output and recent Python tracebacks) — check it first if the app reports a boot failure.

```bash
# Force a clean first-launch setup
rm "$HOME/.hermes/hermes-agent/.hermes-bootstrap-complete"   # macOS/Linux
# Rebuild a broken Python venv
rm -rf "$HOME/.hermes/hermes-agent/venv"                     # macOS/Linux
# Reset a stuck macOS microphone prompt
tccutil reset Microphone com.nousresearch.hermes
```
