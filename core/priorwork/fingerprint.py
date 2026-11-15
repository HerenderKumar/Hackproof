"""Run hashes over token symbols, and winnowed fingerprints (the M9 plan's A8).

A run is `k` consecutive symbols. Its hash is a polynomial over the symbols' IDs modulo the
Mersenne prime 2**61 - 1, computed as a rolling hash; a symbol's ID comes from BLAKE2b, so the
hashes are the same in every process (Python's own `hash` is salted per process). Winnowing
keeps the rightmost smallest hash of every window of `w` runs, once per position: two texts
sharing at least `w + k - 1` symbols in a row share a fingerprint.
"""

from collections.abc import Iterable, Sequence
from functools import lru_cache
from hashlib import blake2b

HASH_VERSION = "1"
M = 2**61 - 1
B = 1_000_003


@lru_cache(maxsize=65536)
def symbol_id(sym: str) -> int:
    return int.from_bytes(blake2b(sym.encode(), digest_size=8).digest(), "big") % M


def run_hashes(syms: Sequence[str], k: int) -> list[int]:
    """The hash of every run of `k` symbols: `max(0, len(syms) - k + 1)` of them."""
    n = len(syms)
    if n < k:
        return []
    ids = [symbol_id(s) for s in syms]
    top = pow(B, k - 1, M)
    value = 0
    for i in range(k):
        value = (value * B + ids[i]) % M
    out = [value]
    for i in range(k, n):
        value = ((value - ids[i - k] * top) * B + ids[i]) % M
        out.append(value)
    return out


def winnow(hashes: Sequence[int], w: int) -> list[tuple[int, int]]:
    """(position, hash) of the rightmost smallest hash of each window, ascending positions."""
    n = len(hashes)
    if n == 0:
        return []
    if n < w:
        smallest = min(hashes)
        last = max(i for i, h in enumerate(hashes) if h == smallest)
        return [(last, smallest)]
    kept: list[tuple[int, int]] = []
    chosen = -1
    for start in range(n - w + 1):
        end = start + w
        if chosen < start:
            # The previous choice left the window: find the rightmost smallest afresh.
            chosen = start
            for i in range(start + 1, end):
                if hashes[i] <= hashes[chosen]:
                    chosen = i
        elif hashes[end - 1] <= hashes[chosen]:
            chosen = end - 1
        if not kept or kept[-1][0] != chosen:
            kept.append((chosen, hashes[chosen]))
    return kept


def pack_runs(hashes: Iterable[int]) -> bytes:
    """Sorted distinct hashes, 8 bytes big-endian each."""
    return b"".join(h.to_bytes(8, "big") for h in sorted(set(hashes)))


def unpack_runs(data: bytes) -> frozenset[int]:
    return frozenset(int.from_bytes(data[i : i + 8], "big") for i in range(0, len(data), 8))
