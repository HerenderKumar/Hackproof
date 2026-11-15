"""A run with the model door on (Prune design, B2), against a fake runtime. Real isolation with
the door (the app reaches nothing but the door, the door nothing but Prune) is checked against
Docker in tests/integration/test_door_isolation.py."""

import asyncio
import dataclasses
import json
import uuid
from pathlib import Path

import pytest

from core.errors import InfraError
from tests.fakes import FakeRuntime
from tests.unit.test_sandbox import ATTEMPT, IMAGE, LIMITS, SPECS, UP, reports, stays_up
from worker.door import DOOR_ALIAS, DoorRun, DoorTerms
from worker.runtime import ContainerSpec
from worker.sandbox import RunOutcome, run_sandboxed

TEAM = uuid.UUID("55555555-5555-5555-5555-555555555555")
TERMS = DoorTerms(
    image="hackproof-door:1",
    prune_base_url="https://api.withprune.com",
    prune_api_key="prune_secret_key",
    models=("gpt-4.1-nano", "claude-haiku-4-5"),
    run_budget_usd=0.05,
    max_calls=20,
    timeout_s=60.0,
)
LOG_LINES = [{"route": "/v1/chat/completions", "model": "gpt-4.1-nano", "status": 200}]


def door_for(run_dir: Path, mode: str = "final") -> DoorRun:
    return DoorRun.make(TERMS, attempt_id=ATTEMPT, team_id=TEAM, mode=mode, run_dir=run_dir)


def door_answers(runtime: FakeRuntime, container_id: str, spec: ContainerSpec) -> None:
    folder = Path(spec.mounts[0][0])
    (folder / "ready").write_text("ready\n")
    (folder / "log.jsonl").write_text("".join(json.dumps(line) + "\n" for line in LOG_LINES))


def door_dies(runtime: FakeRuntime, container_id: str, spec: ContainerSpec) -> None:
    runtime.exit(container_id, 1)


def scripted(door: object, app: object = stays_up, runner: object = None) -> FakeRuntime:
    runtime = FakeRuntime()

    def on_start(rt: FakeRuntime, container_id: str, spec: ContainerSpec) -> None:
        if spec.image == TERMS.image:
            door(rt, container_id, spec)  # type: ignore[operator]
        elif spec.alias == "app":
            app(rt, container_id, spec)  # type: ignore[operator]
        else:
            (runner or reports(UP))(rt, container_id, spec)  # type: ignore[operator]

    runtime.on_start = on_start
    return runtime


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    path = tmp_path / "work" / str(ATTEMPT) / "run"
    path.mkdir(parents=True)
    return path


async def run(runtime: FakeRuntime, run_dir: Path, door: DoorRun | None) -> RunOutcome:
    return await asyncio.wait_for(
        run_sandboxed(
            runtime,
            image=IMAGE,
            attempt_id=ATTEMPT,
            run_dir=run_dir,
            mode="final",
            specs=SPECS,
            limits=LIMITS,
            app_log_path=run_dir.parent / "app.log",
            poll_s=0.01,
            door=door,
        ),
        timeout=10,
    )


def spec_of(
    runtime: FakeRuntime, *, image: str | None = None, alias: str | None = None
) -> ContainerSpec:
    (spec,) = [
        s
        for s in runtime.specs.values()
        if (image is None or s.image == image) and (alias is None or s.alias == alias)
    ]
    return spec


async def test_the_door_joins_both_networks_and_the_app_only_the_internal_one(
    run_dir: Path,
) -> None:
    runtime = scripted(door_answers)
    outcome = await run(runtime, run_dir, door_for(run_dir))
    assert outcome.startup_status == "ok"
    names = {runtime.network_names[i]: runtime.network_internal[i] for i in runtime.network_names}
    assert names == {f"hp-{ATTEMPT}": True, f"hp-{ATTEMPT}-out": False}
    door, app = spec_of(runtime, image=TERMS.image), spec_of(runtime, alias="app")
    assert door.network == f"hp-{ATTEMPT}-out" and door.extra_networks == (
        (f"hp-{ATTEMPT}", DOOR_ALIAS),
    )
    assert app.network == f"hp-{ATTEMPT}" and app.extra_networks == ()
    assert door.read_only and door.user == "10001:10001" and door.memory == "256m"


