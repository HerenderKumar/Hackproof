"""`make fixtures`: the fixture suite (section 20.1).

At M3 this lists what the suite will run, with each variant's expected results. The stages that
run a variant through the pipeline and compare it with `expected.json` join as the pipeline is
built (M4 onwards).
"""

import argparse
import sys

from scripts.fixtures import FixtureError, load_fixtures, load_history_fixtures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.run_fixtures")
    parser.add_argument("--list", action="store_true", help="list the fixtures and stop")
    parser.parse_args(argv)

    try:
        fixtures = load_fixtures()
        histories = load_history_fixtures()
    except FixtureError as exc:
        print(f"fixture error: {exc}", file=sys.stderr)
        return 1

    variants = [(f, v) for f in fixtures for v in f.variants]
    width = max(len(v.remote_name) for _, v in variants)
    for fixture, variant in variants:
        expected = variant.expected
        verdicts = " ".join(f"{c}:{r}" for c, r in sorted(expected["verdicts"].items()))
        print(
            f"{variant.remote_name:<{width}}  {fixture.stack:<10}  {fixture.build_method:<10}"
            f"  build={expected['build']} startup={expected['startup']}  {verdicts}"
        )
    for history in histories:
        print(
            f"{history.slug:<{width}}  {'history':<10}  {f'{len(history.pushes)} pushes':<10}"
            f"  signals: {', '.join(history.expected_signals)}"
        )
    print(f"{len(variants)} app variants, {len(histories)} history fixtures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
