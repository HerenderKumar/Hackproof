"""`make fixtures`: the fixture suite (section 20.1).

Each app variant goes through the real pipeline: its repository under `LOCAL_REMOTES_DIR` is
snapshotted by the local provider, the archive is built and run with the startup check, and
the build and startup stages are compared with `expected.json`. Tests, audit and brief join
in later milestones. Runs inside the worker image (Railpack, Docker, the runner image).

`--list` only lists the suite. `--only F08,F06-fastapi-distance-broken` runs the variants whose
fixture ID or repository name starts with one of the given names.
"""

import argparse
import asyncio
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from core.clock import SystemClock
from core.git import run_git
from core.logging import configure_logging
from core.providers.local import LocalGitProvider
from core.settings import Settings, get_settings
from scripts.fixtures import (
    Fixture,
    FixtureError,
    Variant,
    load_fixtures,
    load_history_fixtures,
)
from worker.run_job import RunReport, build_and_run, startup_spec
from worker.runtime import DockerRuntime
from worker.sandbox import SandboxLimits

# Fixture "teams" get stable IDs, so their images are named the same way on every run.
_FIXTURE_TEAMS = uuid.UUID("8f0f1d1e-6a52-4a51-9b9e-5c1a2f1f0000")


@dataclass(frozen=True)
class Stages:
    build: str  # ok | fail
    startup: str  # ok | fail | skipped (the build failed)
    detail: str
    log_path: str = ""
    notes: str = ""  # a fallback the run needed, or a port other than 8080


Pipeline = Callable[[Fixture, Variant], Awaitable[Stages]]


def select_variants(fixtures: list[Fixture], only: list[str]) -> list[tuple[Fixture, Variant]]:
    variants = [(f, v) for f in fixtures for v in f.variants]
    if not only:
        return variants

    def wanted(fixture: Fixture, variant: Variant, name: str) -> bool:
        return name == fixture.id or variant.remote_name.startswith(name)

    unmatched = [name for name in only if not any(wanted(f, v, name) for f, v in variants)]
    if unmatched:
        raise ValueError(f"no fixture matches {', '.join(unmatched)}")
    return [(f, v) for f, v in variants if any(wanted(f, v, name) for name in only)]


def stages_of(report: RunReport) -> Stages:
    if report.run is None:
        detail = report.build.detail or ""
        return Stages(report.build.status, "skipped", detail, str(report.build.log_path))
    run = report.run
    notes = []
    if run.flags_used.get("root_fallback"):
        notes.append("root fallback")
    port = run.flags_used.get("port")
    if port not in (None, 8080):
        notes.append(f"port {port}")
    return Stages(
        report.build.status,
        run.startup_status,
        run.startup_detail,
        str(run.app_log_path),
        ", ".join(notes),
    )


def compare(expected: dict[str, Any], stages: Stages) -> list[str]:
    differences = []
    for stage in ("build", "startup"):
        actual = getattr(stages, stage)
        if expected[stage] != actual:
            differences.append(f"{stage}: expected {expected[stage]}, got {actual}")
    return differences


async def run_suite(selected: list[tuple[Fixture, Variant]], pipeline: Pipeline) -> int:
    width = max(len(variant.remote_name) for _, variant in selected)
    matched = 0
    for fixture, variant in selected:
        name = f"{variant.remote_name:<{width}}"
        started = time.monotonic()
        try:
            stages = await pipeline(fixture, variant)
        except Exception as exc:
            print(f"{name}  ERROR     {type(exc).__name__}: {exc}", flush=True)
            continue
        seconds = time.monotonic() - started
        differences = compare(variant.expected, stages)
        verdict = "match" if not differences else "MISMATCH"
        notes = f" [{stages.notes}]" if stages.notes else ""
        print(
            f"{name}  build={stages.build:<4} startup={stages.startup:<7} {seconds:4.0f}s"
            f"  {verdict:<8}  {stages.detail}{notes}",
            flush=True,
        )
        for difference in differences:
            print(f"    {difference}; log: {stages.log_path}", flush=True)
        matched += not differences
    print(f"{matched} of {len(selected)} variants match expected.json")
    return 0 if matched == len(selected) else 1


def real_pipeline(settings: Settings) -> Pipeline:
    runtime = DockerRuntime()
    provider = LocalGitProvider(settings.local_remotes_dir, SystemClock())
    limits = SandboxLimits.from_settings(settings)

    async def pipeline(fixture: Fixture, variant: Variant) -> Stages:
        repo = await provider.describe(f"local://{variant.remote_name}")
        git_dir = settings.local_remotes_dir / f"{variant.remote_name}.git"
        head = await run_git("rev-parse", f"{repo.default_branch}^{{commit}}", git_dir=git_dir)
        snapshot = await provider.snapshot(repo, head, settings.artifact_dir / "archives")
        report = await build_and_run(
            archive_path=snapshot.archive_path,
            archive_sha256=snapshot.archive_sha256,
            team_id=uuid.uuid5(_FIXTURE_TEAMS, variant.remote_name),
            attempt_id=uuid.uuid4(),
            mode="final",
            specs=[startup_spec(fixture.health_path)],
            artifact_dir=settings.artifact_dir,
            runtime=runtime,
            build_timeout_s=settings.build_timeout_s,
            buildkit_host=settings.buildkit_host,
            railpack_version=settings.railpack_version,
            limits=limits,
        )
        return stages_of(report)

    return pipeline


def list_suite(fixtures: list[Fixture]) -> int:
    histories = load_history_fixtures()
    variants = [(f, v) for f in fixtures for v in f.variants]
    width = max(len(v.remote_name) for _, v in variants)
    for fixture, variant in variants:
        expected = variant.expected
        verdicts = " ".join(f"{c}:{r}" for c, r in sorted(expected["verdicts"].items()))
        print(
            f"{variant.remote_name:<{width}}  {fixture.stack:<10}  {fixture.build_method:<10}"
            f"  build={expected['build']} startup={expected['startup']}  {verdicts}"
        )
    for history in histories:
        print(
            f"{history.slug:<{width}}  {'history':<10}  {f'{len(history.pushes)} pushes':<10}"
            f"  signals: {', '.join(history.expected_signals)}"
        )
    print(f"{len(variants)} app variants, {len(histories)} history fixtures")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.run_fixtures")
    parser.add_argument("--list", action="store_true", help="list the fixtures and stop")
    parser.add_argument(
        "--only", default="", help="comma-separated fixture IDs or repository names to run"
    )
    args = parser.parse_args(argv)

    try:
        fixtures = load_fixtures()
        if args.list:
            return list_suite(fixtures)
        selected = select_variants(fixtures, [n for n in args.only.split(",") if n])
    except (FixtureError, ValueError) as exc:
        print(f"fixture error: {exc}", file=sys.stderr)
        return 1

    settings = get_settings()
    configure_logging(service="fixtures", level="WARNING")  # the table is the output
    return asyncio.run(run_suite(selected, real_pipeline(settings)))


if __name__ == "__main__":
    sys.exit(main())
