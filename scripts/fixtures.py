"""The fixture suite on disk (section 20.2), loaded and checked before anything runs.

    fixtures/F01-node-splitter/{good,broken}/   the app and its broken copy
    fixtures/F01-node-splitter/fixture.json     title, stack, build method, claim types
    fixtures/F01-node-splitter/claims.json      the team's claims
    fixtures/F01-node-splitter/expected.json    the expected result of every stage, per variant
    fixtures/history/H1-bulk-push/timeline.json a scripted repository history

A malformed fixture raises `FixtureError` naming the file and the problem, so the suite never
half-runs against expectations it cannot compare.
"""

import dataclasses
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from core.specs import ApiSpec

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"

VARIANTS = ("good", "broken")
STACKS = {"node", "python", "static", "go", "dockerfile"}
BUILD_METHODS = {"railpack", "dockerfile"}
CLAIM_TYPES = {"calculation", "create_update", "classification", "authentication", "ui_workflow"}
BUILD_RESULTS = {"ok", "fail"}
STARTUP_RESULTS = {"ok", "fail"}
VERDICTS = {"pass", "fail", "unverified", "blocked"}  # section 6.3
AUDIT_RESULTS = {"implemented", "partial", "not_found", "unclear"}  # section 15.2
SIGNALS = {  # section 6.6
    "bulk_first_push",
    "commits_before_kickoff",
    "repo_created_before_event",
    "late_registration",
    "history_rewritten",
}
CLAIM_FIELDS = {"id", "text", "target", "example_input", "expected_behaviour"}
EXPECTED_FIELDS = {"build", "startup", "verdicts", "audit", "signals", "brief_must_not_contain"}

# Never part of a fixture repository, even if a local run left them behind.
IGNORED = {"node_modules", ".venv", "__pycache__", ".DS_Store", ".pytest_cache"}

_APP_DIR = re.compile(r"^(F\d{2})-[a-z0-9]+(?:-[a-z0-9]+)*$")
_HISTORY_DIR = re.compile(r"^(H\d)-[a-z0-9]+(?:-[a-z0-9]+)*$")
_CLAIM_ID = re.compile(r"^c[1-3]$")
_REPO_PATH = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")


class FixtureError(Exception):
    """A fixture on disk does not match the layout or vocabulary of section 20."""


@dataclass(frozen=True)
class Variant:
    fixture_id: str
    name: str
    path: Path
    remote_name: str
    expected: dict[str, Any]

    def files(self) -> list[Path]:
        """Every file that goes into the variant's repository."""
        return sorted(
            p
            for p in self.path.rglob("*")
            if p.is_file() and not IGNORED & set(p.relative_to(self.path).parts)
        )


@dataclass(frozen=True)
class Fixture:
    id: str
    slug: str
    title: str
    stack: str
    build_method: str
    health_path: str
    claim_types: dict[str, str]
    claims: list[dict[str, str]]
    variants: list[Variant]
    # Hand-written API specs (`specs.json`), one per claim that is not a UI claim (M5).
    specs: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def variant(self, name: str) -> Variant:
        for variant in self.variants:
            if variant.name == name:
                return variant
        raise KeyError(f"{self.slug} has no {name} variant")


@dataclass(frozen=True)
class HistoryCommit:
    name: str
    parent: str | None
    authored: int  # minutes from kickoff; negative is before it
    committed: int
    message: str
    files: dict[str, bytes | None]  # None deletes the path


@dataclass(frozen=True)
class Push:
    at: int  # minutes from kickoff
    head: str  # a commit name


@dataclass(frozen=True)
class HistoryFixture:
    id: str
    slug: str
    title: str
    expected_signals: list[str]
    commits: list[HistoryCommit]
    pushes: list[Push]


# --- app fixtures -------------------------------------------------------------------------------


def load_fixtures(root: Path = FIXTURES_DIR) -> list[Fixture]:
    directories = sorted(p for p in root.iterdir() if p.is_dir() and _APP_DIR.match(p.name))
    fixtures = [_load_fixture(directory) for directory in directories]
    ids = [f.id for f in fixtures]
    if len(set(ids)) != len(ids):
        raise FixtureError(f"two fixture directories share an id: {sorted(ids)}")
    return fixtures


