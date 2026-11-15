"""The door's allowlist proxy for an app's own outside calls (D-063)."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from door.egress import Egress, allowed, serve

HOSTS = ("api.weather.test", "*.maps.test")


def test_hosts_match_exactly_or_under_a_star_entry() -> None:
    assert allowed("api.weather.test", HOSTS) and allowed("API.Weather.Test.", HOSTS)
    assert allowed("tiles.maps.test", HOSTS) and allowed("a.b.maps.test", HOSTS)
    assert not allowed("maps.test", HOSTS)  # a star entry is for subdomains only
    assert not allowed("evil-api.weather.test", HOSTS) and not allowed("weather.test", HOSTS)
    assert not allowed("api.weather.test.evil.com", HOSTS)


class Proxy:
    def __init__(self, upstream_port: int, **options: Any) -> None:
        self.lines: list[dict[str, Any]] = []
        self.upstream_port = upstream_port

        async def resolve(host: str, port: int) -> list[str]:
            return {"private.maps.test": ["10.0.0.5"]}.get(host, ["127.0.0.1"])

        self.egress = Egress(
            HOSTS,
            lambda **fields: self.lines.append(fields),
            resolve=resolve,
            is_public=lambda address: address == "127.0.0.1",  # the test's stand-in for public
            **options,
        )


@pytest.fixture
async def upstream() -> AsyncIterator[int]:
    """An outside host: echoes what it is sent, upper-cased."""

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(1024)
        writer.write(data.upper())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1]
    server.close()


async def ask(proxy: Proxy, first_line: str, payload: bytes = b"") -> bytes:
    server = await serve(proxy.egress, 0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"{first_line}\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    rest = b""
    if b" 200 " in head and payload:
        writer.write(payload)
        await writer.drain()
        rest = await reader.read(1024)
    else:
        rest = await reader.read(1024)
    writer.close()
    server.close()
    await asyncio.sleep(0.05)  # let the tunnel log its line
    return head + rest


async def test_an_allowed_host_is_tunnelled_and_logged_without_content(
    upstream: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The upstream listens on a random port; the proxy allows 443 only, so allow it instead.
    monkeypatch.setattr("door.egress.PORTS", (upstream,))
    proxy = Proxy(upstream)
    reply = await ask(proxy, f"CONNECT api.weather.test:{upstream} HTTP/1.1", b"hello")
    assert b"200 Connection Established" in reply and reply.endswith(b"HELLO")
    (line,) = proxy.lines
    assert (line["egress"], line["status"], line["bytes_up"], line["bytes_down"]) == (
        "api.weather.test",
        200,
        5,
        5,
    )
    assert "hello" not in str(line).lower()


@pytest.mark.parametrize(
    ("first_line", "reason"),
    [
        ("CONNECT api.other.test:443 HTTP/1.1", "not on this event's list"),
        ("CONNECT 93.184.216.34:443 HTTP/1.1", "an address is not allowed"),
        ("CONNECT api.weather.test:80 HTTP/1.1", "only port 443"),
        ("CONNECT private.maps.test:443 HTTP/1.1", "not public"),
        ("GET http://api.weather.test/ HTTP/1.1", "only HTTPS"),
    ],
)
async def test_everything_else_is_refused_in_words(
    upstream: int, first_line: str, reason: str
) -> None:
    proxy = Proxy(upstream)
    reply = await ask(proxy, first_line)
    assert reply.startswith(b"HTTP/1.1 403") and reason.encode() in reply
    (line,) = proxy.lines
    assert line["status"] == 403 and reason in line["refused"]


async def test_connections_past_the_cap_are_refused(upstream: int) -> None:
    proxy = Proxy(upstream, max_tunnels=0)
    reply = await ask(proxy, "CONNECT api.weather.test:443 HTTP/1.1")
    assert b"used up" in reply
