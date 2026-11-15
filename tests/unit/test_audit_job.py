"""The `audit` job (sections 7, 15 and 16): one validated audit and code map per attempt."""

import io
import tarfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from core import queue
from core.codemap import Graph, GraphEdge, GraphError, GraphNode
from core.db import models
from core.errors import InfraError
from core.hashing import sha256_hex
from core.llm import LLMClient
from core.settings import Settings
from tests.factories import CLAIM_ITEMS, make_claims, make_event, make_snapshot, make_submission
from tests.factories import make_team as make_team_row
from tests.helpers import Sessions, claimed
from tests.llm_fakes import Scripted, audit_reply, calls_reply, claim_entry, text_reply
from worker.audit_job import make_audit_handler
from worker.lanes import Outcome

SERVER_JS = """\
const express = require("express");
const app = express();

function split(total, people) {
  return Array(people).fill(total / people);
}

app.post("/split", (req, res) => {
  res.json({ shares: split(req.body.total, req.body.people) });
});

app.listen(8080);
"""
FILES = {"server.js": SERVER_JS, "README.md": "# Splitter\n"}

GRAPH = Graph(
    nodes={
        "server_js": GraphNode("server_js", "server.js", "server.js", 1),
        "server_split": GraphNode("server_split", "split()", "server.js", 4),
    },
    edges=[GraphEdge("server_js", "server_split", "contains", explicit=True)],
)


def cite(path: str, start: int, end: int) -> dict[str, Any]:
    return {"path": path, "start_line": start, "end_line": end, "why": "computes the shares"}


GOOD_AUDIT = audit_reply(
    [
        claim_entry("c1", citations=[cite("server.js", 4, 6)]),
        claim_entry("c2", "not_found", explanation="No word counting anywhere."),
    ],
    [{"path": "server.js", "line": 1, "kind": "other", "excerpt": 'require("express")'}],
)


def archive(directory: Path, files: dict[str, str]) -> tuple[Path, str]:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    digest = sha256_hex(buffer.getvalue())
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.tar.gz"
    path.write_bytes(buffer.getvalue())
    return path, digest


@dataclass
class Scene:
    team_id: uuid.UUID
    submission_id: uuid.UUID
    event_name: str
    team_name: str


async def scene(sessions: Sessions, tmp_path: Path) -> Scene:
    """A team with claims c1 and c2 (version 1) and a later version 2, and a submission of
    version 1 whose snapshot is a real archive of `FILES`."""
    path, digest = archive(tmp_path / "archives", FILES)
    async with sessions.begin() as session:
        event = make_event()
        session.add(event)
        await session.flush()
        team = make_team_row(event.id)
        session.add(team)
        await session.flush()
        snapshot = make_snapshot(team.id, archive_path=str(path), archive_sha256=digest)
        later = {**CLAIM_ITEMS[1], "id": "c3", "text": "Translates the text into Latin."}
        session.add_all(
            [
                snapshot,
                make_claims(team.id, problem_statement="Bills are fiddly to split."),
                make_claims(team.id, [*CLAIM_ITEMS, later], 2),
            ]
        )
        await session.flush()
        submission = make_submission(team.id, snapshot.id, claims_version=1)
        session.add(submission)
        await session.flush()
        return Scene(team.id, submission.id, event.name, team.name)


async def audit_job(sessions: Sessions, s: Scene) -> queue.Job:
    return await claimed(
        sessions,
        "audit",
        key=queue.audit_key(s.submission_id),
        submission_id=s.submission_id,
        team_id=s.team_id,
    )


@dataclass
class FakeGraphify:
    graph: Graph | None = GRAPH
    error: Exception | None = None
    seen: list[set[str]] = field(default_factory=list)

    async def __call__(self, src: Path) -> Graph:
        self.seen.append({p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file()})
        (src / "graphify-out").mkdir()  # as the real one does, inside the source folder
        (src / "graphify-out" / "graph.json").write_text("{}")
        if self.error is not None:
            raise self.error
        assert self.graph is not None
        return self.graph


def settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "llm_mode": "replay",
        "audit_model": "audit-model",
        "audit_max_steps": 3,
        "host_artifact_dir": tmp_path / "artifacts",
        **overrides,
    }
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


async def run(
    sessions: Sessions,
    tmp_path: Path,
    transport: Scripted,
    job: queue.Job,
    graphify: FakeGraphify | None = None,
) -> Outcome | None:
    handler = make_audit_handler(
        sessions,
        LLMClient(transport, sessions=sessions),
        settings(tmp_path),
        graphify=graphify or FakeGraphify(),
    )
    return await handler(job)


async def audits(sessions: Sessions) -> list[models.Audit]:
    async with sessions() as session:
        return list(await session.scalars(select(models.Audit)))


