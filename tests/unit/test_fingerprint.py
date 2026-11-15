"""Run hashes and winnowed fingerprints (M9 scanner, A8)."""

import random
import subprocess
import sys

from core.priorwork.fingerprint import B, M, pack_runs, run_hashes, symbol_id, unpack_runs, winnow


def test_a_run_hash_is_the_same_in_another_process() -> None:
    syms = ["N", "=", "S"] * 20
    code = (
        "from core.priorwork.fingerprint import run_hashes;print(run_hashes(['N','=','S']*20, 25))"
    )
    other = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert other == str(run_hashes(syms, 25))


def test_the_rolling_hash_equals_the_hash_computed_run_by_run() -> None:
    rng = random.Random(7)
    syms = [rng.choice(["N", "S", "0", "(", ")", "if", "=", ";"]) for _ in range(300)]
    k = 25
    ids = [symbol_id(s) for s in syms]
    direct = [
        sum(ids[i + j] * pow(B, k - 1 - j, M) for j in range(k)) % M
        for i in range(len(syms) - k + 1)
    ]
    assert run_hashes(syms, k) == direct


def test_fewer_than_k_symbols_have_no_run() -> None:
    assert run_hashes(["N"] * 24, 25) == []
    assert len(run_hashes(["N"] * 25, 25)) == 1
    assert len(run_hashes(["N"] * 30, 25)) == 6


def test_winnowing_keeps_the_rightmost_smallest_of_each_window() -> None:
    hashes = [77, 74, 42, 17, 98, 50, 17, 98, 8, 88, 67, 39, 77, 74, 42, 17, 98]
    assert winnow(hashes, 4) == [(3, 17), (6, 17), (8, 8), (11, 39), (15, 17)]


def test_fewer_runs_than_a_window_keep_their_smallest() -> None:
    assert winnow([5, 3, 9], 16) == [(1, 3)]
    assert winnow([4, 4], 16) == [(1, 4)]
    assert winnow([], 16) == []


def test_two_sequences_sharing_forty_symbols_share_a_fingerprint() -> None:
    rng = random.Random(2026)
    alphabet = ["N", "S", "0", "(", ")", "{", "}", ";", "=", "+", "if", "for", "return", "."]
    for _ in range(200):
        shared = [rng.choice(alphabet) for _ in range(40)]
        a = [rng.choice(alphabet) for _ in range(rng.randint(0, 60))] + shared
        a += [rng.choice(alphabet) for _ in range(rng.randint(0, 60))]
        b = [rng.choice(alphabet) for _ in range(rng.randint(0, 60))] + shared
        b += [rng.choice(alphabet) for _ in range(rng.randint(0, 60))]
        fa = {h for _, h in winnow(run_hashes(a, 25), 16)}
        fb = {h for _, h in winnow(run_hashes(b, 25), 16)}
        assert fa & fb


def test_runs_pack_sorted_and_distinct_and_unpack_to_the_same_set() -> None:
    hashes = [9, 2**61 - 2, 9, 0, 12345]
    packed = pack_runs(hashes)
    assert len(packed) == 8 * 4
    assert packed[:8] == (0).to_bytes(8, "big")
    assert unpack_runs(packed) == frozenset(hashes)
    assert unpack_runs(b"") == frozenset()