def _load_fixture(directory: Path) -> Fixture:
    match = _APP_DIR.match(directory.name)
    assert match
    fixture_id, slug = match.group(1), directory.name

    meta = _read_json(directory / "fixture.json", dict)
    _require_keys(
        directory / "fixture.json",
        meta,
        {"title", "stack", "build_method", "claim_types"},
        {"health_path"},
    )
    title, stack, build_method = meta["title"], meta["stack"], meta["build_method"]
    health_path = meta.get("health_path", "/")
    claim_types = meta["claim_types"]
    where = f"{slug}/fixture.json"
    if not isinstance(title, str) or not title.strip():
        raise FixtureError(f"{where}: title must be a non-empty string")
    if stack not in STACKS:
        raise FixtureError(f"{where}: stack {stack!r} is not one of {sorted(STACKS)}")
    if build_method not in BUILD_METHODS:
        raise FixtureError(
            f"{where}: build_method {build_method!r} is not one of {sorted(BUILD_METHODS)}"
        )
    if (
        not isinstance(health_path, str)
        or not health_path.startswith("/")
        or health_path.startswith("//")
    ):
        raise FixtureError(f"{where}: health_path must be a path starting with a single /")
    if not isinstance(claim_types, dict) or not 1 <= len(claim_types) <= 3:
        raise FixtureError(f"{where}: claim_types must map one to three claim ids to types")
    for claim_id, claim_type in claim_types.items():
        if not _CLAIM_ID.match(claim_id):
            raise FixtureError(f"{where}: claim id {claim_id!r} must be c1, c2 or c3")
        if claim_type not in CLAIM_TYPES:
            raise FixtureError(
                f"{where}: claim type {claim_type!r} is not one of {sorted(CLAIM_TYPES)}"
            )
    claim_ids = sorted(claim_types)

    claims = _load_claims(directory / "claims.json", slug, claim_ids)
    specs_file = directory / "specs.json"
    specs = _load_specs(specs_file, slug, claim_types) if specs_file.exists() else []
    expected = _read_json(directory / "expected.json", dict)

    variant_dirs = sorted(p.name for p in directory.iterdir() if p.is_dir())
    unknown = [name for name in variant_dirs if name not in VARIANTS]
    if unknown:
        raise FixtureError(
            f"{slug}: unexpected directories {unknown}; variants are {list(VARIANTS)}"
        )
    if "good" not in variant_dirs:
        raise FixtureError(f"{slug}: every fixture needs a good variant")
    for name in variant_dirs:
        if name not in expected:
            raise FixtureError(f"{slug}/expected.json has no results for the {name} variant")
    for name in expected:
        if name not in variant_dirs:
            raise FixtureError(
                f"{slug}/expected.json has results for {name!r}, which has no directory"
            )

    variants = []
    for name in (v for v in VARIANTS if v in variant_dirs):
        path = directory / name
        _check_expected(f"{slug}/expected.json {name}", expected[name], claim_ids)
        variant = Variant(fixture_id, name, path, f"{slug}-{name}", expected[name])
        if not variant.files():
            raise FixtureError(f"{slug}/{name} has no files")
        has_dockerfile = (path / "Dockerfile").is_file()
        if has_dockerfile != (build_method == "dockerfile"):
            raise FixtureError(
                f"{slug}/{name}: a Dockerfile goes with build_method dockerfile, and only with it"
            )
        variants.append(variant)

    return Fixture(
        id=fixture_id,
        slug=slug,
        title=title,
        stack=stack,
        build_method=build_method,
        health_path=health_path,
        claim_types=dict(claim_types),
        claims=claims,
        variants=variants,
        specs=specs,
    )


