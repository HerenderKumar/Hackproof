"""Section 12.3's isolation with the model door on (Prune design, B2), against real Docker.

The door's way out is a stub Prune on the run's out network, so nothing here needs a key or the
internet. Each probe has a positive control. Runs inside the worker image.
"""

import dataclasses
import uuid

import pytest

import tests.integration.test_sandbox as sandbox
from tests.integration.test_sandbox import INTERNET, PYTHON, REACH, Docker
from worker.door import (
    DoorRun,
    DoorTerms,
    app_variables,
    door_container,
    out_network_name,
    prepare_folder,
    wait_ready,
)
from worker.sandbox import app_container, network_name

pytestmark = pytest.mark.integration
dk = sandbox.dk  # the same Docker harness and cleanup

TEAM = uuid.UUID("66666666-6666-6666-6666-666666666666")

# A stub Prune: answers any POST with a chat reply, and says which path it was asked for.
STUB = """
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        reply = {"stub_path": self.path, "choices": [{"message": {"content": "hi"}}]}
        body = json.dumps(reply).encode()
        self.send_response(200); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST
http.server.ThreadingHTTPServer(("0.0.0.0", 8000), H).serve_forever()
"""

# POST a chat call through the door with the token in argv[2]; exit 0 on 200, else 3.
CALL = """
import json, sys, urllib.request
req = urllib.request.Request(sys.argv[1], method="POST", data=json.dumps(
    {"model": "gpt-4.1-nano", "messages": [{"role": "user", "content": "hi"}]}).encode(),
    headers={"Authorization": "Bearer " + sys.argv[2], "content-type": "application/json"})
try:
    reply = urllib.request.urlopen(req, timeout=8)
except Exception as exc:
    print("blocked:", type(exc).__name__, exc); sys.exit(3)
print(reply.read().decode()); sys.exit(0)
"""


async def open_door(dk: Docker, attempt_id: uuid.UUID, egress: tuple[str, ...] = ()) -> DoorRun:
    """The run's two networks, a stub Prune on the out one, and the door, ready."""
    internal = network_name(attempt_id)
    await dk.runtime.create_network(internal, str(attempt_id))
    out = out_network_name(attempt_id)
    await dk.runtime.create_network(out, str(attempt_id), internal=False)
    stub = app_container(PYTHON, attempt_id, dk.limits)
    stub = dataclasses.replace(stub, name=f"hp-{attempt_id}-stub", network=out, alias="prune-stub")
    await dk.start(stub, "python", "-c", STUB)
    terms = DoorTerms(
        image="hackproof-door:1",
        prune_base_url="http://prune-stub:8000",
        prune_api_key="prune_secret_key",
        models=("gpt-4.1-nano",),
        run_budget_usd=0.05,
        max_calls=5,
        timeout_s=10,
        egress_hosts=egress,
    )
    door = DoorRun.make(
        terms, attempt_id=attempt_id, team_id=TEAM, mode="final", run_dir=dk.run_dir(attempt_id)
    )
    prepare_folder(door)
    door_id = await dk.runtime.start_container(door_container(door, internal))

    async def running() -> bool:
        return (await dk.runtime.container_state(door_id)).running

    await wait_ready(door, running)
    return door


async def probe(
    dk: Docker, attempt_id: uuid.UUID, name: str, *command: str, door: DoorRun | None = None
) -> tuple[int | None, str]:
    extra = app_variables(door) if door is not None else None
    app = app_container(PYTHON, attempt_id, dk.limits, extra_env=extra)
    container = await dk.start(dataclasses.replace(app, name=f"{app.name}-{name}"), *command)
    return await dk.exit_code(container), await dk.logs(container)


async def test_the_app_reaches_the_door_and_through_it_only_prune(dk: Docker) -> None:
    run = dk.attempt()
    door = await open_door(dk, run)
    url = "http://model-door:8080/v1/chat/completions"

    code, out = await probe(dk, run, "call", "python", "-c", CALL, url, door.token)
    assert code == 0 and '"stub_path": "/v1/chat/completions"' in out, out  # positive control

    code, out = await probe(dk, run, "badtoken", "python", "-c", CALL, url, "not-the-token")
    assert code == 3, out
    code, out = await probe(
        dk, run, "route", "python", "-c", CALL, "http://model-door:8080/v1/embeddings", door.token
    )
    assert code == 3, out  # the door forwards no other route
    code, out = await probe(dk, run, "stub", "python", "-c", REACH, "http://prune-stub:8000/")
    assert code == 3, out  # Prune (here its stub) is not reachable from the app directly
    code, out = await probe(dk, run, "internet", "python", "-c", INTERNET)
    assert code == 3, out


async def test_one_run_cannot_use_another_run_s_door(dk: Docker) -> None:
    run_a, run_b = dk.attempt(), dk.attempt()
    door_a = await open_door(dk, run_a)
    await dk.runtime.create_network(network_name(run_b), str(run_b))
    door_ip = dk.client.containers.get(f"hp-{run_a}-door").attrs["NetworkSettings"]["Networks"][
        network_name(run_a)
    ]["IPAddress"]
    for n, url in enumerate(
        ("http://model-door:8080/v1/chat/completions", f"http://{door_ip}:8080/v1/chat/completions")
    ):
        code, out = await probe(dk, run_b, f"cross-{n}", "python", "-c", CALL, url, door_a.token)
        assert code == 3, f"run B reached run A's door at {url}: {out}"


async def test_an_app_reaches_only_the_listed_outside_hosts_through_the_door(dk: Docker) -> None:
    run = dk.attempt()
    door = await open_door(dk, run, egress=("example.com",))
    # Positive control: the listed host answers through the proxy the app's variables name.
    code, out = await probe(
        dk, run, "listed", "python", "-c", REACH, "https://example.com/", door=door
    )
    assert code == 0, out
    code, out = await probe(
        dk, run, "unlisted", "python", "-c", REACH, "https://www.iana.org/", door=door
    )
    assert code == 3 and "403" in out, out
    # Without the proxy variables the app still has no way out.
    code, out = await probe(dk, run, "direct", "python", "-c", INTERNET)
    assert code == 3, out
