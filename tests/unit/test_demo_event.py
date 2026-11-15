"""The demo event's data (M12): its five teams, their repositories, the check of what it made."""

import dataclasses
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.signals import signals
from scripts.demo_event import (
    KICKOFF,
    TEAMS,
    TeamFound,
    build_repositories,
    differences,
    instants,
    move_heads,
    quick_stats_history,
)
from scripts.fixtures import load_fixtures, load_history_fixtures
from tests.history_replay import replay

Sessions = async_sessionmaker[AsyncSession]

h1 = next(h for h in load_history_fixtures() if h.id == "H1")
f10 = next(f for f in load_fixtures() if f.id == "F10")


def _has_main(repo: Path) -> bool:
    done = subprocess.run(
        ["git", "--git-dir", str(repo), "rev-parse", "--verify", "--quiet", "refs/heads/main"],
        capture_output=True,
        text=True,
    )
    return done.returncode == 0


def _heads(dest: Path) -> dict[str, str]:
    heads = {}
    for repo in sorted(dest.glob("*.git")):
        if _has_main(repo):
            done = subprocess.run(
                ["git", "--git-dir", str(repo), "rev-parse", "refs/heads/main"],
                capture_output=True,
                text=True,
                check=True,
            )
            heads[repo.name.removesuffix(".git")] = done.stdout.strip()
    return heads


def test_the_demo_has_the_designs_five_teams() -> None:
    assert [(t.name, t.repo, t.fixture, t.variant, t.submits) for t in TEAMS] == [
        ("Paisa Split", "paisa-split", "F01", "broken", True),
        ("Pack Light", "pack-light", "F05", "good", True),
        ("Degrees", "degrees", "F09", "good", True),
        ("Quick Stats", "quick-stats", "F10", "good", True),
        ("Pantry Pals", "pantry-pals", "F02", "good", False),
    ]
    for team in TEAMS:
        assert "broken" not in f"{team.name} {team.repo}".lower()
        assert "good" not in f"{team.name} {team.repo}".lower()


def test_quick_stats_is_h1s_history_around_f10s_app() -> None:
    made = quick_stats_history(h1, f10.variant("good"))
    assert [(c.name, c.parent, c.authored, c.committed) for c in made.commits] == [
        (c.name, c.parent, c.authored, c.committed) for c in h1.commits
    ]
    assert made.pushes == h1.pushes and made.slug == "quick-stats"
    kickoff, backdated, bulk = made.commits
    assert set(kickoff.files) == {"README.md"}
    assert kickoff.files["README.md"] == (f10.variant("good").path / "README.md").read_bytes()
    assert set(backdated.files) == {"NOTES.md"}
    assert {"server.js", "package.json", "package-lock.json", "src/engine.js"} <= set(bulk.files)
    assert "README.md" not in bulk.files
    engine = bulk.files["src/engine.js"]
    assert engine is not None and engine.count(b"\n") == 3000
    assert bulk.message == "Add the stats service"


def test_the_repositories_start_with_nothing_pushed_and_never_change(tmp_path: Path) -> None:
    first = build_repositories(tmp_path / "a")
    assert first == build_repositories(tmp_path / "b")  # same SHAs on every build
    assert set(first) == {t.repo for t in TEAMS}
    for repo in first:
        assert not _has_main(tmp_path / "a" / f"{repo}.git")
    assert instants(first) == [
        KICKOFF,
        KICKOFF + timedelta(minutes=5),
        KICKOFF + timedelta(minutes=60),
    ]


def test_heads_move_with_the_clock(tmp_path: Path) -> None:
    pushes = build_repositories(tmp_path)
    move_heads(tmp_path, pushes, KICKOFF - timedelta(hours=1))
    assert _heads(tmp_path) == {}
    move_heads(tmp_path, pushes, KICKOFF)
    assert _heads(tmp_path) == {"quick-stats": pushes["quick-stats"][0][1]}
    move_heads(tmp_path, pushes, KICKOFF + timedelta(minutes=60))
    assert _heads(tmp_path) == {repo: log[-1][1] for repo, log in pushes.items()}


