"""Canonical JSON and SHA-256. Every hash in HackProof goes through this module (section 10)."""

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """Serialise with sorted keys and no insignificant whitespace, keeping non-ASCII as is."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: str | bytes) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def hash_json(obj: Any) -> str:
    return sha256_hex(canonical_json(obj))
