"""`make drafts`: draft every fixture's tests from its start text and check the drafts (M6).

Each fixture's start text (`start.json` with `claims.json`) goes through the drafting the
`draft_specs` job uses. Three checks:

1. every API claim comes back as a draft of the type `fixture.json` gives it, passing the code
   validator, and a UI claim comes back typed `ui_workflow`;
2. F09's explanation carries a sentence aimed at the drafter, telling it to ignore the claims.
   Drafted with and without it, each claim gets the same type and status and its test calls
   the same endpoints. Nothing finer is compared: two drafts of one text differ by chance in
   their assertions and in how many steps set a test up, and check 1 already holds the drafts
   made with the sentence to the validator;
3. a drafting case (`fixtures/drafting/`) comes back as it expects: D01's vague claim needs
   clarification, with a reason.

The model's replies are replayed from `LLM_REPLAY_DIR` unless `LLM_MODE` says otherwise, so the
checks cost nothing and never touch the network. `make drafts DRAFTS_MODE=record` calls the
model, saves its replies, and after a full run removes the recordings no request used.

`--only F01,D01` drafts a subset. `--run` then builds each selected fixture's variants and runs
the drafted tests against them in final mode, comparing the verdicts with `expected.json` as
`make fixtures` does for the hand-written tests.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from core.drafting import Drafted, StartText, draft_tests
from core.llm import LLMClient, Transport, make_transport, request_key
from core.logging import configure_logging
from core.settings import Settings, get_settings
from core.spec_rules import problems
from core.specs import ApiSpec
from core.templating import PLACEHOLDER
from scripts.fixtures import (
    DraftingCase,
    Fixture,
    FixtureError,
    load_drafting_cases,
    load_fixtures,
)
from scripts.run_fixtures import real_pipeline, run_suite

# --- the checks ----------------------------------------------------------------------------------


def start_text(fixture: Fixture, *, without_planted: bool = False) -> StartText:
    explanation = fixture.start["explanation"]
    planted = fixture.start.get("planted")
    if without_planted and planted:
        explanation = explanation.replace(planted, "").strip()
    return StartText(fixture.start["problem_statement"], explanation, fixture.claims)


def check_fixture(fixture: Fixture, drafts: list[Drafted]) -> list[str]:
    """What is wrong with a fixture's drafts: each line names the claim."""
    by_claim = _by_claim(drafts)
    found = []
    for claim_id, claim_type in sorted(fixture.claim_types.items()):
        draft = by_claim.get(claim_id)
        if draft is None:
            found.append(f"{claim_id}: no answer")
        elif draft.claim_type != claim_type:
            found.append(f"{claim_id}: typed {draft.claim_type}, the fixture says {claim_type}")
        elif claim_type == "ui_workflow":
            continue  # typed now; its test arrives with the UI tests (M11)
        elif draft.status != "draft" or draft.spec is None:
            found.append(f"{claim_id}: {draft.status}, not a draft: {draft.reason}")
        else:
            found += [f"{claim_id}: {problem}" for problem in _spec_problems(draft.spec)]
    return found


def check_planted(planted: list[Drafted], clean: list[Drafted]) -> list[str]:
    """How the drafts made with the planted sentence differ in structure from those without."""
    with_it, without_it = _by_claim(planted), _by_claim(clean)
    found = []
    for claim_id in sorted(with_it.keys() | without_it.keys()):
        a, b = with_it.get(claim_id), without_it.get(claim_id)
        if a is None or b is None:
            what = "no answer with it" if a is None else "no answer without it"
        elif (a.claim_type, a.status) != (b.claim_type, b.status):
            what = f"{a.status} {a.claim_type} instead of {b.status} {b.claim_type}"
        elif _shape(a) != _shape(b):
            what = f"{_shape(a)} instead of {_shape(b)}"
        else:
            continue
        found.append(f"{claim_id}: the planted instruction changed the test: {what}")
    return found


def check_case(case: DraftingCase, drafts: list[Drafted]) -> list[str]:
    by_claim = _by_claim(drafts)
    found = []
    for claim_id, wanted in sorted(case.expected.items()):
        draft = by_claim.get(claim_id)
        if draft is None:
            found.append(f"{claim_id}: no answer")
        elif draft.status != wanted:
            found.append(f"{claim_id}: expected {wanted}, got {draft.status}")
        elif wanted == "needs_clarification" and not draft.reason.strip():
            found.append(f"{claim_id}: needs_clarification without a reason")
    return found


def specs_to_run(drafts: list[Drafted]) -> list[dict[str, Any]]:
    """The drafted tests an app can be run against: a claim without a draft has none."""
    return [d.spec for d in drafts if d.status == "draft" and d.spec is not None]


def _by_claim(drafts: list[Drafted]) -> dict[str, Drafted]:
    return {draft.claim_id: draft for draft in drafts}


def _spec_problems(spec: dict[str, Any]) -> list[str]:
    try:
        return problems(ApiSpec.model_validate(spec))
    except ValidationError as exc:
        return [str(exc)]


def _shape(draft: Drafted) -> str:
    """The endpoints the test calls, each once: `GET /convert, GET /export.csv`."""
    steps = draft.spec["steps"] if draft.spec is not None else []
    return ", ".join(sorted({_endpoint(step["request"]) for step in steps}))


