"""Tests for the Buzz platform adapter plugin."""

import asyncio
from collections import OrderedDict
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.gateway._plugin_adapter_loader import load_plugin_adapter

# Load plugins/platforms/buzz/adapter.py under a unique module name
# (plugin_adapter_buzz) so it cannot collide with other plugin adapters
# loaded by sibling tests in the same xdist worker.
_buzz_mod = load_plugin_adapter("buzz")

BuzzAdapter = _buzz_mod.BuzzAdapter
hex_to_npub = _buzz_mod.hex_to_npub
npub_to_hex = _buzz_mod.npub_to_hex
_normalize_user_ref = _buzz_mod._normalize_user_ref
_cli_error_message = _buzz_mod._cli_error_message
_resolve_private_key = _buzz_mod._resolve_private_key
check_requirements = _buzz_mod.check_requirements
validate_config = _buzz_mod.validate_config
register = _buzz_mod.register
_env_enablement = _buzz_mod._env_enablement
_standalone_send = _buzz_mod._standalone_send

_nostr_auth = _buzz_mod._load_nostr_auth()

# Real key pair (Chip's public identity — public information, not a secret)
SELF_PUBKEY = "9fd5c7ba6d3ef224da78f541e0fcb9c50f72cc63edb19aae76ac6a0474dfa860"
SELF_NPUB = "npub1nl2u0wnd8mezfknc74q7pl9ec58h9nrrakce4tnk434qgaxl4psqe5twr6"
OTHER_PUBKEY = "a" * 64
CHANNEL = "ccc2bc1a-7a82-5a8f-8c4e-57a070cbe7cd"
# Real DM conversation as materialized by a hosted relay: `dms list` returns
# [] for it (#68871) while `channels list` shows it as name "DM", empty
# description, indistinguishable from a channel except via message p-tags.
DM_CHANNEL = "6468cc16-a114-4f23-8b8c-02c1655cbf6b"

