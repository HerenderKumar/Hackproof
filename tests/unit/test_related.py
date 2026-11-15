"""The "possibly related" list beside the percentage (M9 plan, Stage 5, Task 21)."""

import math
import re
from collections.abc import Sequence
from pathlib import Path

from core.priorwork.compare import compare
from core.priorwork.related import pick_old_files, pieces, related, with_related
from scripts.baselines import load_baseline_fixtures
from scripts.fixtures import FIXTURES_DIR
from scripts.run_baselines import run_fixture
from tests.priorwork_fakes import (
    DEFAULT_SETTINGS,
    FAKE_MIN_SCORE,
    FakeEmbedder,
    MemoryBaseline,
    block,
    fake_embedder,
    nobody,
    source,
)

PLAIN = {
    "starter_runs": set(),
    "common_teams": nobody,
    "min_teams": 3,
    "settings": DEFAULT_SETTINGS,
}


def body(lines: int, word: str = "x") -> list[str]:
    return [f"    {word}{n} = {n}" for n in range(lines)]


def test_pieces_start_at_definitions_and_are_cut_at_80_lines() -> None:
    lines = ["import os", "", "def a():", *body(5), "class B:", *body(100)]
    assert pieces("\n".join(lines), ".py") == [(3, 8), (9, 88), (89, 109)]


def test_a_piece_with_fewer_than_three_lines_of_code_is_dropped() -> None:
    lines = ["def a():", "    pass", "", "def b():", *body(3)]
    assert pieces("\n".join(lines), ".py") == [(4, 7)]


def test_a_file_with_no_definition_is_cut_every_40_lines() -> None:
    text = "\n".join(f"x{n} = {n}" for n in range(100))
    assert pieces(text, ".py") == [(1, 40), (41, 80), (81, 100)]


def test_old_files_are_picked_by_the_rare_names_they_share() -> None:
    common = {f"lib/common{n}.js": f"function c{n}() {{ return value + {n}; }}" for n in range(8)}
    device = MemoryBaseline(
        "b1",
        {
            **common,
            "geo/rare.js": "function haversine(radius) { return value * radius; }",
            "geo/half.js": "function haversine() { return 1; }",
            "geo/rare.py": "def haversine(radius):\n    return radius\n",
        },
    )
    final = source("src/d.js", "function haversine(radius) { return value * radius; }")
    picked = pick_old_files(final, [device], limit=2)
    assert [ref.path for ref in picked] == ["geo/rare.js", "geo/half.js"]
    assert all(not ref.path.endswith(".py") for ref in pick_old_files(final, [device]))


def test_old_files_sharing_no_name_are_not_picked() -> None:
    device = MemoryBaseline("b1", {"a.js": "function alpha() { return beta; }"})
    assert pick_old_files(source("b.js", "function gamma() { return delta; }"), [device]) == []


REWRITE_OLD = """function haversineDistance(start, end) {
  const deltaLat = toRadians(end.lat - start.lat);
  const deltaLon = toRadians(end.lon - start.lon);
  return earthRadius * Math.atan2(Math.sqrt(deltaLat), Math.sqrt(deltaLon));
}
"""
REWRITE_NEW = """export function haversineDistance(from, to) {
  const [lat, lon] = [to.lat - from.lat, to.lon - from.lon].map(toRadians);
  const both = Math.sqrt(lat) / Math.sqrt(lon);
  return earthRadius * Math.atan2(both, 1);
}
"""


def test_a_pair_above_the_score_is_listed_and_never_counted() -> None:
    device = MemoryBaseline("b1", {"old/geo.js": REWRITE_OLD})
    files = [source("src/geo.js", REWRITE_NEW)]
    report = compare(files, [device], **PLAIN)
    assert report.matches == ()
    both = with_related(report, files, [device], FakeEmbedder(), 0.5)
    assert both.totals == report.totals and both.files == report.files
    (m,) = both.matches
    assert (m.kind, m.tokens, m.final_path, m.old_path) == (
        "possibly_related",
        0,
        "src/geo.js",
        "old/geo.js",
    )
    assert m.score is not None and m.score >= 0.5
    assert (m.final_start_line, m.final_end_line, m.old_start_line, m.old_end_line) == (1, 5, 1, 5)
    assert m.final_excerpt.text == REWRITE_NEW.rstrip("\n")
    assert m.old_excerpt.text == REWRITE_OLD.rstrip("\n")


