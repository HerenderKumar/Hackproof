"""The door's allowlist proxy: the app's own calls to outside APIs (the human's choice, D-063).

Model calls go to Prune, which filters them (Shield: tools, secrets, personal data, injection).
Calls an app makes itself, to a weather or maps API say, Prune cannot see; they come here. The
organiser lists the hosts an event allows (`DOOR_EGRESS_HOSTS`); with none listed this proxy does
not run and the app has no way out but the model door.

HTTPS only, through `CONNECT host:443`. A host is allowed when it is on the list exactly, or
under an entry written `*.example.com`. An IP address is refused, and so is a name that resolves
to a private, loopback, link-local or otherwise reserved address, so the host machine and its
network stay out of reach. The tunnel carries TLS end to end: the door sees the host and the
bytes each way, never the content. One log line per tunnel, as for model calls.
"""

import asyncio
import contextlib
import ipaddress
import socket
import time
from collections.abc import Callable, Sequence
from typing import Any

HEADER_LIMIT = 8192
PORTS = (443,)
IDLE_S = 60.0
CHUNK = 65536

Log = Callable[..., None]


def allowed(host: str, hosts: Sequence[str]) -> bool:
    host = host.lower().rstrip(".")
    for entry in hosts:
        entry = entry.lower().rstrip(".")
        if entry.startswith("*.") and host.endswith(entry[1:]) and host != entry[2:]:
            return True
        if host == entry:
            return True
    return False


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_multicast


class Egress:
    def __init__(
        self,
        hosts: Sequence[str],
        log: Log,
        *,
        max_tunnels: int = 200,
        max_concurrent: int = 16,
        resolve: Callable[[str, int], Any] | None = None,
        is_public: Callable[[str], bool] = _public,
    ) -> None:
        self.hosts = tuple(h.strip() for h in hosts if h.strip())
        self.log = log
        self.max_tunnels = max_tunnels
        self.tunnels = 0
        self._slots = asyncio.Semaphore(max_concurrent)
        self._resolve = resolve or self._getaddrinfo
        self._is_public = is_public

    @staticmethod
    async def _getaddrinfo(host: str, port: int) -> list[str]:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return [str(info[4][0]) for info in infos]

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        started = time.monotonic()
        host, port = "", 0
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            if len(head) > HEADER_LIMIT:
                raise ValueError("header too long")
            method, target, _ = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ", 2)
            if method.upper() != "CONNECT":
                await self._refuse(writer, target, 0, "only HTTPS (CONNECT) is allowed")
                return
            host, _, port_text = target.rpartition(":")
            port = int(port_text) if port_text.isdigit() else 0
            reason = self._check(host, port)
            if reason is None:
                addresses = await self._resolve(host, port)
                if not addresses or not all(self._is_public(a) for a in addresses):
                    reason = "the host resolves to an address that is not public"
            if reason is not None:
                await self._refuse(writer, host, port, reason)
                return
            self.tunnels += 1
            async with self._slots:
                await self._tunnel(reader, writer, host, port, addresses[0], started)
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            await self._refuse(writer, host, port, "not a proxy request")
        finally:
            writer.close()

    def _check(self, host: str, port: int) -> str | None:
        if not host or _is_ip(host):
            return "an address is not allowed, only a host name on the event's list"
        if port not in PORTS:
            return f"only port {', '.join(map(str, PORTS))} is allowed"
        if not allowed(host, self.hosts):
            return f"{host} is not on this event's list of outside hosts"
        if self.tunnels >= self.max_tunnels:
            return "this run's outside connections are used up"
        return None

    async def _refuse(
        self, writer: asyncio.StreamWriter, host: str, port: int, reason: str
    ) -> None:
        self.log(egress=host, port=port, status=403, refused=reason)
        body = (reason + "\n").encode()
        writer.write(
            b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n"
            + f"Content-Length: {len(body)}\r\nX-Door-Refusal: egress\r\n\r\n".encode()
            + body
        )
        with contextlib.suppress(ConnectionError):
            await writer.drain()

    async def _tunnel(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        host: str,
        port: int,
        address: str,
        started: float,
    ) -> None:
        try:
            # The checked address, not the name again: a second lookup could answer otherwise.
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(address, port), timeout=10
            )
        except (TimeoutError, OSError) as error:
            self.log(egress=host, port=port, status=502, unreachable=type(error).__name__)
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        counts = {"up": 0, "down": 0}

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter, key: str) -> None:
            try:
                while data := await asyncio.wait_for(src.read(CHUNK), timeout=IDLE_S):
                    counts[key] += len(data)
                    dst.write(data)
                    await dst.drain()
            except (TimeoutError, ConnectionError):
                pass
            finally:
                dst.close()

        await asyncio.gather(pipe(reader, up_writer, "up"), pipe(up_reader, writer, "down"))
        self.log(
            egress=host,
            port=port,
            status=200,
            bytes_up=counts["up"],
            bytes_down=counts["down"],
            seconds=round(time.monotonic() - started, 2),
        )


async def serve(egress: Egress, port: int) -> asyncio.Server:
    return await asyncio.start_server(egress.handle, "0.0.0.0", port, limit=HEADER_LIMIT)
