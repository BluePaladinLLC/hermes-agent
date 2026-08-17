import json
import os
import subprocess

import pytest

from plugins.platforms.buzz.nip44 import encrypt_v2
from plugins.platforms.buzz.nostr_auth import public_key_hex


AGENT_KEY = "1".zfill(64)
OWNER_KEY = "2".zfill(64)
NONCE = bytes(range(32))


def test_nip44_v2_matches_nostr_tools_and_decrypts():
    owner_pubkey = public_key_hex(OWNER_KEY)
    payload = json.dumps({"kind": "turn_started", "seq": 1}, separators=(",", ":"))

    ciphertext = encrypt_v2(payload, AGENT_KEY, owner_pubkey, nonce=NONCE)

    script = """
const { nip44 } = require('nostr-tools');
const privateKey = Uint8Array.from(Buffer.from(process.argv[1], 'hex'));
const agentPubkey = process.argv[2];
const ciphertext = process.argv[3];
const conversationKey = nip44.getConversationKey(privateKey, agentPubkey);
process.stdout.write(nip44.v2.decrypt(ciphertext, conversationKey));
"""
    nostr_tools_cwd = os.environ.get("NOSTR_TOOLS_CWD")
    if not nostr_tools_cwd:
        pytest.skip("set NOSTR_TOOLS_CWD to a checkout containing nostr-tools")
    result = subprocess.run(
        [
            "node",
            "-e",
            script,
            OWNER_KEY,
            public_key_hex(AGENT_KEY),
            ciphertext,
        ],
        cwd=nostr_tools_cwd,
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout == payload
    assert 132 <= len(ciphertext) <= 87_472


def test_nip44_v2_rejects_oversized_plaintext():
    try:
        encrypt_v2("x" * 65_536, AGENT_KEY, public_key_hex(OWNER_KEY), nonce=NONCE)
    except ValueError as error:
        assert "65535" in str(error)
    else:  # pragma: no cover
        raise AssertionError("oversized plaintext should fail")
