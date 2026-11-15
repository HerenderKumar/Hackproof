"""The prior-work comparison (M9 scanner, A8): a final submission against device baselines."""

import random
from collections.abc import Collection, Mapping

from core.priorwork.compare import compare, eligible, excerpt
from core.priorwork.fingerprint import run_hashes
from core.priorwork.lexer import tokenize
from core.priorwork.model import KEY_MARKER, MatchSettings, Report, match_key
from tests.priorwork_fakes import MemoryBaseline, block, source

S = MatchSettings()
EXT = [".js", ".py", ".ts"]


def run(
    files: dict[str, str],
    baselines: list[MemoryBaseline],
    *,
    starter: str = "",
    common: Mapping[int, int] | None = None,
    min_teams: int = 3,
) -> Report:
    starter_runs = set(run_hashes([t.sym for t in tokenize(starter, ".js")], S.k))

    def teams(hashes: Collection[int]) -> Mapping[int, int]:
        return {h: n for h, n in (common or {}).items() if h in hashes}

    return compare(
        [source(p, t) for p, t in files.items()],
        baselines,
        starter_runs=starter_runs,
        common_teams=teams,
        min_teams=min_teams,
        settings=S,
    )


def test_the_same_code_reformatted_is_found_unchanged() -> None:
    old = block(1)
    new = "// moved here\n" + old.replace("\n", "\n\n").replace("  ", "      ")
    report = run({"src/a.js": new}, [MemoryBaseline("b1", {"old/a.js": old})])
    assert [m.kind for m in report.matches] == ["unchanged"]
    assert report.totals.matched == report.totals.eligible > 0
    assert report.totals.percent == 100.0


def test_renamed_names_are_found_as_edited() -> None:
    old = block(2)
    new = old.replace("total", "grand").replace("price", "cost")
    report = run({"a.js": new}, [MemoryBaseline("b1", {"x.js": old})])
    assert [m.kind for m in report.matches] == ["edited"]
    assert report.totals.edited == report.totals.matched > 0


def test_code_moved_into_a_larger_file_is_found() -> None:
    old = block(3)
    new = block(10) + old + block(11)
    report = run({"big.js": new}, [MemoryBaseline("b1", {"small.js": old})])
    [m] = report.matches
    assert m.kind == "unchanged" and m.old_path == "small.js"
    assert m.final_start_line > 10 and m.old_start_line == 1
    assert 0 < report.totals.percent < 60  # type: ignore[operator]


def _syms_text(n: int, seed: int) -> str:
    rng = random.Random(seed)
    return " ".join(rng.choice(["a", "+", "(", ")", "1", ";", "if", "{", "}"]) for _ in range(n))


def test_a_stretch_of_24_tokens_is_not_matched_and_one_of_25_is() -> None:
    # A long common stretch makes the old file a candidate; the short one is then measured.
    anchor = _syms_text(60, 99)

    def with_shared(n: int) -> Report:
        shared = _syms_text(n, 5)
        old = anchor + " @ @ @ " + shared + " # # #"
        new = anchor + " ! ! ! " + shared + " ~ ~ ~"
        return run({"n.js": new}, [MemoryBaseline("b1", {"o.js": old})])

    assert with_shared(24).totals.matched == 60
    assert with_shared(25).totals.matched == 85


def test_a_token_matched_by_two_old_files_counts_once_and_names_the_other() -> None:
    old = block(4)
    report = run(
        {"a.js": old},
        [
            MemoryBaseline("b1", {"one.js": old + "\n// one"}),
            MemoryBaseline("b2", {"two.js": "// two\n" + old}),
        ],
    )
    [m] = report.matches
    assert m.also_in == 1
    assert report.totals.matched == report.totals.eligible


def test_an_identical_file_counts_whole_and_needs_k_tokens() -> None:
    old = block(5)
    report = run({"same.js": old}, [MemoryBaseline("b1", {"keep/same.js": old})])
    [m] = report.matches
    assert m.kind == "identical_file" and report.totals.identical == report.totals.total
    tiny = "a = 1;"
    small = run({"t.js": tiny}, [MemoryBaseline("b1", {"t.js": tiny})])
    assert small.matches == () and small.totals.matched == 0


def test_starter_code_leaves_both_sides() -> None:
    starter = block(6)
    new = starter + block(7)
    report = run({"a.js": new}, [MemoryBaseline("b1", {"o.js": starter})], starter=starter)
    t = report.totals
    assert t.excluded_starter > 0 and t.eligible == t.total - t.excluded_starter - t.excluded_common
    assert t.matched == 0


def _prints(text: str) -> set[int]:
    from core.priorwork.fingerprint import winnow

    return {h for _, h in winnow(run_hashes([t.sym for t in tokenize(text, ".js")], S.k), S.w)}


def test_code_three_teams_hold_leaves_both_sides_whole() -> None:
    shared = block(8)
    new = shared + "\nfinal = 1;\n"
    report = run(
        {"a.js": new},
        [MemoryBaseline("b1", {"o.js": shared})],
        common=dict.fromkeys(_prints(shared), 3),
    )
    assert report.matches == ()
    assert report.totals.matched == 0 and report.totals.excluded_common > 0
    assert report.totals.percent == 0.0


