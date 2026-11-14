"""Driving the API in tests: one HTTP client (one cookie jar) per person."""

from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI

from api.main import create_app
from core.settings import Settings
from tests.factories import RUBRIC, T0
from tests.fakes import FakeClock, FakeRepoProvider

ORGANISER_PASSWORD = "correct horse battery staple"
PUBLIC_BASE_URL = "http://localhost:3000"

EVENT: dict[str, Any] = {
    "name": "Winter Hack",
    "kickoff_at": T0.isoformat(),
    "deadline_at": (T0 + timedelta(hours=24)).isoformat(),
    "rubric": RUBRIC,
    "policy_text": "Declare starter code, earlier work and AI assistance.",
}


def make_app(
    database_url: str,
    artifact_dir: Path,
    clock: FakeClock,
    repos: FakeRepoProvider,
    **settings: Any,
) -> FastAPI:
    values: dict[str, Any] = {
        "organiser_password": ORGANISER_PASSWORD,
        "session_secret": "s" * 32,
        "database_url": database_url,
        "host_artifact_dir": artifact_dir,
        "app_env": "local",
        "public_base_url": PUBLIC_BASE_URL,
        "repo_provider": "local",
    }
    config = Settings(_env_file=None, **{**values, **settings})  # type: ignore[arg-type]
    return create_app(config, clock=clock, repo_provider=repos)


def token_in(url: str) -> str:
    return parse_qs(urlsplit(url).query)["token"][0]


class Api:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self._clients: list[httpx.AsyncClient] = []

    def anonymous(self) -> httpx.AsyncClient:
        """A new browser with no cookies."""
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )
        self._clients.append(client)
        return client

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()

    async def organiser(self) -> httpx.AsyncClient:
        client = self.anonymous()
        response = await client.post("/api/auth/organiser", json={"password": ORGANISER_PASSWORD})
        assert response.status_code == 204, response.text
        return client

    async def event(self, organiser: httpx.AsyncClient, **overrides: Any) -> dict[str, Any]:
        response = await organiser.post("/api/events", json={**EVENT, **overrides})
        assert response.status_code == 201, response.text
        created: dict[str, Any] = response.json()
        return created

    async def import_csv(
        self, organiser: httpx.AsyncClient, event_id: str, text: str | bytes
    ) -> httpx.Response:
        data = text.encode() if isinstance(text, str) else text
        return await organiser.post(
            f"/api/events/{event_id}/teams/import", files={"file": ("teams.csv", data, "text/csv")}
        )

    async def add_team(
        self, organiser: httpx.AsyncClient, event_id: str, name: str, repo_url: str = ""
    ) -> dict[str, Any]:
        response = await self.import_csv(
            organiser, event_id, f"team_name,contact_email,repo_url\n{name},,{repo_url}\n"
        )
        assert response.status_code == 200, response.text
        team: dict[str, Any] = response.json()["created"][0]
        return team

    async def add_judge(
        self, organiser: httpx.AsyncClient, event_id: str, name: str = "Judge Judy"
    ) -> dict[str, Any]:
        response = await organiser.post(f"/api/events/{event_id}/judges", json={"name": name})
        assert response.status_code == 201, response.text
        judge: dict[str, Any] = response.json()
        return judge

    async def link(
        self,
        organiser: httpx.AsyncClient,
        event_id: str,
        role: str,
        subject_id: str,
        **body: Any,
    ) -> dict[str, Any]:
        response = await organiser.post(
            f"/api/events/{event_id}/links", json={"role": role, "subject_id": subject_id, **body}
        )
        assert response.status_code == 201, response.text
        link: dict[str, Any] = response.json()
        return link

    async def redeem(self, url: str) -> tuple[httpx.AsyncClient, httpx.Response]:
        """Open an invitation link in a new browser."""
        client = self.anonymous()
        response = await client.get("/api/auth/redeem", params={"token": token_in(url)})
        return client, response

    async def team(
        self, organiser: httpx.AsyncClient, event_id: str, name: str = "Rocket"
    ) -> tuple[dict[str, Any], httpx.AsyncClient]:
        """A team, and a browser signed in as it."""
        team = await self.add_team(organiser, event_id, name)
        link = await self.link(organiser, event_id, "team", team["id"])
        client, response = await self.redeem(link["url"])
        assert response.status_code == 303, response.text
        return team, client

    async def judge(
        self, organiser: httpx.AsyncClient, event_id: str, name: str = "Judge Judy"
    ) -> tuple[dict[str, Any], httpx.AsyncClient]:
        judge = await self.add_judge(organiser, event_id, name)
        link = await self.link(organiser, event_id, "judge", judge["id"])
        client, response = await self.redeem(link["url"])
        assert response.status_code == 303, response.text
        return judge, client