async def test_the_app_gets_the_door_and_a_token_never_the_prune_key(run_dir: Path) -> None:
    runtime = scripted(door_answers)
    door = door_for(run_dir)
    await run(runtime, run_dir, door)
    app, runner = spec_of(runtime, alias="app"), spec_of(runtime, image=LIMITS.runner_image)
    env = app.environment
    assert env["OPENAI_BASE_URL"] == "http://model-door:8080/v1"
    assert env["ANTHROPIC_BASE_URL"] == "http://model-door:8080"
    assert env["OPENAI_API_KEY"] == env["ANTHROPIC_API_KEY"] == door.token and len(door.token) >= 24
    assert env["HP_MODELS"] == "gpt-4.1-nano,claude-haiku-4-5"
    assert env["PORT"] == "8080"
    for spec in (app, runner):
        assert TERMS.prune_api_key not in json.dumps(spec.environment)
        assert not any(k.startswith(("PRUNE", "DOOR_")) for k in spec.environment)
    door_env = spec_of(runtime, image=TERMS.image).environment
    assert door_env["PRUNE_API_KEY"] == TERMS.prune_api_key and door_env["DOOR_TOKEN"] == door.token
    assert (door_env["DOOR_RUN_ID"], door_env["DOOR_CUSTOMER_ID"]) == (str(ATTEMPT), f"team-{TEAM}")
    assert (door_env["DOOR_FEATURE_ID"], door_env["DOOR_MAX_CALLS"]) == ("final_run", "20")


async def test_a_self_check_s_door_names_its_feature(run_dir: Path) -> None:
    assert door_for(run_dir, mode="practice").feature == "self_check_run"


async def test_the_door_log_is_read_and_everything_is_removed(run_dir: Path) -> None:
    runtime = scripted(door_answers)
    door = door_for(run_dir)
    outcome = await run(runtime, run_dir, door)
    assert outcome.door_log == tuple(LOG_LINES)
    assert runtime.containers == {} and runtime.networks == {}
    assert not door.folder.exists()
    assert door.folder.parent == run_dir.parent and door.folder != run_dir  # never in /work


async def test_the_door_is_ready_before_the_app_starts(run_dir: Path) -> None:
    runtime = scripted(door_answers)
    await run(runtime, run_dir, door_for(run_dir))
    order = [spec.image for spec in runtime.specs.values()]
    assert order.index(TERMS.image) < order.index(IMAGE)


async def test_a_door_that_dies_is_an_infrastructure_error_and_leaves_nothing(
    run_dir: Path,
) -> None:
    runtime = scripted(door_dies)
    with pytest.raises(InfraError, match="model door did not start"):
        await run(runtime, run_dir, door_for(run_dir))
    assert IMAGE not in [s.image for s in runtime.specs.values()]  # the app never started
    assert runtime.containers == {} and runtime.networks == {}


async def test_with_the_door_off_a_run_is_unchanged(run_dir: Path) -> None:
    runtime = scripted(door_answers)
    outcome = await run(runtime, run_dir, None)
    assert outcome.door_log is None
    assert [runtime.network_names[i] for i in runtime.network_names] == [f"hp-{ATTEMPT}"]
    assert TERMS.image not in [s.image for s in runtime.specs.values()]
    assert not any(
        k.startswith(("OPENAI", "ANTHROPIC", "HP_"))
        for k in spec_of(runtime, alias="app").environment
    )


async def test_listed_outside_hosts_give_the_app_the_proxy_variables(run_dir: Path) -> None:
    terms = dataclasses.replace(TERMS, egress_hosts=("api.weather.test", "*.maps.test"))
    door = DoorRun.make(terms, attempt_id=ATTEMPT, team_id=TEAM, mode="final", run_dir=run_dir)
    runtime = scripted(door_answers)
    await run(runtime, run_dir, door)
    env = spec_of(runtime, alias="app").environment
    assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://model-door:8081"
    assert "model-door" in env["NO_PROXY"] and env["NODE_USE_ENV_PROXY"] == "1"
    assert env["HP_OUTSIDE_HOSTS"] == "api.weather.test,*.maps.test"
    door_env = spec_of(runtime, image=TERMS.image).environment
    assert door_env["DOOR_EGRESS_HOSTS"] == "api.weather.test,*.maps.test"


async def test_with_no_outside_hosts_the_app_has_no_proxy(run_dir: Path) -> None:
    runtime = scripted(door_answers)
    await run(runtime, run_dir, door_for(run_dir))
    env = spec_of(runtime, alias="app").environment
    assert not any("proxy" in k.lower() for k in env) and "HP_OUTSIDE_HOSTS" not in env
