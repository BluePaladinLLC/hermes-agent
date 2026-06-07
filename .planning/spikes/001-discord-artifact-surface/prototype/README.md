# Discord Artifact Drawer — Standalone Prototype

Fast standalone implementation of the artifact drawer direction.

## Open

Open this file in a browser:

```text
C:\Users\User\artifacts\discord-artifact-drawer-standalone\index.html
```

## What is implemented

- Discord-like dark shell with guild rail, channel rail, messages, and composer.
- Slim right-side artifact rail with open/closed drawer behavior.
- Channel-scoped artifact list, tabs, filtering, reset.
- Artifact detail view with scope, actions, evidence, and risk fields.
- Channel switching for `#axon`, `#handoffs`, `#canaries`, `#artifacts`, `#a2a`, etc.
- Compact density mode and drawer glass/opacity control persisted in `localStorage`.
- Keyboard shortcuts: `Esc` closes detail/drawer, `Ctrl/Cmd+K` opens drawer search.
- Mobile-width layout fixes so the conversation and drawer remain visible.

## Intentional limits

- This is a standalone local prototype, not a real Discord plugin.
- Artifact data is static sample data.
- Real integration still needs an architecture spike for Discord/webview/plugin constraints.

## QA evidence

Screenshots are in:

```text
C:\Users\User\artifacts\discord-artifact-drawer-standalone\screenshots\
```
