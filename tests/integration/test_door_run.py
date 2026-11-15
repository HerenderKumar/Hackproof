"""A model-backed app through a whole run with the door on (Prune design, B5), against Docker.

Offline, the door forwards to a stub Prune that a test runtime starts on each run's out network
as it is created. With `PRUNE_LIVE=1` and `PRUNE_API_KEY` set (and a provider key in Prune's
vault), the same app runs against the real Prune: its receipt is read and verified, and a run
whose budget is zero is refused by Prune, as the team would see it.
"""

import dataclasses
import json
import os
import shutil
import uuid
from pathlib import Path

import pytest

import tests.integration.test_sandbox as sandbox
from core.prune import PruneClient
from tests.integration.test_sandbox import PYTHON, STARTUP_SPEC, TEAM, Docker
from worker.build import build_image
from worker.door import DoorRun, DoorTerms
from worker.model_use import summarise, with_receipt
from worker.runtime import ContainerSpec, DockerRuntime
from worker.sandbox import RunOutcome, app_container, run_sandboxed

pytestmark = pytest.mark.integration
dk = sandbox.dk

APPS = Path(__file__).parent / "apps" / "ai_echo"
MODEL = os.environ.get("PRUNE_LIVE_MODEL", "gpt-4.1-nano")
LIVE = os.environ.get("PRUNE_LIVE") == "1"

# A stub Prune: a chat reply with usage and prune_metadata, as Prune answers.
STUB = """
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        reply = {"choices": [{"message": {"content": "ready"}}],
                 "usage": {"prompt_tokens": 9, "completion_tokens": 1},
                 "prune_metadata": {"cost_with_prune_usd": 0.00001, "cache_hit": False,
                                    "optimizations_applied": []}}
        body = json.dumps(reply).encode()
        self.send_response(200); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body))); self.end_headers()
        self.wfile.write(body)
http.server.ThreadingHTTPServer(("0.0.0.0", 8000), H).serve_forever()
"""


class StubPruneRuntime(DockerRuntime):
    """Docker, plus a stub Prune on every out network the moment it exists."""

    def __init__(self, limits: object) -> None:
        super().__init__()
        self._limits = limits
        self._stubs: dict[str, str] = {}  # out network ID: its stub's container ID

    async def create_network(self, name: str, attempt: str, *, internal: bool = True) -> str:
        network_id = await super().create_network(name, attempt, internal=internal)
        if not internal:
            stub: ContainerSpec = app_container(PYTHON, uuid.UUID(attempt), self._limits)  # type: ignore[arg-type]
            stub = dataclasses.replace(
                stub, name=f"{name}-stub", network=name, alias="prune-stub",
                command=("python", "-c", STUB), environment={},
            )  # fmt: skip
            self._stubs[network_id] = await self.start_container(stub)
        return network_id

    async def remove_network(self, network_id: str) -> None:
        if network_id in self._stubs:
            await self.remove_container(self._stubs.pop(network_id))
        await super().remove_network(network_id)


def terms(**changes: object) -> DoorTerms:
    base = DoorTerms(
        image="hackproof-door:1",
        prune_base_url="http://prune-stub:8000",
        prune_api_key="prune_stub_key",
        models=(MODEL,),
        run_budget_usd=0.05,
        max_calls=5,
        timeout_s=30,
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


async def run_app(
    dk: Docker, runtime: DockerRuntime, variant: str, door_terms: DoorTerms
) -> tuple[RunOutcome, DoorRun]:
    attempt_id = dk.attempt()
    work = dk.artifacts / "work" / str(attempt_id)
    src = work / "src"
    shutil.copytree(APPS / variant, src)
    built = await build_image(
        src, work / "build", team_id=TEAM, attempt_id=attempt_id, log_path=work / "build.log",
        runtime=runtime, timeout_s=300, buildkit_host="docker-container://buildkit",
        railpack_version="0.43.0",
    )  # fmt: skip
    assert built.status == "ok" and built.image is not None, built.log_path.read_text()[-2000:]
    run_dir = dk.run_dir(attempt_id)
    door = DoorRun.make(
        door_terms, attempt_id=attempt_id, team_id=TEAM, mode="final", run_dir=run_dir
    )
    outcome = await run_sandboxed(
        runtime, image=built.image, attempt_id=attempt_id, run_dir=run_dir, mode="final",
        specs=[STARTUP_SPEC], limits=dk.limits, app_log_path=work / "app.log", door=door,
    )  # fmt: skip
    assert dk.labelled(attempt_id) == ([], [])  # the door, both networks and the app are gone
    return outcome, door


async def test_a_model_backed_app_passes_with_its_calls_counted(dk: Docker) -> None:
    runtime = StubPruneRuntime(dk.limits)
    outcome, door = await run_app(dk, runtime, "real", terms())
    assert outcome.startup_status == "ok", outcome.app_log_path.read_text()[-1500:]
    use = summarise(outcome.door_log or ())
    assert (
        use["calls"] >= 1
        and use["calls_ok"] == use["calls"]
        and use["models"] == {MODEL: use["calls"]}
    )
    assert use["tokens"]["prompt_tokens"] == 9 * use["calls"]
    assert not door.folder.exists()


async def test_a_canned_app_passes_with_no_model_calls(dk: Docker) -> None:
    runtime = StubPruneRuntime(dk.limits)
    outcome, _ = await run_app(dk, runtime, "canned", terms())
    assert outcome.startup_status == "ok"
    assert outcome.door_log == () and summarise(outcome.door_log)["calls"] == 0


@pytest.mark.skipif(
    not LIVE, reason="the live check: PRUNE_LIVE=1 with PRUNE_API_KEY and a vaulted provider key"
)
async def test_live_a_run_through_the_real_prune_is_receipted_and_its_budget_holds(
    dk: Docker,
) -> None:
    key = os.environ["PRUNE_API_KEY"]
    live = terms(prune_base_url="https://api.withprune.com", prune_api_key=key)
    outcome, door = await run_app(dk, dk.runtime, "real", live)
    app_log = outcome.app_log_path.read_text()[-600:]
    assert outcome.startup_status == "ok", json.dumps([outcome.door_log, app_log])
    async with PruneClient(live.prune_base_url, key) as prune:
        use = await with_receipt(summarise(outcome.door_log or ()), prune, door.run_id)
    assert use["calls"] >= 1 and use["receipt_verified"] is True, use
    assert use["optimised"] is False, use  # cache and routing were asked off

    # On purpose: a run with no budget is refused by Prune, and that is the team's, not ours.
    broke, _ = await run_app(dk, dk.runtime, "real", dataclasses.replace(live, run_budget_usd=0.0))
    assert broke.startup_status == "fail"
    refused = summarise(broke.door_log or ())["refused"]
    assert refused and "budget" in refused[0]["reason"], refused
