"""The demo event of section 22 (M12): its names, its five teams and their repositories.

Everything here is data or plain git: the seed (`scripts/seed_demo.py`) does the API calls and
the jobs. The names below are inside recorded model requests (the audit's first message carries
the event's name, the kickoff's date and the team's name), so changing one means recording the
demo's replies again.

Every repository is built from the fixtures, dated from the fixed kickoff so its commit SHAs never
change, and starts with nothing pushed: `main` is removed and `move_heads` puts it back at the
head pushed by a given time, as `tests/history_replay.py` does for one history.
"""

import dataclasses
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from scripts.fixtures import (
    HistoryCommit,
    HistoryFixture,
    Variant,
    load_fixtures,
    load_history_fixtures,
)
from scripts.git_fixtures import PUSH_LOG, build_app_remote, build_history_remote

KICKOFF = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
EVENT_NAME, JUDGE_NAME = "HackProof demo", "Demo judge"
POLICY = "Declare starter code, earlier work and AI assistance."
RUBRIC = [
    {"id": "impact", "name": "Impact", "description": "Does it matter?", "max": 10},
    {"id": "craft", "name": "Craft", "description": "Is it well built?", "max": 10},
]

QUICK_STATS_NOTES = (
    b"# Notes\n\n- Mean: the sum over the count.\n"
    b"- Median: sort, take the middle; for an even count, the mean of the two middle values.\n"
)


@dataclass(frozen=True)
class DemoTeam:
    name: str
    repo: str
    fixture: str  # an app fixture's ID
    variant: str
    history: str | None  # a history fixture's ID: the app is pushed along its timeline
    submits: bool  # False: left one step short of submitting, for the live step of the demo


TEAMS = (
    DemoTeam("Paisa Split", "paisa-split", "F01", "broken", None, True),
    DemoTeam("Pack Light", "pack-light", "F05", "good", None, True),
    DemoTeam("Degrees", "degrees", "F09", "good", None, True),
    DemoTeam("Quick Stats", "quick-stats", "F10", "good", "H1", True),
    DemoTeam("Pantry Pals", "pantry-pals", "F02", "good", None, False),
)

Pushes = dict[str, list[tuple[datetime, str]]]  # repository name -> (time, head SHA), oldest first


def quick_stats_history(h1: HistoryFixture, app: Variant) -> HistoryFixture:
    """H1's timeline (README at kickoff, a backdated note, one big push five minutes later)
    carrying a working app: the bulk push holds the app's files and H1's 3,000 lines."""
    kickoff, backdated, bulk = h1.commits
    app_files = {file.relative_to(app.path).as_posix(): file.read_bytes() for file in app.files()}
    readme = app_files.pop("README.md")
    commits = [
        dataclasses.replace(kickoff, files={"README.md": readme}),
        dataclasses.replace(backdated, files={"NOTES.md": QUICK_STATS_NOTES}),
        HistoryCommit(
            name=bulk.name,
            parent=bulk.parent,
            authored=bulk.authored,
            committed=bulk.committed,
            message="Add the stats service",
            files={**app_files, "src/engine.js": bulk.files["src/engine.js"]},
        ),
    ]
    return dataclasses.replace(h1, slug="quick-stats", title="Quick Stats", commits=commits)


def build_repositories(dest: Path, kickoff: datetime = KICKOFF) -> Pushes:
    """The five repositories under `dest`, with nothing pushed yet, and what each pushes when."""
    fixtures = {f.id: f for f in load_fixtures()}
    histories = {h.id: h for h in load_history_fixtures()}
    pushes: Pushes = {}
    for team in TEAMS:
        variant = fixtures[team.fixture].variant(team.variant)
        if team.history is None:
            repo = build_app_remote(variant, dest, kickoff, name=team.repo)
        else:
            history = quick_stats_history(histories[team.history], variant)
            repo = build_history_remote(history, dest, kickoff, name=team.repo)
        log = json.loads((repo / PUSH_LOG).read_text())
        pushes[team.repo] = [(datetime.fromisoformat(p["at"]), p["head"]) for p in log]
        _git(repo, "update-ref", "-d", "refs/heads/main")
    return pushes


def move_heads(dest: Path, pushes: Pushes, at: datetime) -> None:
    """Point each repository's `main` at its last head pushed at or before `at`, or remove it."""
    for repo_name, log in pushes.items():
        repo = dest / f"{repo_name}.git"
        pushed = [head for when, head in log if when <= at]
        if pushed:
            _git(repo, "update-ref", "refs/heads/main", pushed[-1])
        else:
            _git(repo, "update-ref", "-d", "refs/heads/main", check=False)


def instants(pushes: Pushes, kickoff: datetime = KICKOFF) -> list[datetime]:
    """The kickoff and every push time, sorted and distinct: when the seed's clock stops."""
    return sorted({kickoff, *(when for log in pushes.values() for when, _ in log)})


def _git(repo: Path, *args: str, check: bool = True) -> None:
    subprocess.run(["git", "--git-dir", str(repo), *args], check=check, capture_output=True)
