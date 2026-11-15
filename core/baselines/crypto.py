"""Baseline code at rest: AES-256-GCM under BASELINE_KEY (M9, the plan's A4).

Each sealed piece is a fresh 12-byte nonce followed by the ciphertext and its tag. The
associated data names where the piece belongs (`<baseline_id>:part:<n>`), so a piece copied to
another place does not decrypt there.
"""

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.errors import InfraError

NONCE_BYTES = 12
TAG_BYTES = 16


class BaselineCorrupt(InfraError):
    """Stored baseline bytes did not decrypt: another key, another place, or damage."""


def encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def decrypt(key: bytes, data: bytes, aad: bytes) -> bytes:
    if len(data) < NONCE_BYTES + TAG_BYTES:
        raise BaselineCorrupt("stored baseline bytes are too short to be encrypted data")
    try:
        return AESGCM(key).decrypt(data[:NONCE_BYTES], data[NONCE_BYTES:], aad)
    except InvalidTag as exc:
        raise BaselineCorrupt(
            "stored baseline bytes did not decrypt: wrong key, moved, or damaged"
        ) from exc
