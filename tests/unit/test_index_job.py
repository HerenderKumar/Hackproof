"""The `index_baseline` job (M9, Task 13): a sealed scan becomes its fingerprint index."""

import json
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import structlog
from sqlalchemy import text

from core import queue
from core.baselines.store import BaselineStore
from core.db import models
from core.errors import InfraError
from core.logging import configure_logging
from core.settings import Settings
from tests.baseline_flow import entry, make_part
from tests.factories import make_baseline, make_device, make_event, make_team
from tests.helpers import Sessions, claimed, expire_lease, job_row
from tests.priorwork_fakes import block
from worker.index_job import in_pool, make_index_handler

KEY_HEX = "ab" * 32
KEY = bytes.fromhex(KEY_HEX)


@pytest.fixture
def pool() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor(1) as executor:
        yield executor


def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        host_artifact_dir=tmp_path,
        baseline_key=KEY_HEX,
        llm_mode="replay",
        repo_provider="local",
    )


async def sealed(
    sessions: Sessions, tmp_path: Path, parts: list[bytes], status: str = "sealed"
) -> uuid.UUID:
    async with sessions.begin() as s:
        event = make_event()
        s.add(event)
        await s.flush()
        team = make_team(event.id)
        s.add(team)
        await s.flush()
        device = make_device(team.id)
        s.add(device)
        await s.flush()
        baseline = make_baseline(
            device.id, team.id, status, complete=True, incomplete_reasons=[],
            parts=[{"n": n, "sha256": "x", "bytes": len(p)} for n, p in enumerate(parts, 1)],
        )  # fmt: skip
        s.add(baseline)
    store = BaselineStore(tmp_path, KEY)
    for n, part in enumerate(parts, start=1):
        store.write_part(baseline.id, n, part)
    return baseline.id


async def run(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor, baseline_id: uuid.UUID,
    **claim: Any,
) -> tuple[Any, queue.Job]:  # fmt: skip
    job = await claimed(sessions, "index_baseline", payload={"baseline_id": str(baseline_id)})
    handler = make_index_handler(sessions, pool, settings(tmp_path))
    return await handler(job), job


async def row(sessions: Sessions, baseline_id: uuid.UUID) -> models.Baseline:
    async with sessions() as s:
        found = await s.get(models.Baseline, baseline_id)
    assert found is not None
    return found


async def test_a_sealed_baseline_becomes_indexed_with_its_settings_and_summary(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    baseline_id = await sealed(
        sessions,
        tmp_path,
        [make_part({"a.js": block(1).encode()}), make_part({"b.py": b"x = 1\n"})],
    )
    outcome, _ = await run(sessions, tmp_path, pool, baseline_id)

    stored = await row(sessions, baseline_id)
    assert outcome is None and stored.status == "indexed"
    assert stored.index_settings == {"k": 25, "w": 16, "lexer_version": "1", "hash_version": "1"}
    assert stored.index_summary["files"] == 2 and stored.index_summary["contents"] == 2
    assert stored.index_summary["missing_content"] == 0 and stored.complete is True
    assert (tmp_path / "baselines" / str(baseline_id) / "index.sqlite").is_file()


async def test_a_baseline_that_is_not_sealed_is_left_alone(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    baseline_id = await sealed(sessions, tmp_path, [make_part({"a.js": b"a"})], "superseded")
    outcome, _ = await run(sessions, tmp_path, pool, baseline_id)
    assert outcome is not None and outcome.error_class == "validation"
    assert (await row(sessions, baseline_id)).status == "superseded"
    assert not (tmp_path / "baselines" / str(baseline_id) / "index.sqlite").exists()


async def test_running_the_job_twice_changes_nothing(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    baseline_id = await sealed(sessions, tmp_path, [make_part({"a.js": block(2).encode()})])
    await run(sessions, tmp_path, pool, baseline_id)
    first = await row(sessions, baseline_id)
    index = tmp_path / "baselines" / str(baseline_id) / "index.sqlite"
    built = index.stat().st_mtime_ns

    job = await claimed(sessions, "index_baseline", payload={"baseline_id": str(baseline_id)})
    outcome = await make_index_handler(sessions, pool, settings(tmp_path))(job)

    again = await row(sessions, baseline_id)
    assert outcome is None and index.stat().st_mtime_ns == built
    assert (again.status, again.index_summary) == ("indexed", first.index_summary)


async def test_a_listed_file_that_never_came_makes_the_baseline_incomplete(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    lost = [entry("lost.js", b"never"), entry("gone.js", b"sent")]
    part = make_part({"a.js": b"a"}, extra_entries=lost)
    baseline_id = await sealed(sessions, tmp_path, [part])
    await run(sessions, tmp_path, pool, baseline_id)
    stored = await row(sessions, baseline_id)
    assert stored.complete is False
    assert stored.incomplete_reasons == ["2 files were listed but not uploaded"]


async def test_a_corrupt_part_is_retried_then_fails_the_baseline(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    baseline_id = await sealed(sessions, tmp_path, [make_part({"a.js": b"a"})])
    sealed_part = tmp_path / "baselines" / str(baseline_id) / "parts" / "1.bin"
    damaged = bytearray(sealed_part.read_bytes())
    damaged[20] ^= 1
    sealed_part.write_bytes(bytes(damaged))
    handler = make_index_handler(sessions, pool, settings(tmp_path))

    job = await claimed(sessions, "index_baseline", payload={"baseline_id": str(baseline_id)})
    with pytest.raises(InfraError):
        await handler(job)
    assert (await row(sessions, baseline_id)).status == "sealed"

    last = job.model_copy(update={"attempts": job.max_attempts})
    outcome = await handler(last)

    stored = await row(sessions, baseline_id)
    assert outcome is not None and outcome.error_class == "app"
    assert stored.status == "failed" and "decrypt" in (stored.error_detail or "")


async def test_the_lease_is_extended_while_the_pool_works(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor
) -> None:
    job = await claimed(sessions, "index_baseline", payload={})
    async with sessions.begin() as s:
        await s.execute(
            text("UPDATE jobs SET lease_until = now() + interval '5 seconds' WHERE id = :id"),
            {"id": job.id},
        )

    def slow(value: int) -> int:
        time.sleep(0.3)
        return value * 2

    assert await in_pool(pool, sessions, job, slow, 21, every_s=0.05) == 42
    async with sessions() as s:
        left = await s.scalar(
            text("SELECT extract(epoch FROM lease_until - now()) FROM jobs WHERE id = :id"),
            {"id": job.id},
        )
    assert float(left) > 500  # renewed to the kind's 600 s

    await expire_lease(sessions, job.id)
    assert (await job_row(sessions, job.id)).status == "running"


async def test_no_path_or_code_reaches_the_log(
    sessions: Sessions, tmp_path: Path, pool: ThreadPoolExecutor, capsys: pytest.CaptureFixture[str]
) -> None:
    secret_path, secret_code = "projects/secret-plans/launch.js", "launchMissiles(42)"
    baseline_id = await sealed(
        sessions, tmp_path, [make_part({secret_path: (secret_code + block(3)).encode()})]
    )
    configure_logging(service="worker", level="INFO")
    try:
        await run(sessions, tmp_path, pool, baseline_id)
    finally:
        structlog.reset_defaults()
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    assert any(line.get("event") == "baseline_indexed" for line in lines)
    assert "secret-plans" not in out and secret_code not in out and str(tmp_path) not in out