def _load_specs(path: Path, slug: str, claim_types: dict[str, str]) -> list[dict[str, Any]]:
    """Each spec obeys the case rules and tests one claim of the claim's own type."""
    where = f"{slug}/specs.json"
    specs, seen = [], set()
    for item in _read_json(path, list):
        try:
            spec = ApiSpec.model_validate(item)
        except ValidationError as exc:
            raise FixtureError(f"{where}: {exc}") from None
        claim_type = claim_types.get(spec.claim_id)
        if claim_type is None:
            raise FixtureError(f"{where}: fixture.json has no claim {spec.claim_id}")
        if claim_type == "ui_workflow":
            raise FixtureError(f"{where}: {spec.claim_id} is a UI claim; UI specs arrive in M11")
        if spec.claim_type != claim_type:
            raise FixtureError(
                f"{where}: {spec.claim_id} is a {claim_type} claim, not {spec.claim_type}"
            )
        if spec.claim_id in seen:
            raise FixtureError(f"{where}: one spec per claim, and {spec.claim_id} has two")
        seen.add(spec.claim_id)
        specs.append(spec.model_dump(by_alias=True))
    return specs


def _load_claims(path: Path, slug: str, claim_ids: list[str]) -> list[dict[str, str]]:
    claims = _read_json(path, list)
    where = f"{slug}/claims.json"
    for claim in claims:
        if not isinstance(claim, dict):
            raise FixtureError(f"{where}: every claim must be an object")
        _require_keys(path, claim, CLAIM_FIELDS)
        for field, value in claim.items():
            if not isinstance(value, str) or not value.strip():
                raise FixtureError(f"{where}: {claim.get('id')} {field} must be a non-empty string")
    ids = [claim["id"] for claim in claims]
    if ids != claim_ids:
        raise FixtureError(f"{where}: claims {ids} must be exactly {claim_ids}, in order")
    return claims


def _check_expected(where: str, expected: Any, claim_ids: list[str]) -> None:
    if not isinstance(expected, dict):
        raise FixtureError(f"{where}: must be an object")
    missing, extra = EXPECTED_FIELDS - set(expected), set(expected) - EXPECTED_FIELDS
    if missing or extra:
        raise FixtureError(f"{where}: missing {sorted(missing)}, unexpected {sorted(extra)}")
    if expected["build"] not in BUILD_RESULTS:
        raise FixtureError(
            f"{where}: build {expected['build']!r} is not one of {sorted(BUILD_RESULTS)}"
        )
    if expected["startup"] not in STARTUP_RESULTS:
        raise FixtureError(
            f"{where}: startup {expected['startup']!r} is not one of {sorted(STARTUP_RESULTS)}"
        )
    for stage, vocabulary in (("verdicts", VERDICTS), ("audit", AUDIT_RESULTS)):
        results = expected[stage]
        if not isinstance(results, dict):
            raise FixtureError(f"{where}: {stage} must map claim ids to results")
        missing_ids = sorted(set(claim_ids) - set(results))
        extra_ids = sorted(set(results) - set(claim_ids))
        if missing_ids:
            raise FixtureError(f"{where}: {stage} has no result for {missing_ids}")
        if extra_ids:
            raise FixtureError(
                f"{where}: {stage} names claims {extra_ids} the fixture does not have"
            )
        for claim_id, result in results.items():
            if result not in vocabulary:
                noun = "verdict" if stage == "verdicts" else stage
                raise FixtureError(
                    f"{where}: {noun} {result!r} for {claim_id} is not one of {sorted(vocabulary)}"
                )
    if expected["startup"] == "fail" and set(expected["verdicts"].values()) != {"blocked"}:
        raise FixtureError(f"{where}: an app that never starts has every verdict blocked")
    _check_signals(where, expected["signals"])
    phrases = expected["brief_must_not_contain"]
    if not isinstance(phrases, list) or not all(isinstance(p, str) and p for p in phrases):
        raise FixtureError(f"{where}: brief_must_not_contain must be a list of phrases")


# --- history fixtures ---------------------------------------------------------------------------


def load_history_fixtures(root: Path = FIXTURES_DIR) -> list[HistoryFixture]:
    history_dir = root / "history"
    if not history_dir.is_dir():
        return []
    directories = sorted(
        p for p in history_dir.iterdir() if p.is_dir() and _HISTORY_DIR.match(p.name)
    )
    return [_load_history(directory) for directory in directories]