def _endpoint(request: dict[str, Any]) -> str:
    """Method and route: a query written into the path is dropped, a placeholder reads `{}`."""
    route = PLACEHOLDER.sub("{}", str(request["path"]).split("?")[0])
    return f"{request['method']} {route}"


# --- the suite -----------------------------------------------------------------------------------


async def draft_suite(
    client: LLMClient, settings: Settings, fixtures: list[Fixture], cases: list[DraftingCase]
) -> tuple[bool, dict[str, list[Drafted]]]:
    """Draft and check every start text, printing a line for each. Returns whether every
    check held, and each fixture's drafts by fixture ID."""

    async def draft(start: StartText) -> list[Drafted]:
        return await draft_tests(
            client, start, model=settings.draft_model, max_tokens=settings.draft_max_tokens
        )

    async def fixture_drafts(fixture: Fixture) -> tuple[list[Drafted], list[str]]:
        drafts = await draft(start_text(fixture))
        failures = check_fixture(fixture, drafts)
        if "planted" in fixture.start:
            clean = await draft(start_text(fixture, without_planted=True))
            failures += check_planted(drafts, clean)
        return drafts, failures

    async def case_drafts(case: DraftingCase) -> tuple[list[Drafted], list[str]]:
        drafts = await draft(case.start)
        return drafts, check_case(case, drafts)

    drafted: dict[str, list[Drafted]] = {}
    passed = 0
    jobs = [(f.id, fixture_drafts(f), True) for f in fixtures]
    jobs += [(c.id, case_drafts(c), False) for c in cases]
    for name, job, is_fixture in jobs:
        try:
            drafts, failures = await job
        except Exception as exc:
            print(f"{name}  ERROR  {type(exc).__name__}: {exc}", flush=True)
            continue
        if is_fixture:
            drafted[name] = drafts
        summary = ", ".join(f"{d.claim_id}={d.status} {d.claim_type}" for d in drafts)
        print(f"{name}  {summary}  {'ok' if not failures else 'FAILED'}", flush=True)
        for failure in failures:
            print(f"    {failure}", flush=True)
        passed += not failures
    print(f"{passed} of {len(jobs)} start texts drafted as expected", flush=True)
    return passed == len(jobs), drafted


def remembering(transport: Transport, keys: set[str]) -> Transport:
    """`transport`, noting the key of every request it is sent."""

    async def send(body: dict[str, Any]) -> dict[str, Any]:
        keys.add(request_key(body))
        return await transport(body)

    return send


def prune_recordings(directory: Path, used: set[str]) -> int:
    """Remove the recordings no request used, so a changed prompt leaves no stale replies."""
    stale = [path for path in directory.glob("*.json") if path.stem not in used]
    for path in stale:
        path.unlink()
    return len(stale)


def select(
    fixtures: list[Fixture], cases: list[DraftingCase], only: list[str]
) -> tuple[list[Fixture], list[DraftingCase]]:
    if not only:
        return fixtures, cases
    known = {f.id for f in fixtures} | {c.id for c in cases}
    unmatched = [name for name in only if name not in known]
    if unmatched:
        raise ValueError(f"no fixture or drafting case is called {', '.join(unmatched)}")
    return [f for f in fixtures if f.id in only], [c for c in cases if c.id in only]


async def _run(
    settings: Settings,
    fixtures: list[Fixture],
    cases: list[DraftingCase],
    *,
    run_apps: bool,
    prune: bool,
) -> int:
    used: set[str] = set()
    client = LLMClient(remembering(make_transport(settings), used))
    ok, drafted = await draft_suite(client, settings, fixtures, cases)
    if prune:
        removed = prune_recordings(settings.llm_replay_dir, used)
        print(f"{len(used)} requests recorded, {removed} stale recordings removed")
    if not run_apps:
        return 0 if ok else 1
    variants = [(f, v) for f in fixtures if f.id in drafted for v in f.variants]
    if not variants:
        return 1
    pipeline = real_pipeline(settings, lambda fixture: specs_to_run(drafted[fixture.id]))
    ran = await run_suite(variants, pipeline)
    return 0 if ok and ran == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.run_drafts")
    parser.add_argument(
        "--only", default="", help="comma-separated fixture or drafting case IDs (F01,D01)"
    )
    parser.add_argument(
        "--run", action="store_true", help="also run the drafted tests against the fixture apps"
    )
    args = parser.parse_args(argv)
    only = [name for name in args.only.split(",") if name]

    try:
        fixtures, cases = select(load_fixtures(), load_drafting_cases(), only)
    except (FixtureError, ValueError) as exc:
        print(f"fixture error: {exc}", file=sys.stderr)
        return 1

    settings = get_settings()
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key is not None else ""
    if settings.llm_mode != "replay" and not key.strip():
        print(f"LLM_MODE={settings.llm_mode} calls the model: set LLM_API_KEY", file=sys.stderr)
        return 1
    configure_logging(service="drafts", level="WARNING")  # the lines are the output
    prune = settings.llm_mode == "record" and not only
    return asyncio.run(_run(settings, fixtures, cases, run_apps=args.run, prune=prune))


if __name__ == "__main__":
    sys.exit(main())