def work_folder(tmp_path: Path, job: queue.Job) -> Path:
    return tmp_path / "artifacts" / "work" / str(job.attempt_id)


# --- queued by the submission: tests/unit/test_api_submissions.py ---------------------------------


# --- what is stored ------------------------------------------------------------------------------


async def test_the_audit_stores_validated_claims_hints_and_a_code_map(
    sessions: Sessions, tmp_path: Path
) -> None:
    s = await scene(sessions, tmp_path)
    job = await audit_job(sessions, s)
    transport = Scripted(GOOD_AUDIT)
    graphify = FakeGraphify()

    assert await run(sessions, tmp_path, transport, job, graphify) is None

    [row] = await audits(sessions)
    assert (row.submission_id, row.job_id, row.attempt_id) == (
        s.submission_id,
        job.id,
        job.attempt_id,
    )
    assert (row.partial, row.citations_dropped) == (False, 0)
    content = row.content
    assert [c["claim_id"] for c in content["claims"]] == ["c1", "c2"]
    assert content["claims"][0]["citations"] == [
        {"id": "c1-1", "path": "server.js", "start_line": 4, "end_line": 6,
         "why": "computes the shares"}
    ]  # fmt: skip
    assert content["claims"][1]["implementation"] == "not_found"
    assert content["text_hints"] == [
        {"path": "server.js", "line": 1, "kind": "other", "excerpt": 'require("express")'}
    ]
    assert (content["missing_claims"], content["model"], content["failure"]) == (
        [],
        "audit-model",
        None,
    )
    assert row.code_map["claims"]["c1"]["method"] == "graph"
    assert [a["label"] for a in row.code_map["claims"]["c1"]["anchors"]] == ["split()"]
    assert row.code_map["claims"]["c2"]["method"] == "none"
    assert row.code_map_status == "fallback"
    assert graphify.seen == [{"server.js", "README.md"}]  # the extracted snapshot, nothing else


async def test_the_model_reads_the_submitted_claims_version_and_the_event(
    sessions: Sessions, tmp_path: Path
) -> None:
    s = await scene(sessions, tmp_path)
    transport = Scripted(GOOD_AUDIT)

    await run(sessions, tmp_path, transport, await audit_job(sessions, s))

    [body] = transport.bodies
    assert body["model"] == "audit-model"
    [first] = transport.user_messages()
    assert s.event_name in first and s.team_name in first
    assert "Bills are fiddly to split." in first
    assert '<claim id="c1">' in first and '<claim id="c2">' in first
    # Version 2 came after the submission. (Not a bare "c3": the random names may hold it.)
    assert '<claim id="c3">' not in first and "Latin" not in first
    assert "server.js" in first  # the top folder's listing
    assert "graphify-out" not in first  # Graphify runs after the audit


async def test_invalid_citations_are_dropped_and_counted_in_the_row(
    sessions: Sessions, tmp_path: Path
) -> None:
    s = await scene(sessions, tmp_path)
    citations = [cite("server.js", 4, 6), cite("../../etc/passwd", 1, 2), cite("server.js", 9, 99)]
    transport = Scripted(audit_reply([claim_entry("c1", citations=citations)]))

    await run(sessions, tmp_path, transport, await audit_job(sessions, s))

    [row] = await audits(sessions)
    assert row.citations_dropped == 2
    assert [c["id"] for c in row.content["claims"][0]["citations"]] == ["c1-1"]
    assert row.content["missing_claims"] == ["c2"]


async def test_a_forced_graphify_failure_still_stores_a_renderable_audit(
    sessions: Sessions, tmp_path: Path
) -> None:
    s = await scene(sessions, tmp_path)
    graphify = FakeGraphify(error=GraphError("graphify took longer than 120 s"))

    outcome = await run(
        sessions, tmp_path, Scripted(GOOD_AUDIT), await audit_job(sessions, s), graphify
    )

    assert outcome is None
    [row] = await audits(sessions)
    assert [c["claim_id"] for c in row.content["claims"]] == ["c1", "c2"]
    assert row.code_map["graphify"] == {
        "ok": False, "detail": "graphify took longer than 120 s", "nodes": 0, "edges": 0,
    }  # fmt: skip
    assert row.code_map["claims"]["c1"]["method"] == "search"
    assert row.code_map["claims"]["c1"]["hits"][0]["line"] == 8
    assert row.code_map_status == "fallback"


