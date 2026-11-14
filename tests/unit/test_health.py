import httpx

from api.main import create_app
from core.settings import Settings


def app_client() -> httpx.AsyncClient:
    settings = Settings(_env_file=None, organiser_password="pw", session_secret="s" * 32)  # type: ignore[call-arg]
    app = create_app(settings)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_healthz_reports_the_process_is_up() -> None:
    async with app_client() as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_every_response_carries_a_request_id() -> None:
    async with app_client() as client:
        response = await client.get("/healthz")
    assert len(response.headers["x-request-id"]) >= 16


async def test_an_incoming_request_id_is_echoed() -> None:
    async with app_client() as client:
        response = await client.get("/healthz", headers={"x-request-id": "abc-123-abc-123-abc"})
    assert response.headers["x-request-id"] == "abc-123-abc-123-abc"
