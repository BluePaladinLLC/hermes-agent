"""Minimal NIP-44 v2 encryption for owner-scoped Buzz telemetry."""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import secrets

try:
    from .nostr_auth import FIELD_ORDER, _point_multiply, decode_private_key
except ImportError:  # Loaded by the platform plugin's standalone test loader.
    import importlib.util
    from pathlib import Path

    _auth_path = Path(__file__).with_name("nostr_auth.py")
    _auth_spec = importlib.util.spec_from_file_location(
        "plugin_adapter_buzz_nip44_nostr_auth", _auth_path
    )
    assert _auth_spec is not None and _auth_spec.loader is not None
    _auth = importlib.util.module_from_spec(_auth_spec)
    _auth_spec.loader.exec_module(_auth)
    FIELD_ORDER = _auth.FIELD_ORDER
    _point_multiply = _auth._point_multiply
    decode_private_key = _auth.decode_private_key

_MASK32 = 0xFFFF_FFFF


def _rotl32(value: int, shift: int) -> int:
    return ((value << shift) & _MASK32) | (value >> (32 - shift))


def _quarter_round(state: list[int], a: int, b: int, c: int, d: int) -> None:
    state[a] = (state[a] + state[b]) & _MASK32
    state[d] = _rotl32(state[d] ^ state[a], 16)
    state[c] = (state[c] + state[d]) & _MASK32
    state[b] = _rotl32(state[b] ^ state[c], 12)
    state[a] = (state[a] + state[b]) & _MASK32
    state[d] = _rotl32(state[d] ^ state[a], 8)
    state[c] = (state[c] + state[d]) & _MASK32
    state[b] = _rotl32(state[b] ^ state[c], 7)


def _chacha20(key: bytes, nonce: bytes, data: bytes) -> bytes:
    if len(key) != 32 or len(nonce) != 12:
        raise ValueError("ChaCha20 requires a 32-byte key and 12-byte nonce")
    constants = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574]
    key_words = [int.from_bytes(key[i : i + 4], "little") for i in range(0, 32, 4)]
    nonce_words = [int.from_bytes(nonce[i : i + 4], "little") for i in range(0, 12, 4)]
    output = bytearray()
    for counter, offset in enumerate(range(0, len(data), 64)):
        initial = constants + key_words + [counter & _MASK32] + nonce_words
        working = initial.copy()
        for _ in range(10):
            _quarter_round(working, 0, 4, 8, 12)
            _quarter_round(working, 1, 5, 9, 13)
            _quarter_round(working, 2, 6, 10, 14)
            _quarter_round(working, 3, 7, 11, 15)
            _quarter_round(working, 0, 5, 10, 15)
            _quarter_round(working, 1, 6, 11, 12)
            _quarter_round(working, 2, 7, 8, 13)
            _quarter_round(working, 3, 4, 9, 14)
        block = b"".join(
            ((working[i] + initial[i]) & _MASK32).to_bytes(4, "little")
            for i in range(16)
        )
        chunk = data[offset : offset + 64]
        output.extend(left ^ right for left, right in zip(chunk, block))
    return bytes(output)

_MAX_PLAINTEXT_BYTES = 65_535


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    output = bytearray()
    previous = b""
    counter = 1
    while len(output) < length:
        previous = hmac.new(prk, previous + info + bytes([counter]), hashlib.sha256).digest()
        output.extend(previous)
        counter += 1
    return bytes(output[:length])


def _lift_x(pubkey_hex: str) -> tuple[int, int]:
    try:
        x = int(pubkey_hex, 16)
    except ValueError as exc:
        raise ValueError("owner pubkey must be 64 lowercase hex characters") from exc
    if len(pubkey_hex) != 64 or pubkey_hex.lower() != pubkey_hex or x >= FIELD_ORDER:
        raise ValueError("owner pubkey must be 64 lowercase hex characters")
    y_squared = (pow(x, 3, FIELD_ORDER) + 7) % FIELD_ORDER
    y = pow(y_squared, (FIELD_ORDER + 1) // 4, FIELD_ORDER)
    if pow(y, 2, FIELD_ORDER) != y_squared:
        raise ValueError("owner pubkey is not a secp256k1 x-coordinate")
    if y & 1:
        y = FIELD_ORDER - y
    return x, y


def conversation_key(private_key: str, owner_pubkey: str) -> bytes:
    shared = _point_multiply(decode_private_key(private_key), _lift_x(owner_pubkey))
    if shared is None:  # pragma: no cover - valid points/scalars cannot reach infinity
        raise ValueError("invalid NIP-44 shared secret")
    return _hkdf_extract(b"nip44-v2", shared[0].to_bytes(32, "big"))


def _padded_length(length: int) -> int:
    if length <= 32:
        return 32
    next_power = 2 ** (math.floor(math.log2(length - 1)) + 1)
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * (math.floor((length - 1) / chunk) + 1)


def encrypt_v2(
    plaintext: str,
    private_key: str,
    owner_pubkey: str,
    *,
    nonce: bytes | None = None,
) -> str:
    raw = plaintext.encode()
    if not 1 <= len(raw) <= _MAX_PLAINTEXT_BYTES:
        raise ValueError("NIP-44 plaintext must be between 1 and 65535 bytes")
    nonce = secrets.token_bytes(32) if nonce is None else nonce
    if len(nonce) != 32:
        raise ValueError("NIP-44 nonce must be 32 bytes")

    padded = len(raw).to_bytes(2, "big") + raw
    padded += bytes(_padded_length(len(raw)) - len(raw))
    keys = _hkdf_expand(conversation_key(private_key, owner_pubkey), nonce, 76)
    ciphertext = _chacha20(keys[:32], keys[32:44], padded)
    mac = hmac.new(keys[44:76], nonce + ciphertext, hashlib.sha256).digest()
    return base64.b64encode(b"\x02" + nonce + ciphertext + mac).decode()