def _load_history(directory: Path) -> HistoryFixture:
    match = _HISTORY_DIR.match(directory.name)
    assert match
    path = directory / "timeline.json"
    where = f"history/{directory.name}/timeline.json"
    timeline = _read_json(path, dict)
    _require_keys(path, timeline, {"title", "expected_signals", "commits", "pushes"})
    _check_signals(where, timeline["expected_signals"])

    commits: list[HistoryCommit] = []
    by_name: dict[str, HistoryCommit] = {}
    for raw in timeline["commits"]:
        _require_keys(
            path, raw, {"name", "parent", "authored", "committed", "message", "files"}, {"note"}
        )
        name, parent = raw["name"], raw["parent"]
        if name in by_name:
            raise FixtureError(f"{where}: commit {name!r} is defined twice")
        if parent is not None and parent not in by_name:
            raise FixtureError(
                f"{where}: commit {name!r} has parent {parent!r}, defined later or not at all"
            )
        if not all(isinstance(raw[k], int) for k in ("authored", "committed")):
            raise FixtureError(f"{where}: commit {name!r} times are whole minutes from kickoff")
        commit = HistoryCommit(
            name=name,
            parent=parent,
            authored=raw["authored"],
            committed=raw["committed"],
            message=raw["message"],
            files={p: _file_content(where, name, p, v) for p, v in raw["files"].items()},
        )
        commits.append(commit)
        by_name[name] = commit

    pushes: list[Push] = []
    for raw in timeline["pushes"]:
        _require_keys(path, raw, {"at", "head"})
        push = Push(at=raw["at"], head=raw["head"])
        if push.head not in by_name:
            raise FixtureError(f"{where}: push at {push.at} names unknown commit {push.head!r}")
        if by_name[push.head].committed > push.at:
            raise FixtureError(f"{where}: commit {push.head!r} is pushed before it was committed")
        if pushes and (push.at <= pushes[-1].at or push.head == pushes[-1].head):
            raise FixtureError(f"{where}: each push must be later than, and differ from, the last")
        pushes.append(push)
    if not pushes:
        raise FixtureError(f"{where}: a timeline needs at least one push")

    return HistoryFixture(
        id=match.group(1),
        slug=directory.name,
        title=timeline["title"],
        expected_signals=list(timeline["expected_signals"]),
        commits=commits,
        pushes=pushes,
    )


def _file_content(where: str, commit: str, path: str, value: Any) -> bytes | None:
    if not _REPO_PATH.match(path) or ".." in path.split("/"):
        raise FixtureError(f"{where}: commit {commit!r} has an unsafe path {path!r}")
    if value is None or isinstance(value, str):
        return None if value is None else value.encode()
    if isinstance(value, dict) and set(value) == {"generated_lines"}:
        count = value["generated_lines"]
        if isinstance(count, int) and count > 0:
            return "".join(
                f"export const step{n:05d} = {n};\n" for n in range(1, count + 1)
            ).encode()
    raise FixtureError(
        f'{where}: {path} in {commit!r} must be text, null, or {{"generated_lines": <n>}}'
    )


# --- shared -------------------------------------------------------------------------------------


def _check_signals(where: str, signals: Any) -> None:
    if not isinstance(signals, list) or not all(s in SIGNALS for s in signals):
        raise FixtureError(f"{where}: signals {signals!r} must be a list from {sorted(SIGNALS)}")


def _read_json[T](path: Path, kind: type[T]) -> T:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        raise FixtureError(f"{path.parent.name}/{path.name} is missing") from None
    except json.JSONDecodeError as exc:
        raise FixtureError(f"{path.parent.name}/{path.name} is not valid JSON: {exc}") from None
    if not isinstance(value, kind):
        raise FixtureError(f"{path.parent.name}/{path.name} must be a JSON {kind.__name__}")
    return value


def _require_keys(
    path: Path, value: Any, required: set[str], optional: frozenset[str] | set[str] = frozenset()
) -> None:
    where = f"{path.parent.name}/{path.name}"
    if not isinstance(value, dict):
        raise FixtureError(f"{where}: expected an object")
    missing, extra = required - set(value), set(value) - required - set(optional)
    if missing or extra:
        raise FixtureError(f"{where}: missing {sorted(missing)}, unexpected {sorted(extra)}")
