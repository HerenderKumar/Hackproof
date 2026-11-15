"""Organiser set-up: the event, team import, judges and invitation links (section 5.1)."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from api.csv_import import MAX_BYTES
from tests.api_helpers import EVENT, PUBLIC_BASE_URL, Api
from tests.baseline_flow import add_device, scan
from tests.factories import RUBRIC, T0
from tests.fakes import FakeClock
from tests.helpers import Sessions
from tests.team_flow import started

# --- events ----------------------------------------------------------------------------------


async def test_an_event_is_created_with_the_briefs_defaults(api: Api) -> None:
    organiser = await api.organiser()

    response = await organiser.post("/api/events", json=EVENT)

    assert response.status_code == 201
    event = response.json()
    assert event["name"] == "Winter Hack"
    assert event["rubric"] == RUBRIC
    assert (event["bulk_push_lines"], event["bulk_push_window_minutes"]) == (2000, 60)
    assert (event["poll_interval_seconds"], event["rubric_version"]) == (300, 1)
    assert event["scores_locked"] is False
    assert (await organiser.get(f"/api/events/{event['id']}")).json() == event


async def test_the_deadline_must_follow_the_kickoff(api: Api) -> None:
    organiser = await api.organiser()
    response = await organiser.post(
        "/api/events", json={**EVENT, "deadline_at": EVENT["kickoff_at"]}
    )
    assert response.status_code == 422


async def test_event_times_need_a_time_zone(api: Api) -> None:
    organiser = await api.organiser()
    response = await organiser.post(
        "/api/events", json={**EVENT, "kickoff_at": "2026-11-14T09:00:00"}
    )
    assert response.status_code == 422


async def test_a_rubric_needs_criteria_with_distinct_ids(api: Api) -> None:
    organiser = await api.organiser()
    twice = [RUBRIC[0], {**RUBRIC[1], "id": RUBRIC[0]["id"]}]

    for rubric in ([], twice, [{**RUBRIC[0], "max": 0}]):
        response = await organiser.post("/api/events", json={**EVENT, "rubric": rubric})
        assert response.status_code == 422, rubric


async def test_unknown_event_fields_are_refused(api: Api) -> None:
    organiser = await api.organiser()
    response = await organiser.post("/api/events", json={**EVENT, "scores_locked": True})
    assert response.status_code == 422


async def test_an_unknown_event_is_not_found(api: Api) -> None:
    organiser = await api.organiser()
    response = await organiser.get(f"/api/events/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "event_not_found"


async def test_an_event_can_be_edited(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    later = (T0 + timedelta(hours=36)).isoformat()

    response = await organiser.patch(
        f"/api/events/{event['id']}", json={"name": "Spring Hack", "deadline_at": later}
    )

    assert response.status_code == 200
    edited = response.json()
    assert edited["name"] == "Spring Hack"
    assert edited["deadline_at"] != event["deadline_at"]
    assert (edited["kickoff_at"], edited["rubric_version"]) == (event["kickoff_at"], 1)


async def test_changing_the_rubric_starts_a_new_rubric_version(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    new_rubric = [*RUBRIC, {"id": "demo", "name": "Demo", "description": "", "max": 5}]

    same = await organiser.patch(f"/api/events/{event['id']}", json={"rubric": RUBRIC})
    changed = await organiser.patch(f"/api/events/{event['id']}", json={"rubric": new_rubric})

    assert same.json()["rubric_version"] == 1
    assert (changed.json()["rubric_version"], changed.json()["rubric"]) == (2, new_rubric)


async def test_an_edit_cannot_put_the_deadline_before_the_kickoff(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    earlier = (T0 - timedelta(hours=1)).isoformat()

    response = await organiser.patch(f"/api/events/{event['id']}", json={"deadline_at": earlier})

    assert response.status_code == 422
    assert (await organiser.get(f"/api/events/{event['id']}")).json() == event


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)


async def test_an_event_states_its_claims_lock(api: Api) -> None:
    organiser = await api.organiser()

    event = await api.event(organiser)
    explicit = await api.event(
        organiser, name="Spring Hack", claims_lock_at=(T0 + timedelta(hours=20)).isoformat()
    )
    patched = await organiser.patch(
        f"/api/events/{event['id']}",
        json={"claims_lock_at": (T0 + timedelta(hours=12)).isoformat()},
    )

    assert _at(event["claims_lock_at"]) == T0 + timedelta(hours=16)  # two thirds of 24 hours
    assert _at(explicit["claims_lock_at"]) == T0 + timedelta(hours=20)
    assert patched.status_code == 200
    assert _at(patched.json()["claims_lock_at"]) == T0 + timedelta(hours=12)


async def test_the_default_lock_follows_an_edited_deadline(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)

    await organiser.patch(f"/api/events/{event['id']}", json={"name": "Renamed"})
    later = await organiser.patch(
        f"/api/events/{event['id']}", json={"deadline_at": (T0 + timedelta(hours=48)).isoformat()}
    )

    assert later.status_code == 200
    assert _at(later.json()["claims_lock_at"]) == T0 + timedelta(hours=32)


@pytest.mark.parametrize("offset", [timedelta(seconds=-1), timedelta(hours=24, seconds=1)])
async def test_a_lock_outside_the_event_is_a_422(api: Api, offset: timedelta) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    lock = (T0 + offset).isoformat()

    created = await organiser.post("/api/events", json={**EVENT, "claims_lock_at": lock})
    patched = await organiser.patch(f"/api/events/{event['id']}", json={"claims_lock_at": lock})

    assert created.status_code == 422
    assert (patched.status_code, patched.json()["detail"]["code"]) == (422, "event_invalid")
    assert (await organiser.get(f"/api/events/{event['id']}")).json() == event


async def test_moving_the_deadline_before_the_lock_is_a_422(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser, claims_lock_at=(T0 + timedelta(hours=20)).isoformat())

    response = await organiser.patch(
        f"/api/events/{event['id']}", json={"deadline_at": (T0 + timedelta(hours=19)).isoformat()}
    )

    assert (response.status_code, response.json()["detail"]["code"]) == (422, "event_invalid")


async def test_the_rubric_is_frozen_once_scores_are_locked(api: Api, sessions: Sessions) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    async with sessions.begin() as s:
        await s.execute(text("UPDATE events SET scores_locked = true"))

    rubric = await organiser.patch(f"/api/events/{event['id']}", json={"rubric": RUBRIC[:1]})
    name = await organiser.patch(f"/api/events/{event['id']}", json={"name": "Renamed"})

    assert rubric.status_code == 409
    assert rubric.json()["detail"]["code"] == "scores_locked"
    assert name.status_code == 200


# --- team import -----------------------------------------------------------------------------

CSV = (
    "team_name,contact_email,repo_url\n"
    "Rocket,ada@example.com,https://github.com/acme/rocket\n"
    "Comet,,\n"
)


# --- the kickoff scan's settings (M9) --------------------------------------------------------


async def test_an_event_states_its_scan_settings(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    assert event["scan_extensions"][:5] == [".js", ".jsx", ".ts", ".tsx", ".py"]
    assert len(event["scan_extensions"]) == 12
    assert (event["common_code_min_teams"], event["baseline_retention_days"]) == (3, 30)

    edited = await organiser.patch(
        f"/api/events/{event['id']}",
        json={"scan_extensions": [".PY", ".rs"], "common_code_min_teams": 2,
              "baseline_retention_days": 7},
    )  # fmt: skip
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["scan_extensions"] == [".py", ".rs"]
    assert (body["common_code_min_teams"], body["baseline_retention_days"]) == (2, 7)
    for field, value in (("common_code_min_teams", 1), ("baseline_retention_days", 366)):
        refused = await organiser.post("/api/events", json={**EVENT, field: value})
        assert refused.status_code == 422, field


@pytest.mark.parametrize("extension", [".env", ".pem", ".key", "py", ".TOO-LONG-AN-EXTENSION"])
async def test_an_extension_that_could_hold_secrets_or_is_malformed_is_refused(
    api: Api, extension: str
) -> None:
    organiser = await api.organiser()
    created = await organiser.post("/api/events", json={**EVENT, "scan_extensions": [extension]})
    assert created.status_code == 422
    event = await api.event(organiser)
    patched = await organiser.patch(
        f"/api/events/{event['id']}", json={"scan_extensions": [".py", extension]}
    )
    assert patched.status_code == 422


async def test_extensions_freeze_once_a_baseline_is_sealed(api: Api, clock: FakeClock) -> None:
    flow = await started(api, clock, kickoff_ago=timedelta(hours=-2))
    path = f"/api/events/{flow.event['id']}"
    before = await flow.organiser.patch(path, json={"scan_extensions": [".py", ".js"]})
    assert before.status_code == 200
    _, code = await add_device(flow, "laptop")
    await scan(flow, code, {"a.js": b"let a = 1;"})

    frozen = await flow.organiser.patch(path, json={"scan_extensions": [".py"]})
    same = await flow.organiser.patch(path, json={"scan_extensions": [".py", ".js"]})
    other = await flow.organiser.patch(path, json={"baseline_retention_days": 10})

    assert frozen.status_code == 409 and frozen.json()["detail"]["code"] == "extensions_frozen"
    assert same.status_code == 200 and other.status_code == 200


async def test_teams_are_imported_from_csv(api: Api, sessions: Sessions) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)

    response = await api.import_csv(organiser, event["id"], CSV)

    assert response.status_code == 200
    body = response.json()
    assert [(t["name"], t["contact_email"]) for t in body["created"]] == [
        ("Rocket", "ada@example.com"),
        ("Comet", None),
    ]
    assert body["skipped"] == []
    async with sessions() as s:
        rows = (
            await s.execute(text("SELECT name, repo_url, registered_at FROM teams ORDER BY name"))
        ).all()
    # The CSV's repo URL is kept as a suggestion; the team registers the repository itself.
    assert [tuple(r) for r in rows] == [
        ("Comet", None, None),
        ("Rocket", "https://github.com/acme/rocket", None),
    ]


async def test_importing_again_skips_teams_that_already_exist(api: Api, sessions: Sessions) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    await api.import_csv(organiser, event["id"], CSV)

    response = await api.import_csv(organiser, event["id"], CSV + "Meteor,,\n")

    body = response.json()
    assert [t["name"] for t in body["created"]] == ["Meteor"]
    assert [(s["line"], s["team_name"]) for s in body["skipped"]] == [(2, "Rocket"), (3, "Comet")]
    async with sessions() as s:
        assert (await s.execute(text("SELECT count(*) FROM teams"))).scalar_one() == 3


async def test_the_same_team_name_can_be_used_in_another_event(api: Api) -> None:
    organiser = await api.organiser()
    first = await api.event(organiser, name="First")
    second = await api.event(organiser, name="Second")
    await api.import_csv(organiser, first["id"], CSV)

    response = await api.import_csv(organiser, second["id"], CSV)

    assert [t["name"] for t in response.json()["created"]] == ["Rocket", "Comet"]


async def test_a_file_with_a_bad_row_imports_nothing(api: Api, sessions: Sessions) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)

    response = await api.import_csv(
        organiser, event["id"], "team_name,contact_email\nRocket,ada@example.com\n,nobody@x.org\n"
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "csv_invalid"
    assert detail["errors"] == [{"line": 3, "message": "team_name is empty"}]
    async with sessions() as s:
        assert (await s.execute(text("SELECT count(*) FROM teams"))).scalar_one() == 0


async def test_an_oversized_file_is_refused(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)

    response = await api.import_csv(organiser, event["id"], b"team_name\n" + b"x" * MAX_BYTES)

    assert response.status_code == 413


async def test_importing_into_an_unknown_event_is_not_found(api: Api) -> None:
    organiser = await api.organiser()
    response = await api.import_csv(organiser, str(uuid.uuid4()), CSV)
    assert response.status_code == 404


# --- judges ----------------------------------------------------------------------------------


async def test_a_judge_is_added_to_an_event(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)

    response = await organiser.post(
        f"/api/events/{event['id']}/judges",
        json={"name": "Judge Judy", "email": "judy@example.com"},
    )

    assert response.status_code == 201
    judge = response.json()
    assert (judge["name"], judge["email"], judge["event_id"]) == (
        "Judge Judy", "judy@example.com", event["id"],
    )  # fmt: skip


async def test_a_judge_needs_a_name(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    response = await organiser.post(f"/api/events/{event['id']}/judges", json={"name": "  "})
    assert response.status_code == 422


# --- links -----------------------------------------------------------------------------------


async def test_a_link_is_returned_once_with_its_expiry(api: Api, clock: FakeClock) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    team = await api.add_team(organiser, event["id"], "Rocket")

    link = await api.link(organiser, event["id"], "team", team["id"])

    assert link["url"].startswith(f"{PUBLIC_BASE_URL}/redeem?token=")
    assert (link["role"], link["subject_id"]) == ("team", team["id"])
    assert link["expires_at"] == (clock.now() + timedelta(days=7)).isoformat().replace(
        "+00:00", "Z"
    )


async def test_a_link_must_name_someone_of_that_role_in_that_event(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    other_event = await api.event(organiser, name="Other")
    team = await api.add_team(organiser, event["id"], "Rocket")
    outsider = await api.add_team(organiser, other_event["id"], "Outsider")

    for role, subject in (
        ("judge", team["id"]),
        ("team", outsider["id"]),
        ("team", str(uuid.uuid4())),
    ):
        response = await organiser.post(
            f"/api/events/{event['id']}/links", json={"role": role, "subject_id": subject}
        )
        assert response.status_code == 404, (role, subject)
        assert response.json()["detail"]["code"] == "subject_not_found"


async def test_a_links_life_is_between_one_and_thirty_days(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    team = await api.add_team(organiser, event["id"], "Rocket")

    for days in (0, 31):
        response = await organiser.post(
            f"/api/events/{event['id']}/links",
            json={"role": "team", "subject_id": team["id"], "expires_in_days": days},
        )
        assert response.status_code == 422, days


async def test_revoking_is_repeatable_and_unknown_links_are_not_found(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    team = await api.add_team(organiser, event["id"], "Rocket")
    link = await api.link(organiser, event["id"], "team", team["id"])

    first = await organiser.delete(f"/api/links/{link['id']}")
    second = await organiser.delete(f"/api/links/{link['id']}")
    unknown = await organiser.delete(f"/api/links/{uuid.uuid4()}")

    assert (first.status_code, second.status_code, unknown.status_code) == (204, 204, 404)


async def test_events_are_listed_newest_made_first(api: Api) -> None:
    organiser = await api.organiser()
    later_kickoff = await api.event(organiser, name="First made")
    earlier = (T0 - timedelta(days=3)).isoformat()
    second = await api.event(organiser, name="Second made", kickoff_at=earlier)
    listed = (await organiser.get("/api/events")).json()["events"]
    assert [e["id"] for e in listed] == [second["id"], later_kickoff["id"]]
