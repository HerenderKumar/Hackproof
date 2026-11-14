import hashlib

from core.hashing import canonical_json, hash_json, sha256_hex


def test_canonical_json_sorts_keys_and_drops_whitespace() -> None:
    assert canonical_json({"b": 1, "a": [1, 2, {"d": None, "c": True}]}) == (
        '{"a":[1,2,{"c":true,"d":null}],"b":1}'
    )


def test_canonical_json_is_independent_of_key_order() -> None:
    assert canonical_json({"x": 1, "y": 2}) == canonical_json({"y": 2, "x": 1})


def test_canonical_json_keeps_non_ascii_characters() -> None:
    assert canonical_json({"amount": "₹100"}) == '{"amount":"₹100"}'


def test_sha256_hex_of_text_hashes_its_utf8_bytes() -> None:
    assert sha256_hex("₹") == hashlib.sha256("₹".encode()).hexdigest()


def test_sha256_hex_accepts_bytes() -> None:
    assert sha256_hex(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_hash_json_is_the_sha256_of_the_canonical_form() -> None:
    obj = {"kind": "api", "steps": [{"id": "s1"}]}
    assert hash_json(obj) == sha256_hex(canonical_json(obj))


def test_hash_json_changes_when_a_value_changes() -> None:
    assert hash_json({"total": 100}) != hash_json({"total": 101})
