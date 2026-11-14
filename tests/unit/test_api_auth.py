"""Sign-in: the organiser password, invitation links and sessions (sections 12.4 and 17)."""

from pathlib import Path

from sqlalchemy import text

from api.auth import hash_token
from tests.api_helpers import ORGANISER_PASSWORD, PUBLIC_BASE_URL, Api, make_app, token_in
from tests.fakes import FakeClock, FakeRepoProvider
from tests.helpers import Sessions

# --- organiser -------------------------------------------------------------------------------


async def test_the_organiser_signs_in_with_the_password(api: Api) -> None:
    client = api.anonymous()

    response = await client.post("/api/auth/organiser", json={"password": ORGANISER_PASSWORD})

    assert response.status_code == 204
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("hp_session=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    me = await client.get("/api/me")
    assert me.status_code == 200
    assert (me.json()["role"], me.json()["subject_id"]) == ("organiser", None)


async def test_a_wrong_password_is_refused(api: Api) -> None:
    client = api.anonymous()

    response = await client.post("/api/auth/organiser", json={"password": "letmein"})

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_password"
    assert "set-cookie" not in response.headers
    assert (await client.get("/api/me")).status_code == 401


async def test_the_session_cookie_is_secure_outside_local_development(
    api: Api, migrated_database_url: str, tmp_path: Path
) -> None:
    local = await api.anonymous().post("/api/auth/organiser", json={"password": ORGANISER_PASSWORD})
    assert "Secure" not in local.headers["set-cookie"]

    app = make_app(
        migrated_database_url, tmp_path, FakeClock(api.app.state.clock.now()), FakeRepoProvider(),
        app_env="production",
    )  # fmt: skip
    production = (
        await Api(app)
        .anonymous()
        .post("/api/auth/organiser", json={"password": ORGANISER_PASSWORD})
    )
    await app.state.engine.dispose()
    assert "Secure" in production.headers["set-cookie"]


async def test_me_needs_a_session(api: Api) -> None:
    response = await api.anonymous().get("/api/me")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "not_signed_in"


async def test_a_session_ends_after_its_lifetime(api: Api, clock: FakeClock) -> None:
    organiser = await api.organiser()

    clock.advance(hours=11, minutes=59)
    assert (await organiser.get("/api/me")).status_code == 200
    clock.advance(minutes=2)
    assert (await organiser.get("/api/me")).status_code == 401


async def test_a_forged_cookie_is_refused(api: Api) -> None:
    client = api.anonymous()
    client.cookies.set("hp_session", "eyJyb2xlIjoib3JnYW5pc2VyIn0.forged-signature")
    assert (await client.get("/api/me")).status_code == 401


async def test_logout_ends_the_session(api: Api) -> None:
    organiser = await api.organiser()

    response = await organiser.post("/api/auth/logout")

    assert response.status_code == 204
    assert (await organiser.get("/api/me")).status_code == 401


async def test_the_organisers_me_names_the_latest_event(api: Api) -> None:
    organiser = await api.organiser()
    assert (await organiser.get("/api/me")).json()["event_id"] is None

    await api.event(organiser, name="First")
    second = await api.event(organiser, name="Second")

    assert (await organiser.get("/api/me")).json()["event_id"] == second["id"]


# --- invitation links ------------------------------------------------------------------------


async def test_a_team_link_signs_the_team_in_and_sends_it_to_its_page(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    team = await api.add_team(organiser, event["id"], "Rocket")
    link = await api.link(organiser, event["id"], "team", team["id"])
    assert link["url"].startswith(f"{PUBLIC_BASE_URL}/redeem?token=")

    client, response = await api.redeem(link["url"])

    assert (response.status_code, response.headers["location"]) == (303, "/team")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    me = (await client.get("/api/me")).json()
    assert (me["role"], me["subject_id"], me["event_id"], me["name"]) == (
        "team", team["id"], event["id"], "Rocket",
    )  # fmt: skip


async def test_a_judge_link_signs_the_judge_in_and_sends_them_to_the_judge_page(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    judge = await api.add_judge(organiser, event["id"], "Judge Judy")
    link = await api.link(organiser, event["id"], "judge", judge["id"])

    client, response = await api.redeem(link["url"])

    assert (response.status_code, response.headers["location"]) == (303, "/judge")
    me = (await client.get("/api/me")).json()
    assert (me["role"], me["subject_id"], me["name"]) == ("judge", judge["id"], "Judge Judy")


async def test_a_link_can_be_redeemed_on_more_than_one_device(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    judge = await api.add_judge(organiser, event["id"])
    link = await api.link(organiser, event["id"], "judge", judge["id"])

    phone, first = await api.redeem(link["url"])
    laptop, second = await api.redeem(link["url"])

    assert (first.status_code, second.status_code) == (303, 303)
    assert (await phone.get("/api/me")).status_code == 200
    assert (await laptop.get("/api/me")).status_code == 200


async def test_an_unknown_token_is_refused(api: Api) -> None:
    client = api.anonymous()

    response = await client.get("/api/auth/redeem", params={"token": "not-a-real-token"})

    assert (response.status_code, response.headers["location"]) == (303, "/redeem?error=invalid")
    assert "set-cookie" not in response.headers
    assert (await client.get("/api/me")).status_code == 401


async def test_an_expired_link_is_refused(api: Api, clock: FakeClock) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    team = await api.add_team(organiser, event["id"], "Rocket")
    link = await api.link(organiser, event["id"], "team", team["id"])  # the default: 7 days

    clock.advance(days=6, hours=23)
    _, in_time = await api.redeem(link["url"])
    clock.advance(hours=2)
    late_client, late = await api.redeem(link["url"])

    assert in_time.headers["location"] == "/team"
    assert late.headers["location"] == "/redeem?error=expired"
    assert (await late_client.get("/api/me")).status_code == 401


async def test_a_link_can_be_given_a_shorter_life(api: Api, clock: FakeClock) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    team = await api.add_team(organiser, event["id"], "Rocket")
    link = await api.link(organiser, event["id"], "team", team["id"], expires_in_days=1)

    clock.advance(days=1, minutes=1)
    _, response = await api.redeem(link["url"])

    assert response.headers["location"] == "/redeem?error=expired"


async def test_a_revoked_link_is_refused(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    team = await api.add_team(organiser, event["id"], "Rocket")
    link = await api.link(organiser, event["id"], "team", team["id"])

    revoked = await organiser.delete(f"/api/links/{link['id']}")
    client, response = await api.redeem(link["url"])

    assert revoked.status_code == 204
    assert response.headers["location"] == "/redeem?error=revoked"
    assert (await client.get("/api/me")).status_code == 401


async def test_revoking_a_link_ends_the_sessions_it_started(api: Api) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    team = await api.add_team(organiser, event["id"], "Rocket")
    link = await api.link(organiser, event["id"], "team", team["id"])
    client, _ = await api.redeem(link["url"])
    assert (await client.get("/api/team")).status_code == 200

    await organiser.delete(f"/api/links/{link['id']}")

    response = await client.get("/api/team")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "link_revoked"


async def test_only_the_hash_of_a_token_is_stored(api: Api, sessions: Sessions) -> None:
    organiser = await api.organiser()
    event = await api.event(organiser)
    team = await api.add_team(organiser, event["id"], "Rocket")
    link = await api.link(organiser, event["id"], "team", team["id"])
    token = token_in(link["url"])

    async with sessions() as s:
        stored = (await s.execute(text("SELECT row_to_json(l)::text FROM access_links l"))).scalar()

    assert stored is not None
    assert hash_token(token) in stored
    assert token not in stored
