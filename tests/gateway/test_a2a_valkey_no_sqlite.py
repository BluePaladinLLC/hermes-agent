from __future__ import annotations

import importlib
import sys

import pytest

from gateway.a2a_valkey import A2AInboxError, A2AValkeyInboxStore
from gateway.a2a_receiver_tick import run_receiver_tick
from gateway.a2a_actionable_receiver import A2AActionResult


class FakeStreamClient:
    def __init__(self):
        self.hashes = {}
        self.streams = {}
        self.acked = []
        self.groups = set()
        self._next_id = 1

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.hashes:
            return False
        self.hashes[key] = value
        return True

    def get(self, key):
        return self.hashes.get(key)

    def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    def hgetall(self, key):
        value = self.hashes.get(key, {})
        return dict(value) if isinstance(value, dict) else {}

    def xadd(self, stream, fields):
        stream_id = f"{self._next_id}-0"
        self._next_id += 1
        self.streams.setdefault(stream, []).append((stream_id, dict(fields)))
        return stream_id

    def xgroup_create(self, stream, group, id="0", mkstream=True):
        key = (stream, group)
        if key in self.groups:
            raise Exception("BUSYGROUP Consumer Group name already exists")
        self.groups.add(key)
        self.streams.setdefault(stream, [])

    def xreadgroup(self, group, consumer, streams, count=1, block=0):
        out = []
        for stream, start in streams.items():
            entries = self.streams.get(stream, [])[:count]
            if entries:
                out.append((stream, entries))
        return out

    def xack(self, stream, group, stream_id):
        self.acked.append((stream, group, stream_id))
        return 1


def test_a2a_valkey_module_imports_without_sqlite_fallback(monkeypatch):
    sys.modules.pop("gateway.a2a_inbox", None)
    module = importlib.import_module("gateway.a2a_valkey")

    assert hasattr(module, "A2AValkeyInboxStore")
    assert issubclass(module.A2AInboxError, ValueError)


def test_valkey_receiver_roundtrip_does_not_create_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    client = FakeStreamClient()
    store = A2AValkeyInboxStore(client=client)

    created = store.enqueue(
        sender="thalamus",
        targets=["pons"],
        message_type="work_request",
        topic_id="topic-no-sqlite",
        subject="No SQLite fallback",
        body="prove Valkey receiver path works without sqlite inbox",
    )
    assert created["status"] == "queued"

    summary = run_receiver_tick(
        store=store,
        target="pons",
        consumer="pons-test",
        handlers={"work_request": lambda _msg: A2AActionResult(body="ok", evidence_links=["test:no-sqlite"])},
        max_messages=1,
    )

    assert summary["processed"] == 1
    original = summary["results"][0]["original"]
    assert original["status"] == "completed"
    assert original["result"] == "ok"
    assert not (tmp_path / "a2a_inbox.db").exists()


def test_valkey_storage_errors_no_longer_require_sqlite_module():
    with pytest.raises(A2AInboxError, match="client is required"):
        A2AValkeyInboxStore(client=None)