_ENV_VARS = (
    "BUZZ_RELAY_URL",
    "BUZZ_PRIVATE_KEY",
    "BUZZ_CHANNELS",
    "BUZZ_HOME_CHANNEL",
    "BUZZ_ALLOWED_USERS",
    "BUZZ_ALLOW_ALL_USERS",
    "BUZZ_POLL_INTERVAL",
    "BUZZ_CLI_PATH",
    "BUZZ_CREDENTIALS_FILE",
    "BUZZ_OWNER_PUBKEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Keep tests hermetic: no ambient Buzz env vars or real credentials."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(_buzz_mod, "_DEFAULT_CREDENTIALS_DIR", tmp_path / "no-creds")
    yield


def _event(event_id, pubkey=OTHER_PUBKEY, content="hello", created_at=1000, kind=9):
    return {
        "id": event_id,
        "pubkey": pubkey,
        "content": content,
        "created_at": created_at,
        "kind": kind,
        "tags": [["h", CHANNEL]],
    }


def _make_adapter(extra=None):
    from gateway.config import PlatformConfig

    cfg = PlatformConfig(enabled=True, extra={"relay_url": "https://test.relay", **(extra or {})})
    adapter = BuzzAdapter(cfg)
    adapter._self_pubkey = SELF_PUBKEY
    adapter._self_npub = SELF_NPUB
    adapter._display_name = "Chip"
    adapter._private_key = "nsec1test"
    return adapter


class _ScriptedCli:
    """Fake ``_run_cli`` that routes on the buzz subcommand and records calls."""

    def __init__(self):
        self.responses = {}  # (group, cmd) -> list of (code, stdout, stderr)
        self.calls = []

    def script(self, group, cmd, payload, code=0, stderr=""):
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        self.responses.setdefault((group, cmd), []).append((code, stdout, stderr))

    async def __call__(self, args, *, input_text=None):
        self.calls.append((list(args), input_text))
        queue = self.responses.get((args[0], args[1]), [])
        if len(queue) > 1:
            return queue.pop(0)
        if queue:
            return queue[0]
        return 0, "[]", ""


# ── bech32 / identity helpers ─────────────────────────────────────────────


class TestBech32Helpers:

    def test_hex_to_npub_known_pair(self):
        assert hex_to_npub(SELF_PUBKEY) == SELF_NPUB

    def test_npub_to_hex_known_pair(self):
        assert npub_to_hex(SELF_NPUB) == SELF_PUBKEY


class TestSignedEventBuilder:
    def test_build_signed_event_uses_canonical_nostr_serialization(self):
        private_key = "1".zfill(64)
        event = _nostr_auth.build_signed_event(
            private_key=private_key,
            kind=20002,
            tags=[["h", CHANNEL]],
            content="",
            created_at=1_700_000_000,
            auxiliary_randomness=b"\x00" * 32,
        )
        canonical = json.dumps(
            [
                0,
                event["pubkey"],
                1_700_000_000,
                20002,
                [["h", CHANNEL]],
                "",
            ],
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        import hashlib

        assert event["id"] == hashlib.sha256(canonical).hexdigest()
        assert len(event["sig"]) == 128


# ── Adapter init / config precedence ──────────────────────────────────────


class TestBuzzAdapterInit:


    def test_init_from_config_extra(self):
        from gateway.config import PlatformConfig
        cfg = PlatformConfig(
            enabled=True,
            extra={
                "relay_url": "https://cfg.relay",
                "channels": ["ccc"],
                "poll_interval": 2,
                "home_channel": "ccc",
            },
        )
        adapter = BuzzAdapter(cfg)
        assert adapter.relay_url == "https://cfg.relay"
        assert adapter.channels == ["ccc"]
        assert adapter.poll_interval == 2.0
        assert adapter.home_channel == "ccc"

    def test_env_overrides_config(self, monkeypatch):
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://env.relay")
        from gateway.config import PlatformConfig
        adapter = BuzzAdapter(PlatformConfig(enabled=True, extra={"relay_url": "https://cfg.relay"}))
        assert adapter.relay_url == "https://env.relay"


class TestObserverActivity:
    @pytest.mark.asyncio
    async def test_publish_activity_encrypts_signs_tags_and_acks(self, monkeypatch):
        agent_key = "1".zfill(64)
        owner_key = "2".zfill(64)
        owner_pubkey = _nostr_auth.public_key_hex(owner_key)
        adapter = _make_adapter({"owner_pubkey": owner_pubkey})
        adapter._private_key = agent_key
        adapter._self_pubkey = _nostr_auth.public_key_hex(agent_key)
        websocket = AsyncMock()

        async def recv_ack():
            sent = json.loads(websocket.send.await_args.args[0])
            return json.dumps(["OK", sent[1]["id"], True, ""])

        websocket.recv.side_effect = recv_ack
        monkeypatch.setattr(
            adapter, "_ensure_typing_websocket", AsyncMock(return_value=websocket)
        )
        assert await adapter.publish_activity(
            {"kind": "turn_started", "sessionId": "s1"}
        )
        event = json.loads(websocket.send.await_args.args[0])[1]
        assert event["kind"] == 24200
        assert event["tags"] == [
            ["p", owner_pubkey],
            ["agent", adapter._self_pubkey],
            ["frame", "telemetry"],
        ]
        assert event["pubkey"] == adapter._self_pubkey
        assert len(event["id"]) == 64
        assert len(event["sig"]) == 128

    @pytest.mark.asyncio
    async def test_publish_activity_is_disabled_without_owner(self):
        adapter = _make_adapter()
        assert await adapter.publish_activity({"kind": "turn_started"}) is False


class TestTypingEvents:
    def test_build_typing_event_is_signed_ephemeral_channel_event(self, monkeypatch):
        adapter = _make_adapter()
        expected = {"id": "typing-event", "kind": 20002}
        build = MagicMock(return_value=expected)
        monkeypatch.setattr(
            _buzz_mod,
            "_load_nostr_auth",
            lambda: MagicMock(build_signed_event=build),
        )

        event = adapter._build_typing_event(CHANNEL)

        assert event == expected
        build.assert_called_once_with(
            private_key="nsec1test",
            kind=20002,
            tags=[["h", CHANNEL]],
            content="",
        )

    @pytest.mark.asyncio
    async def test_send_typing_reuses_authenticated_socket_and_waits_for_ok(self):
        adapter = _make_adapter()
        websocket = AsyncMock()
        websocket.recv.side_effect = [
            json.dumps(["OK", "typing-1", True, ""]),
            json.dumps(["OK", "typing-2", True, ""]),
        ]
        adapter._ensure_typing_websocket = AsyncMock(return_value=websocket)
        adapter._build_typing_event = MagicMock(
            side_effect=[{"id": "typing-1"}, {"id": "typing-2"}]
        )

        await adapter.send_typing(CHANNEL)
        await adapter.send_typing(CHANNEL)

        assert websocket.send.await_count == 2
        assert adapter._ensure_typing_websocket.await_count == 2
        adapter._build_typing_event.assert_called_with(CHANNEL, None)

    @pytest.mark.asyncio
    async def test_send_typing_rejection_is_best_effort_and_closes_socket(self):
        adapter = _make_adapter()
        websocket = AsyncMock()
        websocket.recv.return_value = json.dumps(
            ["OK", "typing-rejected", False, "rate limited"]
        )
        adapter._typing_websocket = websocket
        adapter._ensure_typing_websocket = AsyncMock(return_value=websocket)
        adapter._build_typing_event = MagicMock(
            return_value={"id": "typing-rejected"}
        )

        await adapter.send_typing(CHANNEL)

        websocket.close.assert_awaited_once()
        assert adapter._typing_websocket is None

    @pytest.mark.asyncio
    async def test_typing_handshake_cancellation_closes_half_open_socket(self, monkeypatch):
        adapter = _make_adapter()
        websocket = AsyncMock()
        websocket.state = 1
        connect = AsyncMock(return_value=websocket)
        monkeypatch.setitem(__import__("sys").modules, "websockets", MagicMock(connect=connect))
        adapter._authenticate_websocket = AsyncMock(side_effect=asyncio.CancelledError)

        with pytest.raises(asyncio.CancelledError):
            await adapter._ensure_typing_websocket()

        websocket.close.assert_awaited_once()
        assert adapter._typing_websocket is None

    @pytest.mark.asyncio
    async def test_closed_websockets15_connection_reconnects_before_send(self, monkeypatch):
        adapter = _make_adapter()
        stale = AsyncMock()
        stale.state = 3  # websockets.protocol.State.CLOSED
        adapter._typing_websocket = stale
        fresh = AsyncMock()
        fresh.state = 1  # OPEN
        connect = AsyncMock(return_value=fresh)
        monkeypatch.setitem(__import__("sys").modules, "websockets", MagicMock(connect=connect))
        adapter._authenticate_websocket = AsyncMock()

        result = await adapter._ensure_typing_websocket()

        assert result is fresh
        stale.close.assert_awaited_once()
        adapter._authenticate_websocket.assert_awaited_once_with(fresh)

    def test_build_typing_event_preserves_thread_scope_tags(self, monkeypatch):
        adapter = _make_adapter()
        build = MagicMock(return_value={"id": "thread-typing"})
        monkeypatch.setattr(
            _buzz_mod,
            "_load_nostr_auth",
            lambda: MagicMock(build_signed_event=build),
        )

        adapter._build_typing_event(
            CHANNEL,
            {"root_event_id": "root-id", "parent_event_id": "parent-id"},
        )

        assert build.call_args.kwargs["tags"] == [
            ["h", CHANNEL],
            ["e", "root-id", "", "root"],
            ["e", "parent-id", "", "reply"],
        ]


class TestInboundThreadScope:
    @pytest.mark.asyncio
    async def test_root_and_reply_tags_propagate_to_dispatched_source(self):
        adapter = _make_adapter()
        adapter.require_mention = False
        adapter._message_handler = AsyncMock(return_value="ok")
        adapter._channel_names[CHANNEL] = "Threaded channel"
        state = {"chat_type": "group", "last_ts": 0, "seen": OrderedDict()}
        event = _event("reply-id", content="thread reply", created_at=42)
        event["tags"] += [
            ["e", "root-id", "", "root"],
            ["e", "parent-id", "", "reply"],
        ]

        await adapter._handle_event(CHANNEL, state, event)

        dispatched = adapter._message_handler.await_args.args[0]
        assert dispatched.source.thread_id is None
        assert dispatched.source.root_event_id == "root-id"
        assert dispatched.source.parent_event_id == "parent-id"
        assert dispatched.metadata["root_event_id"] == "root-id"
        assert dispatched.metadata["parent_event_id"] == "parent-id"

    @pytest.mark.asyncio
    async def test_unmarked_reply_tag_becomes_thread_root(self):
        adapter = _make_adapter()
        adapter.require_mention = False
        adapter._message_handler = AsyncMock(return_value="ok")
        state = {"chat_type": "group", "last_ts": 0, "seen": OrderedDict()}
        event = _event("reply-id", content="legacy thread reply", created_at=42)
        event["tags"].append(["e", "legacy-root"])

        await adapter._handle_event(CHANNEL, state, event)

        dispatched = adapter._message_handler.await_args.args[0]
        assert dispatched.source.thread_id is None
        assert dispatched.source.root_event_id == "legacy-root"
        assert dispatched.source.parent_event_id is None


class TestTypingAcknowledgements:
    @pytest.mark.asyncio
    async def test_send_typing_ignores_unrelated_ack_before_matching_ack(self):
        adapter = _make_adapter()
        websocket = AsyncMock()
        websocket.recv.side_effect = [
            json.dumps(["OK", "other-event", True, ""]),
            json.dumps(["OK", "typing-event", True, ""]),
        ]
        adapter._ensure_typing_websocket = AsyncMock(return_value=websocket)
        adapter._build_typing_event = MagicMock(return_value={"id": "typing-event"})

        await adapter.send_typing(CHANNEL)

        assert websocket.recv.await_count == 2

    @pytest.mark.asyncio
    async def test_cancellation_while_waiting_for_ack_closes_socket(self):
        adapter = _make_adapter()
        websocket = AsyncMock()
        websocket.recv.side_effect = asyncio.CancelledError
        adapter._typing_websocket = websocket
        adapter._ensure_typing_websocket = AsyncMock(return_value=websocket)
        adapter._build_typing_event = MagicMock(return_value={"id": "typing-event"})

        with pytest.raises(asyncio.CancelledError):
            await adapter.send_typing(CHANNEL)

        websocket.close.assert_awaited_once()
        assert adapter._typing_websocket is None

    @pytest.mark.asyncio
    async def test_malformed_typing_ack_is_best_effort_and_closes_socket(self):
        adapter = _make_adapter()
        websocket = AsyncMock()
        websocket.recv.return_value = "not-json"
        adapter._typing_websocket = websocket
        adapter._ensure_typing_websocket = AsyncMock(return_value=websocket)
        adapter._build_typing_event = MagicMock(return_value={"id": "typing-event"})

        await adapter.send_typing(CHANNEL)

        websocket.close.assert_awaited_once()
        assert adapter._typing_websocket is None

    @pytest.mark.asyncio
    async def test_disconnect_closes_typing_socket(self):
        adapter = _make_adapter()
        websocket = AsyncMock()
        adapter._typing_websocket = websocket

        await adapter.disconnect()

        websocket.close.assert_awaited_once()
        assert adapter._typing_websocket is None

    @pytest.mark.asyncio
    async def test_disconnect_during_authentication_cannot_resurrect_socket(self, monkeypatch):
        adapter = _make_adapter()
        websocket = AsyncMock()
        websocket.state = 1
        connect = AsyncMock(return_value=websocket)
        monkeypatch.setitem(__import__("sys").modules, "websockets", MagicMock(connect=connect))
        auth_started = asyncio.Event()
        release_auth = asyncio.Event()

        async def authenticate(_websocket):
            auth_started.set()
            await release_auth.wait()

        adapter._authenticate_websocket = authenticate
        task = asyncio.create_task(adapter._ensure_typing_websocket())
        await auth_started.wait()

        await adapter.disconnect()
        release_auth.set()

        with pytest.raises(ConnectionError, match="superseded by disconnect"):
            await task
        websocket.close.assert_awaited_once()
        assert adapter._typing_websocket is None

    @pytest.mark.asyncio
    async def test_legacy_open_websocket_is_reused(self):
        adapter = _make_adapter()
        websocket = AsyncMock()
        del websocket.state
        websocket.closed = False
        adapter._typing_websocket = websocket

        result = await adapter._ensure_typing_websocket()

        assert result is websocket


# ── CLI error contract ────────────────────────────────────────────────────


class TestCliErrorContract:

    def test_parses_json_error(self):
        msg = _cli_error_message('{"error":"relay_error","message":"boom","retryable":false}', 2)
        assert "relay_error" in msg and "boom" in msg and "exit 2" in msg


# ── Seeding / high-water mark / de-dupe ───────────────────────────────────


class TestPollingDedupe:

    @pytest.fixture
    def adapter(self):
        a = _make_adapter()
        a._dispatched = []

        async def capture(**kwargs):
            a._dispatched.append(kwargs)

        a._dispatch_message = capture
        a._message_handler = AsyncMock()
        return a

    @pytest.mark.asyncio
    async def test_seed_sets_high_water_mark_without_dispatch(self, adapter):
        cli = _ScriptedCli()
        cli.script("messages", "get", [
            _event("e1", content="@Chip old history", created_at=100),
            _event("e2", content="@Chip newer history", created_at=200),
        ])
        adapter._run_cli = cli
        await adapter._seed_channel(CHANNEL, chat_type="group")

        state = adapter._channel_state[CHANNEL]
        assert state["last_ts"] == 200
        assert set(state["seen"]) == {"e1", "e2"}
        # Seeding must never replay history into the agent
        assert adapter._dispatched == []

    @pytest.mark.asyncio
    async def test_new_event_dispatched_once(self, adapter):
        cli = _ScriptedCli()
        cli.script("messages", "get", [_event("e1", content="@Chip hi", created_at=100)])
        adapter._run_cli = cli
        await adapter._seed_channel(CHANNEL, chat_type="group")

        # Poll 1: seeded event + a genuinely new mention
        cli.responses.clear()
        cli.script("messages", "get", [
            _event("e1", content="@Chip hi", created_at=100),
            _event("e2", content="hey @Chip, ping", created_at=150),
        ])
        await adapter._poll_channel(CHANNEL)
        assert [d["message_id"] for d in adapter._dispatched] == ["e2"]
        assert adapter._dispatched[0]["text"] == "hey @Chip, ping"
        assert adapter._channel_state[CHANNEL]["last_ts"] == 150

        # Poll 2: identical response — the seen-id set must de-dupe
        await adapter._poll_channel(CHANNEL)
        assert len(adapter._dispatched) == 1


# ── Mention gating / DMs / authorization ──────────────────────────────────


class TestMentionGating:

    @pytest.fixture
    def adapter(self):
        a = _make_adapter()
        a._dispatched = []

        async def capture(**kwargs):
            a._dispatched.append(kwargs)

        a._dispatch_message = capture
        a._message_handler = AsyncMock()
        a._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        return a

    async def _poll_with(self, adapter, *events):
        cli = _ScriptedCli()
        cli.script("messages", "get", list(events))
        adapter._run_cli = cli
        await adapter._poll_channel(CHANNEL)

    @pytest.mark.asyncio
    async def test_unaddressed_channel_message_ignored(self, adapter):
        await self._poll_with(adapter, _event("e1", content="just chatting", created_at=10))
        assert adapter._dispatched == []

    @pytest.mark.asyncio
    async def test_name_mention_dispatched(self, adapter):
        await self._poll_with(adapter, _event("e1", content="hey @Chip can you help?", created_at=10))
        assert len(adapter._dispatched) == 1


    @pytest.mark.asyncio
    async def test_allowlist_blocks_unauthorized(self, adapter):
        adapter._allowed_pubkeys = {"b" * 64}
        await self._poll_with(adapter, _event("e1", content="@Chip hello", created_at=10))
        assert adapter._dispatched == []


# ── DM classification via p-tags (issue #68871) ──────────────────────────
#
# `buzz dms list` returns [] on some hosted relays, so DM conversations leak
# in via `channels list` and get seeded chat_type="group".  The adapter must
# reclassify them from the Nostr tags of real traffic: DM messages are
# p-tagged to our own pubkey WITHOUT the text mentioning us, while channel
# messages only ever p-tag us when the text visibly @mentions us.


def _tagged_event(event_id, channel, *, content, pubkey=OTHER_PUBKEY,
                  created_at=1000, kind=9, p=None, reply_to=None):
    """Event with the tag shapes observed on a live relay (h/p/e tags)."""
    tags = [["h", channel]]
    if reply_to:
        tags.append(["e", reply_to, "", "reply"])
    if p:
        tags.append(["p", p])
    return {
        "id": event_id,
        "pubkey": pubkey,
        "content": content,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
    }


class TestDmClassification:

    @pytest.fixture
    def adapter(self):
        a = _make_adapter()
        a._dispatched = []

        async def capture(**kwargs):
            a._dispatched.append(kwargs)

        a._dispatch_message = capture
        a._message_handler = AsyncMock()
        # Metadata exactly as `channels list` returns it on the hosted relay.
        a._channel_meta = {
            DM_CHANNEL: {"channel_id": DM_CHANNEL, "name": "DM", "description": ""},
            CHANNEL: {
                "channel_id": CHANNEL,
                "name": "general",
                "description": "General conversation and community updates.",
            },
        }
        a._channel_names = {DM_CHANNEL: "DM", CHANNEL: "general"}
        # Both leaked in as group — the bug under test.
        a._channel_state[DM_CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        a._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        return a

    async def _poll_with(self, adapter, channel, *events):
        cli = _ScriptedCli()
        cli.script("messages", "get", list(events))
        adapter._run_cli = cli
        await adapter._poll_channel(channel)

    @pytest.mark.asyncio
    async def test_unmentioned_ptagged_dm_latches_and_dispatches(self, adapter):
        """The reported bug: a DM without an @mention must dispatch."""
        await self._poll_with(
            adapter, DM_CHANNEL,
            _tagged_event("e1", DM_CHANNEL, content="here's a test message", p=SELF_PUBKEY),
        )
        assert adapter._channel_state[DM_CHANNEL]["chat_type"] == "dm"
        assert [d["message_id"] for d in adapter._dispatched] == ["e1"]
        assert adapter._dispatched[0]["chat_type"] == "dm"


    @pytest.mark.asyncio
    async def test_general_reply_ptagging_self_stays_channel(self, adapter):
        """A #general reply to us p-tags our pubkey (observed live) — that
        must NOT reclassify the channel; mention gating still applies."""
        await self._poll_with(
            adapter, CHANNEL,
            _tagged_event("e1", CHANNEL, content="@chip what's up?",
                          p=SELF_PUBKEY, reply_to="root-event"),
        )
        assert adapter._channel_state[CHANNEL]["chat_type"] == "group"
        # It carried a mention, so it dispatches — but as a group message.
        assert [d["chat_type"] for d in adapter._dispatched] == ["group"]

        # And once the mention is absent, the channel gate drops the message
        # even though the earlier reply p-tagged us.
        await self._poll_with(
            adapter, CHANNEL,
            _tagged_event("e2", CHANNEL, content="thanks everyone", created_at=1001),
        )
        assert len(adapter._dispatched) == 1


    @pytest.mark.asyncio
    async def test_channel_like_metadata_blocks_latch_even_without_mention(self, adapter):
        """Second guard on its own: even a p-tagged, un-mentioned message
        cannot reclassify a conversation whose metadata says real channel."""
        adapter._channel_meta[CHANNEL]["description"] = ""
        adapter._channel_meta[CHANNEL]["name"] = "announcements"
        await self._poll_with(
            adapter, CHANNEL,
            _tagged_event("e1", CHANNEL, content="fyi everyone", p=SELF_PUBKEY),
        )
        assert adapter._channel_state[CHANNEL]["chat_type"] == "group"
        assert adapter._dispatched == []


    @pytest.mark.asyncio
    async def test_dm_shaped_channel_discovered_when_dms_list_empty(self):
        """Fallback discovery: with `dms list` broken (returns []), a
        DM-shaped `channels list` entry gets watched; real channels not
        already watched are left alone."""
        a = _make_adapter()
        cli = _ScriptedCli()
        cli.script("dms", "list", [])
        cli.script("channels", "list", [
            {"channel_id": DM_CHANNEL, "name": "DM", "description": "", "created_at": 1},
            {"channel_id": CHANNEL, "name": "general",
             "description": "General conversation and community updates.", "created_at": 2},
        ])
        a._run_cli = cli
        await a._discover_dms(seed=False)
        # Watched as group; the p-tag latch flips it on the first real DM.
        assert a._channel_state[DM_CHANNEL]["chat_type"] == "group"
        assert a._may_reclassify_as_dm(DM_CHANNEL) is True
        assert CHANNEL not in a._channel_state
        assert a._may_reclassify_as_dm(CHANNEL) is False

    @pytest.mark.asyncio
    async def test_membership_event_subscribes_new_shared_channel(self):
        """Joining a shared room is enough to make it live at runtime."""
        a = _make_adapter()
        cli = _ScriptedCli()
        cli.script("channels", "list", [
            {"channel_id": CHANNEL, "name": "exercise", "description": "Shared room"},
        ])
        cli.script("dms", "list", [])
        a._run_cli = cli
        websocket = AsyncMock()
        subscriptions = {"hermes-buzz-memberships": None}

        await a._handle_membership_event(
            websocket,
            subscriptions,
            {"created_at": 1000, "kind": 44100, "tags": [["p", SELF_PUBKEY]]},
        )

        assert a._channel_state[CHANNEL]["chat_type"] == "group"
        assert CHANNEL in subscriptions.values()
        request = json.loads(websocket.send.await_args.args[0])
        assert request[0] == "REQ"
        assert request[2]["#h"] == [CHANNEL]

    @pytest.mark.asyncio
    async def test_fixed_watchlist_does_not_expand_on_membership_event(self):
        """An explicit channels list remains an operator-controlled allowlist."""
        a = _make_adapter({"channels": [DM_CHANNEL]})
        cli = _ScriptedCli()
        cli.script("channels", "list", [
            {"channel_id": CHANNEL, "name": "exercise", "description": "Shared room"},
        ])
        cli.script("dms", "list", [])
        a._run_cli = cli
        websocket = AsyncMock()
        subscriptions = {"hermes-buzz-memberships": None}

        await a._handle_membership_event(
            websocket,
            subscriptions,
            {"created_at": 1000, "kind": 44100, "tags": [["p", SELF_PUBKEY]]},
        )

        assert CHANNEL not in a._channel_state
        websocket.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_poll_discovery_seeds_shared_room_without_replaying_history(self):
        """Polling fallback starts after existing room history, not at zero."""
        a = _make_adapter()
        a._dispatched = []
        cli = _ScriptedCli()
        cli.script("channels", "list", [
            {"channel_id": CHANNEL, "name": "exercise", "description": "Shared room"},
        ])
        cli.script("messages", "get", [
            _tagged_event("old-mention", CHANNEL, content="@chip old", created_at=900),
        ])
        a._run_cli = cli

        await a._discover_joined_channels(seed=False)

        assert a._channel_state[CHANNEL]["last_ts"] == 900
        assert "old-mention" in a._channel_state[CHANNEL]["seen"]
        assert a._dispatched == []


# ── Sending ───────────────────────────────────────────────────────────────


class TestBuzzAdapterSend:

    @pytest.mark.asyncio
    async def test_send_success_via_stdin(self):
        adapter = _make_adapter()
        adapter._channel_state[CHANNEL] = {"chat_type": "group", "last_ts": 0, "seen": {}}
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt123", "message": ""})
        adapter._run_cli = cli

        result = await adapter.send(CHANNEL, "hello **markdown**")
        assert result.success is True
        assert result.message_id == "evt123"

        args, stdin_text = cli.calls[0]
        assert args[:2] == ["messages", "send"]
        assert args[args.index("--channel") + 1] == CHANNEL
        # Content travels via stdin (--content -), never argv
        assert args[args.index("--content") + 1] == "-"
        assert stdin_text == "hello **markdown**"
        # Our own event id is marked seen for echo suppression
        assert "evt123" in adapter._channel_state[CHANNEL]["seen"]


    @pytest.mark.asyncio
    async def test_send_image_local_file_uses_file_flag(self, tmp_path):
        img = tmp_path / "shot.png"
        img.write_bytes(b"\x89PNG fake")
        adapter = _make_adapter()
        cli = _ScriptedCli()
        cli.script("messages", "send", {"accepted": True, "event_id": "evt126", "message": ""})
        adapter._run_cli = cli
        result = await adapter.send_image(CHANNEL, str(img), caption="screenshot")
        assert result.success is True
        args, _stdin = cli.calls[0]
        assert args[args.index("--file") + 1] == str(img)


# ── Lifecycle ─────────────────────────────────────────────────────────────


class TestBuzzAdapterLifecycle:


    @pytest.mark.asyncio
    async def test_disconnect_releases_scoped_lock(self, monkeypatch):
        """The identity lock taken in connect() must be released on disconnect."""
        import gateway.status as gateway_status

        released = []
        monkeypatch.setattr(
            gateway_status,
            "release_scoped_lock",
            lambda platform, key: released.append((platform, key)),
        )
        adapter = _make_adapter()
        adapter._lock_key = "wss://relay.example:" + SELF_PUBKEY
        await adapter.disconnect()
        assert released == [("buzz", "wss://relay.example:" + SELF_PUBKEY)]
        assert adapter._lock_key is None

    @pytest.mark.asyncio
    async def test_connect_fails_when_identity_lock_held(self, monkeypatch):
        """A second profile using the same relay+pubkey must fail fast."""
        import gateway.status as gateway_status

        monkeypatch.setattr(
            gateway_status, "acquire_scoped_lock", lambda platform, key: False
        )
        adapter = _make_adapter()
        adapter.cli_path = "/fake/buzz"
        monkeypatch.setattr(_buzz_mod, "_resolve_private_key", lambda extra=None: "nsec1test")
        cli = _ScriptedCli()
        cli.script(
            "users", "get",
            [{"pubkey": SELF_PUBKEY, "display_name": "Chip"}],
        )
        adapter._run_cli = cli
        assert await adapter.connect() is False
        assert adapter._lock_key is None


# ── Credentials / requirements ────────────────────────────────────────────


class TestCredentialResolution:

    def test_env_key_wins(self, monkeypatch):
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1fromenv")
        assert _resolve_private_key() == "nsec1fromenv"

    def test_credentials_file_fallback(self, monkeypatch, tmp_path):
        creds = tmp_path / "agent_credentials.json"
        creds.write_text(json.dumps({"nsec": "nsec1fromfile", "npub": "npub1x"}), encoding="utf-8")
        monkeypatch.setenv("BUZZ_CREDENTIALS_FILE", str(creds))
        assert _resolve_private_key() == "nsec1fromfile"


# ── Env enablement / registration / standalone send ──────────────────────


class TestEnvEnablement:

    def test_returns_none_when_unconfigured(self):
        assert _env_enablement() is None


class TestBuzzPluginRegistration:

    def test_register_platform_contract(self):
        from gateway.platform_registry import platform_registry

        platform_registry.unregister("buzz")
        ctx = MagicMock()
        register(ctx)
        ctx.register_platform.assert_called_once()
        kwargs = ctx.register_platform.call_args.kwargs
        assert kwargs["name"] == "buzz"
        assert kwargs["cron_deliver_env_var"] == "BUZZ_HOME_CHANNEL"
        assert kwargs["allowed_users_env"] == "BUZZ_ALLOWED_USERS"
        assert kwargs["allow_all_env"] == "BUZZ_ALLOW_ALL_USERS"
        assert callable(kwargs["standalone_sender_fn"])
        assert callable(kwargs["env_enablement_fn"])
        assert set(kwargs["required_env"]) == {"BUZZ_RELAY_URL", "BUZZ_PRIVATE_KEY"}


class TestStandaloneSend:

    @pytest.mark.asyncio
    async def test_standalone_send_success(self, monkeypatch, tmp_path):
        from gateway.config import PlatformConfig

        fake_cli = tmp_path / "buzz"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        monkeypatch.setenv("BUZZ_RELAY_URL", "https://r")
        monkeypatch.setenv("BUZZ_PRIVATE_KEY", "nsec1x")
        monkeypatch.setenv("BUZZ_CLI_PATH", str(fake_cli))

        captured = {}

        async def fake_exec(cli_path, args, *, relay_url, private_key, input_text=None, timeout=30.0):
            captured.update(cli_path=cli_path, args=args, relay_url=relay_url, input_text=input_text)
            return 0, json.dumps({"accepted": True, "event_id": "evt-cron", "message": ""}), ""

        monkeypatch.setattr(_buzz_mod, "_exec_buzz", fake_exec)

        result = await _standalone_send(PlatformConfig(enabled=True, extra={}), CHANNEL, "cron says hi")
        assert result == {"success": True, "message_id": "evt-cron"}
        assert captured["args"][:2] == ["messages", "send"]
        assert captured["input_text"] == "cron says hi"
        # The private key must never be part of argv
        assert all("nsec1x" not in str(a) for a in captured["args"])


