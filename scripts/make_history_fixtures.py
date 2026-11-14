"""Build the fixture repositories under `LOCAL_REMOTES_DIR` (section 20.3).

    python -m scripts.make_history_fixtures [--kickoff 2026-11-14T09:00:00+00:00] [--dest DIR]

Builds H1 and H2 from their `timeline.json`, and one repository per app variant, all dated
relative to the kickoff. An existing repository of the same name is replaced.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from core.settings import Settings
from scripts.fixtures import FixtureError, load_fixtures, load_history_fixtures
from scripts.git_fixtures import build_app_remote, build_history_remote

DEFAULT_KICKOFF = "2026-11-14T09:00:00+00:00"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.make_history_fixtures")
    parser.add_argument("--kickoff", default=DEFAULT_KICKOFF, type=datetime.fromisoformat)
    parser.add_argument("--dest", type=Path, default=None)
    args = parser.parse_args(argv)
    kickoff: datetime = args.kickoff
    if kickoff.utcoffset() is None:
        parser.error("--kickoff needs a timezone, for example +00:00")
    dest: Path = args.dest or Settings().local_remotes_dir

    try:
        histories = load_history_fixtures()
        fixtures = load_fixtures()
    except FixtureError as exc:
        print(f"fixture error: {exc}", file=sys.stderr)
        return 1

    for history in histories:
        print(build_history_remote(history, dest, kickoff))
    for fixture in fixtures:
        for variant in fixture.variants:
            print(build_app_remote(variant, dest, kickoff))
    return 0


if __name__ == "__main__":
    sys.exit(main())
