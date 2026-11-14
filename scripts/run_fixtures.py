"""`make fixtures`: the fixture suite (section 20.1).

Each app variant goes through the real pipeline: its repository under `LOCAL_REMOTES_DIR` is
snapshotted by the local provider, the archive is built and run in final mode with the
fixture's hand-written specs (`specs.json`), and the build, startup and verdict stages are
compared with `expected.json`. Verdicts come from the same mapping the run job uses. UI claims
are not compared until UI tests exist (M11); audit and brief join in later milestones. Runs
inside the worker image (Railpack, Docker, the runner image).

`--list` only lists the suite. `--only F08,F06-fastapi-distance-broken` runs the variants whose
fixture ID or repository name starts with one of the given names.
"""

import argparse
import asyncio
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field, replace
from typing import Any

from core.clock import SystemClock
from core.git import run_git
from core.logging import configure_logging
from core.providers.local import LocalGitProvider
from core.settings import Settings, get_settings
from core.specs import for_mode
from core.verdicts import claim_verdicts
from scripts.fixtures import (
    Fixture,
    FixtureError,
    Variant,
    load_fixtures,
    load_history_fixtures,
)
from worker.results import result_rows
from worker.run_job import RunReport, build_and_run, startup_spec
from worker.run_specs import RunPlan
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
    verdicts: dict[str, str] | None = None  # per claim; None when the stage did not run
    reasons: dict[str, str] = field(default_factory=dict)  # why a claim failed or is unverified

    def with_verdicts(self, verdicts: dict[str, str], reasons: dict[str, str]) -> "Stages":
        return replace(self, verdicts=verdicts, reasons=reasons)


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


def verdicts_of(report: RunReport, plan: RunPlan, claim_ids: list[str]) -> dict[str, str]:
    """Claim verdicts through the run job's own mapping (`worker.results.result_rows`)."""
    claim_of = {str(spec["spec_id"]): str(spec["claim_id"]) for spec in plan.runnable}
    pairs = [
        (claim_of[str(row["spec_id"])], row["verdict"])
        for row in result_rows(report, plan)
        if str(row["spec_id"]) in claim_of
    ]
    return dict(claim_verdicts(claim_ids, pairs))


def reasons_of(report: RunReport, plan: RunPlan) -> dict[str, str]:
    """For each claim with a row that failed or is unverified, what the first such row says.
    Blocked rows are left out: the startup detail already says why the app did not start."""
    claim_of = {str(spec["spec_id"]): str(spec["claim_id"]) for spec in plan.runnable}
    reasons: dict[str, str] = {}
    for row in result_rows(report, plan):
        claim = claim_of.get(str(row["spec_id"]))
        if claim is not None and claim not in reasons and row["verdict"] in ("fail", "unverified"):
            reasons[claim] = _reason(row)
    return reasons


def _reason(row: dict[str, Any]) -> str:
    failed = next((a for a in row["assertion_results"] or [] if not a.get("passed")), None)
    where = " ".join(str(part) for part in (row["case_id"], failed and failed.get("step")) if part)
    if failed is None:
        what = str(row["error"])
    else:
        what = " ".join(str(part) for part in (failed["op"], failed.get("path")) if part)
        what += f" expected {_short(failed['expected'])}"
        note = failed.get("note")
        what += f": {note}" if note else f", got {_short(failed['actual'])}"
    return f"{where}: {what}" if where else what


def _short(value: Any, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def compare(
    expected: dict[str, Any], stages: Stages, *, ui_claims: Collection[str] = frozenset()
) -> list[str]:
    differences = []
    for stage in ("build", "startup"):
        actual = getattr(stages, stage)
        if expected[stage] != actual:
            differences.append(f"{stage}: expected {expected[stage]}, got {actual}")
    if stages.verdicts is not None:
        for claim, wanted in sorted(expected["verdicts"].items()):
            got = stages.verdicts.get(claim)
            if claim not in ui_claims and got != wanted:
                differences.append(f"{claim}: expected {wanted}, got {got}")
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
        ui_claims = {c for c, kind in fixture.claim_types.items() if kind == "ui_workflow"}
        differences = compare(variant.expected, stages, ui_claims=ui_claims)
        verdict = "match" if not differences else "MISMATCH"
        notes = [stages.notes] if stages.notes else []
        if stages.verdicts is not None:
            shown = {c: v for c, v in sorted(stages.verdicts.items()) if c not in ui_claims}
            for claim, value in shown.items():
                why = stages.reasons.get(claim)
                notes.append(f"{claim}={value} ({why})" if why else f"{claim}={value}")
            notes += ["UI tests arrive in M11"] if ui_claims else []
        noted = f" [{'; '.join(notes)}]" if notes else ""
        print(
            f"{name}  build={stages.build:<4} startup={stages.startup:<7} {seconds:4.0f}s"
            f"  {verdict:<8}  {stages.detail}{noted}",
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
        plan = RunPlan(
            "final",
            [
                for_mode(spec, "final")
                | {"spec_id": str(uuid.uuid5(_FIXTURE_TEAMS, f"{fixture.id}:{spec['claim_id']}"))}
                for spec in fixture.specs
            ],
            [],
        )
        report = await build_and_run(
            archive_path=snapshot.archive_path,
            archive_sha256=snapshot.archive_sha256,
            team_id=uuid.uuid5(_FIXTURE_TEAMS, variant.remote_name),
            attempt_id=uuid.uuid4(),
            mode="final",
            specs=[startup_spec(fixture.health_path), *plan.runnable],
            artifact_dir=settings.artifact_dir,
            runtime=runtime,
            build_timeout_s=settings.build_timeout_s,
            buildkit_host=settings.buildkit_host,
            railpack_version=settings.railpack_version,
            limits=limits,
        )
        return stages_of(report).with_verdicts(
            verdicts_of(report, plan, sorted(fixture.claim_types)), reasons_of(report, plan)
        )

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