def test_a_whole_file_three_teams_hold_is_excluded_to_its_ends() -> None:
    shared = block(8)
    report = run(
        {"same.js": shared, "new.js": block(16)},
        [MemoryBaseline("b1", {"o.js": shared})],
        common=dict.fromkeys(_prints(shared), 3),
    )
    assert report.matches == ()
    [same] = [f for f in report.files if f.path == "same.js"]
    assert same.excluded == same.total and same.matched == 0
    assert report.totals.percent == 0.0


def test_code_two_teams_hold_is_not_common() -> None:
    shared = block(8)
    report = run(
        {"a.js": shared + "\nfinal = 1;\n"},
        [MemoryBaseline("b1", {"o.js": shared})],
        common=dict.fromkeys(_prints(shared), 2),
    )
    assert report.totals.excluded_common == 0 and report.totals.matched > 0


def test_a_late_baseline_never_adds_to_the_percent() -> None:
    old = block(9)
    report = run({"a.js": old + block(12)}, [MemoryBaseline("b1", {"o.js": old}, late=True)])
    t = report.totals
    assert t.matched == 0 and t.percent == 0.0
    assert t.late_matched > 0 and t.late_percent is not None and t.late_percent > 0
    assert all(m.late for m in report.matches)


def test_a_token_matched_on_time_is_not_counted_again_as_late() -> None:
    old = block(13)
    report = run(
        {"a.js": old + block(14)},
        [MemoryBaseline("b1", {"o.js": old}), MemoryBaseline("b2", {"p.js": old}, late=True)],
    )
    assert report.totals.late_matched == 0 and report.totals.matched > 0
    assert [m.late for m in report.matches] == [False]


def test_a_blanked_key_equals_any_string() -> None:
    old = block(15).replace("{\n", '{\n  key = "HACKPROOF_BLANKED";\n', 1)
    new = block(15).replace("{\n", '{\n  key = "sk-live-123";\n', 1)
    assert KEY_MARKER in old
    report = run({"a.js": new}, [MemoryBaseline("b1", {"o.js": old})])
    assert [m.kind for m in report.matches] == ["unchanged"]


def test_percent_is_absent_without_eligible_code() -> None:
    # The caller passes only eligible files; one holding nothing but a comment has no tokens.
    report = run({"a.js": "// only a comment\n"}, [MemoryBaseline("b1", {"o.js": block(1)})])
    assert report.totals.eligible == 0 and report.totals.percent is None
    assert report.totals.late_percent is None


def test_files_off_the_list_too_large_or_not_counted_are_not_eligible() -> None:
    limit = 1024 * 1024
    assert not eligible("src/a.rs", 10, EXT, limit)
    assert not eligible("src/a.js", limit + 1, EXT, limit)
    assert not eligible("node_modules/a.js", 10, EXT, limit)
    assert not eligible("app.min.js", 10, EXT, limit)
    assert eligible("SRC/App.JS", 10, EXT, limit)
    assert eligible("src/a.js", limit, EXT, limit)


def test_excerpts_hold_the_lines_and_five_around() -> None:
    text = "\n".join(f"line {n}" for n in range(1, 31))
    part = excerpt(text, 10, 12, 5)
    assert part.first_line == 5
    assert part.text.splitlines() == [f"line {n}" for n in range(5, 18)]
    start = excerpt(text, 2, 3, 5)
    assert start.first_line == 1 and start.text.splitlines()[0] == "line 1"


def test_an_identical_file_stores_its_first_200_lines() -> None:
    old = "".join(block(n, 3) for n in range(100))  # about 500 lines
    report = run({"a.js": old}, [MemoryBaseline("b1", {"o.js": old})])
    [m] = report.matches
    assert m.final_excerpt.first_line == m.old_excerpt.first_line == 1
    assert len(m.final_excerpt.text.splitlines()) == 200
    assert m.old_excerpt.text == m.final_excerpt.text


def test_every_counted_token_belongs_to_one_match() -> None:
    a, b, c = block(20), block(21), block(22)
    report = run(
        {"x.js": a + block(30) + b, "y.js": c + a},
        [
            MemoryBaseline("b1", {"1.js": a, "2.js": b + c}),
            MemoryBaseline("b2", {"3.js": c}, late=True),
            MemoryBaseline("b3", {"4.js": block(30)}, late=True),
        ],
    )
    on_time = sum(m.tokens for m in report.matches if not m.late)
    late = sum(m.tokens for m in report.matches if m.late)
    assert on_time == report.totals.matched > 0
    assert late == report.totals.late_matched > 0
    assert sum(f.matched for f in report.files) == report.totals.matched


def test_the_report_is_the_same_whatever_order_its_inputs_come_in() -> None:
    a, b = block(40), block(41)
    files = {"x.js": a + b, "y.js": b + a}
    baselines = [MemoryBaseline("b1", {"1.js": a}), MemoryBaseline("b2", {"2.js": b})]
    first = run(files, baselines)
    second = run(dict(reversed(files.items())), list(reversed(baselines)))
    assert first == second


def test_a_match_key_is_stable_and_differs_between_matches() -> None:
    a, b = block(50), block(51)
    report = run({"x.js": a + b}, [MemoryBaseline("b1", {"1.js": a, "2.js": b})])
    keys = [match_key(m) for m in report.matches]
    assert len(keys) == 2 and len(set(keys)) == 2 and all(len(k) == 32 for k in keys)
    again = run({"x.js": a + b}, [MemoryBaseline("b1", {"1.js": a, "2.js": b})])
    assert [match_key(m) for m in again.matches] == keys
