"""`make audits`: audit every fixture variant from its own files and compare (M8).

Each variant's files (those that go into its repository) are archived as a snapshot would be,
extracted with the snapshot's checks, and audited through `worker.audit_job.audit_folder`: the
tool loop, citation validation, Graphify and the code map, as the `audit` job runs them. Per
claim, the implementation is compared with the variant's `audit` in `expected.json`; a claim
the model did not answer reads `missing`. A kept explanation holding a phrase of the variant's
`brief_must_not_contain` is a difference too: F09's README carries a line aimed at an AI
reviewer, asking for exactly those words.

The step and token budgets are the job's. The time budget is not applied: it reads the wall
clock, so a recording that hit it would ask a different last question from its replay. Each
line shows the audit's time instead, flagged when it is over `AUDIT_TIMEOUT_S`.

Replies are replayed from `LLM_REPLAY_DIR` unless `LLM_MODE` says otherwise, so the suite costs
nothing and never touches the network. `make audits AUDITS_MODE=record` calls the model, saves
its replies, and after a full run removes the audit recordings no request used; the drafting
recordings in the same folder are left alone. `--only F01,F09-flask-injection-good` audits a
subset. A request already sent in a run gets the reply it got then (`answering_once`), so
re-record a fixture whole (`--only F03`), not one of its variants: its variants may share their
first request, and recording one alone would replace the answer the other replays.
"""

import argparse
import asyncio
import copy
import math
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.audit import SUBMIT_AUDIT, AuditInput
from core.claims import claim_fields
from core.codemap import run_graphify
from core.hashing import sha256_hex
from core.llm import LLMClient, Transport, make_transport, request_key
from core.logging import configure_logging
from core.settings import Settings, get_settings
from core.snapshots import extract_archive
from scripts.fixtures import Fixture, FixtureError, Variant, load_fixtures
from scripts.run_drafts import prune_recordings, remembering
from scripts.run_fixtures import select_variants
from worker.audit_job import Graphify, audit_folder, budget_of

# What the model is told about the event: fixed, so a replay asks what the recording asked.
EVENT_NAME = "HackProof fixture suite"
KICKOFF_AT = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)


@dataclass(frozen=True)
class VariantAudit:
    results: dict[str, str]  # claim ID -> implementation, or "missing"
    explanations: dict[str, str]
    citations_kept: int
    citations_dropped: int
    hints: int
    code_map_status: str
    partial: bool
    failure: str | None
    steps: int
    tokens: int