async def test_an_unexpected_graphify_crash_is_a_graph_error_too(
    sessions: Sessions, tmp_path: Path
) -> None:
    s = await scene(sessions, tmp_path)
    graphify = FakeGraphify(error=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad byte"))

    await run(sessions, tmp_path, Scripted(GOOD_AUDIT), await audit_job(sessions, s), graphify)

    [row] = await audits(sessions)
    assert row.code_map["graphify"]["ok"] is False
    assert "UnicodeDecodeError" in row.code_map["graphify"]["detail"]


async def test_an_audit_that_never_submits_is_stored_as_failed(
    sessions: Sessions, tmp_path: Path
) -> None:
    s = await scene(sessions, tmp_path)
    reads = calls_reply(("read_file", {"path": "server.js", "start_line": 1, "end_line": 5}))
    transport = Scripted(reads, text_reply("Still looking."), reads, text_reply("Done, I think."))

    outcome = await run(sessions, tmp_path, transport, await audit_job(sessions, s))

    assert outcome is not None and outcome.error_class == "budget"
    [row] = await audits(sessions)
    assert row.partial is True
    assert row.content["claims"] == [] and row.content["missing_claims"] == ["c1", "c2"]
    assert row.content["failure"] == outcome.error_detail
    assert "submit_audit" in row.content["failure"]
    assert row.code_map_status in ("ok", "fallback", "failed")


async def test_a_partial_audit_completes_with_a_budget_outcome(
    sessions: Sessions, tmp_path: Path
) -> None:
    s = await scene(sessions, tmp_path)
    reads = calls_reply(("list_dir", {"path": ""}))
    transport = Scripted(reads, reads, reads, GOOD_AUDIT)

    outcome = await run(sessions, tmp_path, transport, await audit_job(sessions, s))

    assert outcome == Outcome("budget", "the audit used up its budget after 4 steps")
    [row] = await audits(sessions)
    assert row.partial is True and row.content["failure"] is None
    assert len(row.content["claims"]) == 2


# --- repeats, failures and clean-up --------------------------------------------------------------


async def test_the_work_folder_is_removed_however_the_job_ends(
    sessions: Sessions, tmp_path: Path
) -> None:
    s = await scene(sessions, tmp_path)
    done = await audit_job(sessions, s)
    await run(sessions, tmp_path, Scripted(GOOD_AUDIT), done)
    assert not work_folder(tmp_path, done).exists()

    async with sessions.begin() as session:
        first = await session.get(models.Submission, s.submission_id)
        assert first is not None
        other = make_submission(s.team_id, first.snapshot_id)
        session.add(other)
    broken = await claimed(sessions, "audit", key=queue.audit_key(other.id), submission_id=other.id)
    with pytest.raises(InfraError):
        await run(sessions, tmp_path, Scripted(InfraError("gateway 502")), broken)
    assert not work_folder(tmp_path, broken).exists()
    assert work_folder(tmp_path, broken).parent.exists()  # only this attempt's folder goes


async def test_a_second_run_of_the_same_attempt_stores_nothing_new(
    sessions: Sessions, tmp_path: Path
) -> None:
    s = await scene(sessions, tmp_path)
    job = await audit_job(sessions, s)
    transport = Scripted(GOOD_AUDIT)  # one reply: a second model call would fail the test
    await run(sessions, tmp_path, transport, job)

    assert await run(sessions, tmp_path, transport, job) is None

    assert len(await audits(sessions)) == 1
    assert len(transport.bodies) == 1


async def test_a_row_stored_while_the_model_worked_is_not_doubled(
    sessions: Sessions, tmp_path: Path
) -> None:
    s = await scene(sessions, tmp_path)
    job = await audit_job(sessions, s)

    async def twin_stores_first(body: dict[str, Any]) -> dict[str, Any]:
        async with sessions.begin() as session:
            session.add(
                models.Audit(
                    submission_id=s.submission_id,
                    job_id=job.id,
                    attempt_id=job.attempt_id,
                    content={"claims": []},
                )
            )
        return GOOD_AUDIT

    handler = make_audit_handler(
        sessions,
        LLMClient(twin_stores_first, sessions=sessions),
        settings(tmp_path),
        graphify=FakeGraphify(),
    )

    assert await handler(job) is None
    [row] = await audits(sessions)
    assert row.content == {"claims": []}  # the twin's row, not a second one


async def test_a_gateway_failure_is_left_to_the_job_loop(
    sessions: Sessions, tmp_path: Path
) -> None:
    s = await scene(sessions, tmp_path)

    with pytest.raises(InfraError, match="gateway 502"):
        await run(
            sessions, tmp_path, Scripted(InfraError("gateway 502")), await audit_job(sessions, s)
        )

    assert await audits(sessions) == []


async def test_a_job_without_a_submission_is_a_validation_outcome(
    sessions: Sessions, tmp_path: Path
) -> None:
    job = await claimed(sessions, "audit", key=queue.audit_key(uuid.uuid4()))

    outcome = await run(sessions, tmp_path, Scripted(), job)

    assert outcome == Outcome("validation", "audit needs a submission")
