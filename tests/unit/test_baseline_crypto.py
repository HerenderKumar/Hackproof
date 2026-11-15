"""Baseline code at rest (M9): AES-256-GCM with a label bound to each piece."""

import pytest

from core.baselines.crypto import BaselineCorrupt, decrypt, encrypt

KEY, OTHER = bytes(range(32)), bytes(range(1, 33))


def test_what_is_encrypted_decrypts_only_with_the_same_key_and_label() -> None:
    sealed = encrypt(KEY, b"def f(): pass", b"b1:part:1")
    assert decrypt(KEY, sealed, b"b1:part:1") == b"def f(): pass"
    with pytest.raises(BaselineCorrupt):
        decrypt(OTHER, sealed, b"b1:part:1")
    with pytest.raises(BaselineCorrupt):
        decrypt(KEY, sealed, b"b1:part:2")


def test_two_encryptions_of_the_same_bytes_differ() -> None:
    a, b = encrypt(KEY, b"same", b"x"), encrypt(KEY, b"same", b"x")
    assert a != b and a[:12] != b[:12]
    assert b"same" not in a


def test_a_flipped_byte_is_detected() -> None:
    sealed = bytearray(encrypt(KEY, b"some code", b"x"))
    sealed[15] ^= 1
    with pytest.raises(BaselineCorrupt):
        decrypt(KEY, bytes(sealed), b"x")
    with pytest.raises(BaselineCorrupt):
        decrypt(KEY, b"short", b"x")