def archive_variant(variant: Variant, directory: Path) -> tuple[Path, str]:
    """A `.tar.gz` of the variant's repository files, as a snapshot holds them."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "snapshot.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        for file in variant.files():
            tar.add(file, arcname=file.relative_to(variant.path).as_posix(), recursive=False)
    return path, sha256_hex(path.read_bytes())


def audit_input(fixture: Fixture) -> AuditInput:
    return AuditInput(
        event_name=EVENT_NAME,
        kickoff_at=KICKOFF_AT,
        team_name=fixture.title,
        problem_statement=fixture.start.get("problem_statement", ""),
        claims=[claim_fields(claim) for claim in fixture.claims],
    )


async def audit_variant(
    client: LLMClient,
    settings: Settings,
    fixture: Fixture,
    variant: Variant,
    work: Path,
    *,
    graphify: Graphify = run_graphify,
) -> VariantAudit:
    archive, digest = await asyncio.to_thread(archive_variant, variant, work / "archive")
    src = work / "src"
    await asyncio.to_thread(extract_archive, archive, digest, src)
    done = await audit_folder(
        client,
        src,
        audit_input(fixture),
        model=settings.audit_model,
        budget=replace(budget_of(settings), timeout_s=math.inf),
        graphify=graphify,
        ui_specs={spec["claim_id"]: spec for spec in fixture.specs if spec.get("kind") == "ui"},
    )
    content = done.validated.content
    answered = {claim["claim_id"]: claim for claim in content["claims"]}
    return VariantAudit(
        results={
            claim_id: answered[claim_id]["implementation"] if claim_id in answered else "missing"
            for claim_id in sorted(fixture.claim_types)
        },
        explanations={claim_id: claim["explanation"] for claim_id, claim in answered.items()},
        citations_kept=sum(len(claim["citations"]) for claim in content["claims"]),
        citations_dropped=done.validated.citations_dropped,
        hints=len(content["text_hints"]),
        code_map_status=done.code_map_status,
        partial=done.ran.partial,
        failure=done.ran.failure,
        steps=done.ran.steps,
        tokens=done.ran.tokens,
    )


def differences(variant: Variant, audited: VariantAudit) -> list[str]:
    """How the audit differs from `expected.json`: each line names the claim."""
    found = [
        f"{claim_id}: {audited.results.get(claim_id, 'missing')}, expected {wanted}"
        for claim_id, wanted in sorted(variant.expected["audit"].items())
        if audited.results.get(claim_id, "missing") != wanted
    ]
    phrases = [phrase.lower() for phrase in variant.expected["brief_must_not_contain"]]
    for claim_id, text in sorted(audited.explanations.items()):
        found += [
            f"{claim_id}: the explanation says {phrase!r}"
            for phrase in phrases
            if phrase in text.lower()
        ]
    if audited.failure:
        found.append(f"the audit failed: {audited.failure}")
    return found


def summary(variant: Variant, audited: VariantAudit, seconds: float, timeout_s: float) -> str:
    claims = "  ".join(
        f"{claim_id} {got} (expected {variant.expected['audit'].get(claim_id, '?')})"
        for claim_id, got in audited.results.items()
    )
    slow = f" (over the job's {timeout_s:g} s)" if seconds > timeout_s else ""
    return (
        f"{claims}  citations {audited.citations_kept} kept {audited.citations_dropped} dropped"
        f"  hints {audited.hints}  code map {audited.code_map_status}"
        f"  {audited.steps} steps {audited.tokens} tokens {seconds:.0f} s{slow}"
        + ("  partial" if audited.partial else "")
    )


async def audit_suite(
    client: LLMClient,
    settings: Settings,
    selected: list[tuple[Fixture, Variant]],
    *,
    graphify: Graphify = run_graphify,
) -> bool:
    """Audit and compare every selected variant, printing a line for each. True when every
    variant was audited as expected."""
    names = {variant.remote_name: f"{fixture.id} {variant.name}" for fixture, variant in selected}
    width = max((len(name) for name in names.values()), default=0)
    matched = 0
    for fixture, variant in selected:
        name = f"{names[variant.remote_name]:<{width}}"
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="hp-audit-") as work:
                audited = await audit_variant(
                    client, settings, fixture, variant, Path(work), graphify=graphify
                )
        except Exception as exc:
            print(f"{name}  ERROR  {type(exc).__name__}: {exc}", flush=True)
            continue
        found = differences(variant, audited)
        line = summary(variant, audited, time.monotonic() - started, settings.audit_timeout_s)
        print(f"{name}  {line}  {'ok' if not found else 'DIFFERS'}", flush=True)
        for difference in found:
            print(f"    {difference}", flush=True)
        matched += not found
    print(f"{matched} of {len(selected)} variants audited as expected", flush=True)
    return matched == len(selected)


def answering_once(transport: Transport) -> Transport:
    """`transport`, except that a request already sent in this run gets the reply it got then.

    Two variants whose files differ only inside a line of the same length (F03's pattern, F05's
    button type) list the same names and sizes, so the model's first request is the same for
    both. Recorded separately, the second reply would overwrite the first under the same key,
    and one variant's replay would then ask a question that was never recorded.
    """
    replies: dict[str, dict[str, Any]] = {}

    async def send(body: dict[str, Any]) -> dict[str, Any]:
        key = request_key(body)
        if key not in replies:
            replies[key] = await transport(body)
        return copy.deepcopy(replies[key])

    return send


async def _run(
    settings: Settings,
    selected: list[tuple[Fixture, Variant]],
    *,
    prune: bool,
    graphify: Graphify = run_graphify,
) -> int:
    used: set[str] = set()
    client = LLMClient(answering_once(remembering(make_transport(settings), used)))
    ok = await audit_suite(client, settings, selected, graphify=graphify)
    if prune:
        removed = prune_recordings(settings.llm_replay_dir, used, SUBMIT_AUDIT.name)
        print(f"{len(used)} requests recorded, {removed} stale audit recordings removed")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.run_audits")
    parser.add_argument(
        "--only", default="", help="comma-separated fixture IDs or variant names (F01,F09-…-good)"
    )
    args = parser.parse_args(argv)
    only = [name for name in args.only.split(",") if name]

    try:
        selected = select_variants(load_fixtures(), only)
    except (FixtureError, ValueError) as exc:
        print(f"fixture error: {exc}", file=sys.stderr)
        return 1

    settings = get_settings()
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key is not None else ""
    if settings.llm_mode != "replay" and not key.strip():
        print(f"LLM_MODE={settings.llm_mode} calls the model: set LLM_API_KEY", file=sys.stderr)
        return 1
    configure_logging(service="audits", level="WARNING")  # the lines are the output
    prune = settings.llm_mode == "record" and not only
    return asyncio.run(_run(settings, selected, prune=prune))


if __name__ == "__main__":
    sys.exit(main())