def test_a_pair_below_the_score_is_not_listed() -> None:
    device = MemoryBaseline("b1", {"old/geo.js": REWRITE_OLD})
    files = [source("src/geo.js", REWRITE_NEW)]
    report = compare(files, [device], **PLAIN)
    assert related(report, files, [device], FakeEmbedder(), 0.999) == ()


def test_only_files_less_than_half_matched_are_looked_at() -> None:
    copied = block(1, 30)
    device = MemoryBaseline("b1", {"old/copied.js": copied, "old/geo.js": REWRITE_OLD})
    files = [source("src/copied.js", copied + "\n"), source("src/geo.js", REWRITE_NEW)]
    report = compare(files, [device], **PLAIN)
    assert {f.path: f.matched > 0 for f in report.files} == {
        "src/copied.js": True,
        "src/geo.js": False,
    }
    embedder = FakeEmbedder()
    related(report, files, [device], embedder, 0.5)
    assert REWRITE_NEW.rstrip("\n") in embedder.seen
    assert not any("mix" in text for text in embedder.seen)  # copied.js was not cut or embedded


class Scripted:
    """A vector per text: pieces naming `fN` lean away from [1, 0] by N thousandths."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for text in texts:
            found = re.search(r"function f(\d+)", text)
            out.append([1.0, int(found.group(1)) / 1000] if found else [1.0, 0.0])
        return out


def test_at_most_fifty_pairs_are_kept_best_first() -> None:
    defs = [f"function f{n}(shared) {{\n  shared.a = {n};\n  return shared;\n}}" for n in range(60)]
    old = "function base(shared) {\n  shared.a = 0;\n  return shared;\n}"
    device = MemoryBaseline("b1", {"old/base.js": old})
    files = [source("src/many.js", "\n".join(defs))]
    report = compare(files, [device], **PLAIN)
    found = related(report, files, [device], Scripted(), 0.9)
    assert len(found) == 50
    scores = [m.score for m in found]
    assert scores == sorted(scores, reverse=True)
    assert found[0].final_start_line == 1 and math.isclose(found[0].score or 0, 1.0)
    assert max(m.final_start_line for m in found) == 1 + 4 * 49


def test_a_piece_inside_a_match_is_not_listed_again() -> None:
    copied = block(2, 30)
    device = MemoryBaseline("b1", {"old/copied.js": copied})
    new = "\n".join(
        f"function g{n}(total) {{\n  total.b = {n};\n  return total;\n}}" for n in range(60)
    )
    files = [source("src/mixed.js", new + "\n" + copied)]
    report = compare(files, [device], **PLAIN)
    (share,) = report.files
    assert 0 < 2 * share.matched < share.total - share.excluded
    found = related(report, files, [device], FakeEmbedder(), 0.0)
    (m,) = report.matches
    assert found
    assert all(
        r.final_start_line > m.final_end_line or r.final_end_line < m.final_start_line
        for r in found
    )


# B6-rewritten: the device's algorithm written again with other local names, another structure
# and other comments, so no 25-token stretch survives: percent 0.0, one possibly_related match.
def test_b6_is_possibly_related_and_not_in_the_percentage(tmp_path: Path) -> None:
    (fixture,) = [f for f in load_baseline_fixtures(FIXTURES_DIR) if f.id == "B6-rewritten"]
    plain, _ = run_fixture(fixture, tmp_path / "plain")
    report, _ = run_fixture(
        fixture,
        tmp_path / "related",
        embedder_factory=fake_embedder,
        related_min_score=FAKE_MIN_SCORE,
    )
    assert plain.matches == () and report.totals == plain.totals
    assert report.totals.percent == 0.0
    kinds = {(m.kind, m.final_path) for m in report.matches}
    assert kinds == {("possibly_related", "src/distance.js")}