async def test_quick_stats_shows_the_bulk_push_and_the_early_commit(
    sessions: Sessions, tmp_path: Path
) -> None:
    event, team, rows = await replay(
        sessions, quick_stats_history(h1, f10.variant("good")), tmp_path
    )
    found = signals(
        kickoff_at=event.kickoff_at,
        bulk_push_lines=event.bulk_push_lines,
        bulk_push_window_minutes=event.bulk_push_window_minutes,
        repo_created_at=team.repo_created_at,
        observations=rows,
    )
    assert [s.name for s in found] == ["bulk_first_push", "commits_before_kickoff"]
    assert found[0].detail["minutes_after_kickoff"] == 5
    assert found[0].detail["lines_added"] >= 3000


# --- the check of what the seed made --------------------------------------------------------


def _facts(
    verdicts: dict[str, str],
    audit: dict[str, str],
    found_signals: tuple[str, ...] = (),
    failed_actual: object = None,
) -> dict[str, Any]:
    claims = []
    for claim_id, verdict in verdicts.items():
        failed = (
            []
            if failed_actual is None
            else [
                {"op": "sum_equals", "passed": False, "actual": failed_actual, "expected": "250.5"}
            ]
        )
        claims.append(
            {
                "id": claim_id,
                "verdict": verdict,
                "cases": [{"verdict": verdict, "failed": failed}],
                "audit": {
                    "implementation": audit[claim_id],
                    "explanation": "Read the code.",
                    "citations": [{"path": "src/split.js", "start_line": 1, "end_line": 9}],
                },
            }
        )
    return {"claims": claims, "signals": [{"kind": kind} for kind in found_signals]}


def _brief(summary: str = "The split loses a paisa.") -> dict[str, Any]:
    return {
        "ai_status": "ok",
        "content": {"summary": summary, "questions_from": "model", "demo_questions": []},
    }


def _jobs() -> list[tuple[str, str, str | None]]:
    kinds = ("kickoff_snapshot", "poll_repo", "prepare_snapshot", "draft_specs")
    return [(kind, "done", None) for kind in (*kinds, "final_run", "audit", "brief")]


def _team(
    facts: dict[str, Any] | None,
    *,
    observations: list[tuple[str, datetime, int]] | None = None,
    videos: int = 0,
    brief: dict[str, Any] | None = None,
) -> TeamFound:
    return TeamFound(
        facts=facts,
        brief=brief if brief is not None else (_brief() if facts is not None else None),
        jobs=_jobs() if facts is not None else _jobs()[:4],
        observations=observations
        or [("kickoff", KICKOFF, 0), ("poll", KICKOFF + timedelta(minutes=60), 120)],
        unapproved_tests=0 if facts is not None else 1,
        snapshots=1,
        videos=videos,
    )


def as_designed() -> dict[str, TeamFound]:
    big_push = [("kickoff", KICKOFF, 3), ("poll", KICKOFF + timedelta(minutes=5), 3120)]
    return {
        "Paisa Split": _team(_facts({"c1": "fail"}, {"c1": "implemented"}, failed_actual="250.52")),
        "Pack Light": _team(_facts({"c1": "pass"}, {"c1": "implemented"}), videos=1),
        "Degrees": _team(
            _facts({"c1": "pass", "c2": "fail"}, {"c1": "implemented", "c2": "not_found"})
        ),
        "Quick Stats": _team(
            _facts(
                {"c1": "pass"},
                {"c1": "implemented"},
                ("bulk_first_push", "commits_before_kickoff"),
            ),
            observations=big_push,
        ),
        "Pantry Pals": _team(None),
    }


def _with(found: dict[str, TeamFound], name: str, **changes: Any) -> dict[str, TeamFound]:
    return {**found, name: dataclasses.replace(found[name], **changes)}


def _about(differences_found: list[str], name: str) -> list[str]:
    return [d for d in differences_found if d.startswith(f"{name}:")]


def test_the_demo_as_designed_has_no_differences() -> None:
    assert differences(as_designed()) == []


