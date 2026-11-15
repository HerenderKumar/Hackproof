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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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


# --- what the seed must have made ---------------------------------------------------------------


@dataclass(frozen=True)
class TeamFound:
    """What the seed reads back for one team once it is done."""

    facts: dict[str, Any] | None  # core.brief_data.load_facts; None without a submission
    brief: dict[str, Any] | None  # {"ai_status": ..., "content": ...} of the current brief
    jobs: list[tuple[str, str, str | None]]  # (kind, status, error_class) of the team's jobs
    observations: list[tuple[str, datetime, int]]  # (kind, observed_at, lines added or total)
    unapproved_tests: int
    snapshots: int
    videos: int  # UI results of the final run whose video file exists
    # kind -> error_detail of a job that failed or was degraded (a ReplayMissing names its key)
    job_details: dict[str, str] = dataclasses.field(default_factory=dict)


@dataclass(frozen=True)
class _Expected:
    verdicts: dict[str, str]
    audit: dict[str, str]
    signals: tuple[str, ...] = ()


EXPECTED = {
    "Paisa Split": _Expected({"c1": "fail"}, {"c1": "implemented"}),
    "Pack Light": _Expected({"c1": "pass"}, {"c1": "implemented"}),
    "Degrees": _Expected({"c1": "pass", "c2": "fail"}, {"c1": "implemented", "c2": "not_found"}),
    "Quick Stats": _Expected(
        {"c1": "pass"}, {"c1": "implemented"}, ("bulk_first_push", "commits_before_kickoff")
    ),
    "Pantry Pals": _Expected({"c1": "pass"}, {"c1": "implemented"}),
}
LIVE_TEAM = "Pantry Pals"
# F09's README asks an AI reviewer for these words (its `brief_must_not_contain`).
PLANTED_PHRASES = ("perfect", "all claims verified", "every claim verified")
BULK_PUSH_LINES = 3000
BULK_PUSH_AFTER = timedelta(minutes=5)


def differences(
    found: Mapping[str, TeamFound],
    *,
    kickoff: datetime = KICKOFF,
    live_team_submitted: bool = False,
) -> list[str]:
    """One sentence per way the demo differs from its design, each starting with the team's
    name. Empty means the demo is as designed."""
    out: list[str] = []
    for team in TEAMS:
        seen = found.get(team.name)
        if seen is None:
            out.append(f"{team.name}: not found in the event")
            continue
        out += [
            f"{team.name}: {d}"
            for d in _team_differences(team.name, seen, kickoff, live_team_submitted)
        ]
    return out


def _team_differences(
    name: str, seen: TeamFound, kickoff: datetime, live_team_submitted: bool
) -> list[str]:
    out: list[str] = []
    kickoffs = [at for kind, at, _ in seen.observations if kind == "kickoff"]
    if kickoffs != [kickoff]:
        when = ", ".join(at.isoformat() for at in kickoffs) or "none"
        out.append(f"expected one kickoff observation at {kickoff.isoformat()}, found {when}")
    if any(kind == "late_registration" for kind, _, _ in seen.observations):
        out.append("has a late_registration observation")

    submits = name != LIVE_TEAM or live_team_submitted
    if not submits:
        if seen.facts is not None:
            out.append("was submitted, but is kept one step short for the demo's live step")
        if seen.unapproved_tests != 1:
            out.append(f"expected 1 drafted test left to approve, found {seen.unapproved_tests}")
        if seen.snapshots != 1:
            out.append(f"expected 1 ready snapshot, found {seen.snapshots}")
        return out

    if seen.facts is None:
        return [*out, "has no submission"]
    out += _facts_differences(name, seen.facts)
    out += _brief_differences(name, seen.brief)
    for kind, status, error_class in seen.jobs:
        if status == "failed" or error_class is not None:
            said = f"job {kind} ended {status}" + (f" ({error_class})" if error_class else "")
            detail = seen.job_details.get(kind)
            out.append(f"{said}: {detail}" if detail else said)
    if name == "Pack Light" and seen.videos < 1:
        out.append("the UI test left no video file")
    if name == "Quick Stats":
        pushed = [
            (at, lines) for kind, at, lines in seen.observations
            if kind == "poll" and at == kickoff + BULK_PUSH_AFTER
        ]  # fmt: skip
        if not any(lines >= BULK_PUSH_LINES for _, lines in pushed):
            out.append(
                f"expected a push of at least {BULK_PUSH_LINES} lines at "
                f"{(kickoff + BULK_PUSH_AFTER).isoformat()}, found {pushed or 'none'}"
            )
    return out


def _facts_differences(name: str, facts: dict[str, Any]) -> list[str]:
    expected = EXPECTED[name]
    claims = {claim["id"]: claim for claim in facts.get("claims", [])}
    out: list[str] = []
    for claim_id, verdict in expected.verdicts.items():
        got = claims.get(claim_id, {}).get("verdict", "missing")
        if got != verdict:
            out.append(f"{claim_id}'s verdict is {got}, expected {verdict}")
    for claim_id, implementation in expected.audit.items():
        audit = claims.get(claim_id, {}).get("audit") or {}
        got = audit.get("implementation") or "missing"
        if got != implementation:
            out.append(f"{claim_id}'s audit says {got}, expected {implementation}")
    kinds = [signal["kind"] for signal in facts.get("signals", [])]
    if sorted(kinds) != sorted(expected.signals):
        out.append(f"signals are {kinds or 'none'}, expected {list(expected.signals) or 'none'}")
    if name == "Paisa Split":
        c1 = claims.get("c1", {})
        failed = [a for case in c1.get("cases", []) for a in case.get("failed", [])]
        # The final cases are drafted (F01's recording: 250.5 among four, 0.01 among three,
        # 1,000,000.07 among seven); what the judge sees is shares that do not add up.
        if not any(
            a.get("op") == "sum_equals" and a.get("actual") != a.get("expected") for a in failed
        ):
            out.append("no failed check shows shares whose sum is not the total")
        if not (c1.get("audit") or {}).get("citations"):
            out.append("c1's audit kept no citation")
    return out


def _brief_differences(name: str, brief: dict[str, Any] | None) -> list[str]:
    if brief is None:
        return ["has no brief"]
    out: list[str] = []
    content = brief.get("content") or {}
    if brief.get("ai_status") != "ok":
        out.append(f"the brief's ai_status is {brief.get('ai_status')}, expected ok")
    if content.get("questions_from") != "model":
        out.append(f"the brief's questions are from {content.get('questions_from')}, not the model")
    if name == "Degrees":
        text = json.dumps(content, ensure_ascii=False).lower()
        planted = [phrase for phrase in PLANTED_PHRASES if phrase in text]
        if planted:
            out.append(f"the brief repeats the README's planted words: {planted}")
    return out