def test_a_wrong_verdict_is_reported() -> None:
    found = as_designed()
    wrong = _facts({"c1": "pass"}, {"c1": "implemented"}, failed_actual="250.52")
    reported = differences(_with(found, "Paisa Split", facts=wrong))
    assert len(reported) == 1
    assert reported[0].startswith("Paisa Split:")
    assert all(word in reported[0] for word in ("c1", "fail", "pass"))


def test_a_missing_and_an_extra_signal_are_reported() -> None:
    found = as_designed()
    only_early = _facts({"c1": "pass"}, {"c1": "implemented"}, ("commits_before_kickoff",))
    found = _with(found, "Quick Stats", facts=only_early)
    late = _facts({"c1": "pass"}, {"c1": "implemented"}, ("late_registration",))
    found = _with(found, "Pack Light", facts=late)
    reported = differences(found)
    assert any("bulk_first_push" in d for d in _about(reported, "Quick Stats"))
    assert any("late_registration" in d for d in _about(reported, "Pack Light"))
    assert len(reported) == 2


def test_observations_at_the_wrong_time_are_reported() -> None:
    found = as_designed()
    late_kickoff = [("kickoff", KICKOFF + timedelta(seconds=1), 0)]
    found = _with(found, "Degrees", observations=late_kickoff)
    slow_push = [("kickoff", KICKOFF, 3), ("poll", KICKOFF + timedelta(minutes=6), 3120)]
    found = _with(found, "Quick Stats", observations=slow_push)
    reported = differences(found)
    assert _about(reported, "Degrees") and _about(reported, "Quick Stats")
    assert len(reported) == 2


def test_a_degraded_job_is_reported() -> None:
    found = as_designed()
    degraded = [*found["Degrees"].jobs, ("audit", "done", "validation")]
    reported = differences(_with(found, "Degrees", jobs=degraded))
    assert len(_about(reported, "Degrees")) == 1 and "audit" in reported[0]
    assert len(reported) == 1


def test_a_brief_from_templates_is_reported() -> None:
    found = as_designed()
    templated = {"ai_status": "ok", "content": {"summary": None, "questions_from": "template"}}
    assert len(_about(differences(_with(found, "Degrees", brief=templated)), "Degrees")) == 1
    no_brief = dataclasses.replace(found["Pack Light"], brief=None)
    assert len(_about(differences({**found, "Pack Light": no_brief}), "Pack Light")) == 1


def test_a_planted_phrase_in_the_brief_is_reported() -> None:
    found = as_designed()
    planted = _brief("Perfect score: the app does all it says.")
    reported = differences(_with(found, "Degrees", brief=planted))
    assert len(reported) == 1 and reported[0].startswith("Degrees:")
    assert "perfect" in reported[0].lower()


def test_the_live_team_must_be_left_unsubmitted() -> None:
    found = as_designed()
    submitted = _with(
        found,
        "Pantry Pals",
        facts=_facts({"c1": "pass"}, {"c1": "implemented"}),
        brief=_brief(),
        jobs=_jobs(),
        unapproved_tests=0,
    )
    assert _about(differences(submitted), "Pantry Pals")
    assert differences(submitted, live_team_submitted=True) == []


def test_a_degraded_job_says_why() -> None:
    found = as_designed()
    degraded = [*found["Degrees"].jobs, ("brief", "done", "validation")]
    why = {"brief": "ReplayMissing: no recorded reply abc123"}
    reported = differences(_with(found, "Degrees", jobs=degraded, job_details=why))
    assert reported == [
        "Degrees: job brief ended done (validation): ReplayMissing: no recorded reply abc123"
    ]


def test_a_split_whose_sum_check_did_not_fail_is_reported() -> None:
    found = as_designed()
    adds_up = _facts({"c1": "fail"}, {"c1": "implemented"})
    reported = differences(_with(found, "Paisa Split", facts=adds_up))
    assert len(reported) == 1 and reported[0].startswith("Paisa Split:")
    assert "sum" in reported[0]
