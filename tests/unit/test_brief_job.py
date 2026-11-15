"""The brief's written part (M10): code keeps a question only when it points at something the
brief holds, and the job stores a row a judge can read whatever the model did."""

import pytest
from sqlalchemy import select

from core import queue
from core.brief import BriefOutput, kept, known_refs, render_input, template_questions
from core.brief_data import load_facts
from core.db import models
from core.errors import InfraError
from core.llm import LLMClient
from core.settings import Settings
from tests.factories import make_event, make_team
from tests.helpers import Sessions, claimed
from tests.judging import Judged, judged
from tests.llm_fakes import Scripted, text_reply, tool_reply
from worker.brief_job import make_brief_handler

SETTINGS = Settings(_env_file=None, brief_model="brief-model")  # type: ignore[call-arg]


async def planted(sessions: Sessions, **kwargs: object) -> tuple[Judged, dict]:
    async with sessions.begin() as session:
        event = make_event()
        session.add(event)
        await session.flush()
        team = make_team(
            event.id, repo_url="https://github.com/acme/rocket", repo_provider="github"
        )
        session.add(team)
        await session.flush()
        team_id = team.id
    found = await judged(sessions, team_id, **kwargs)  # type: ignore[arg-type]
    async with sessions() as session:
        facts = await load_facts(session, found.submission_id)
    assert facts is not None
    return found, facts


def answer(found: Judged, *refs: tuple[str, str]) -> dict:
    return {
        "summary": "A bill splitter. " + "word " * 80,
        "claims": [
            {"claim_id": "c1", "one_line": "Shares add up to 99.99."},
            {"claim_id": "c9", "one_line": "No such claim."},
        ],
        "demo_questions": [
            {"question": f"Question {n}?", "ref": {"kind": kind, "id": ref}}
            for n, (kind, ref) in enumerate(refs)
        ],
    }


async def brief_job(sessions: Sessions, found: Judged) -> queue.Job:
    return await claimed(
        sessions,
        "brief",
        key=queue.brief_key(found.submission_id),
        submission_id=found.submission_id,
        team_id=found.team_id,
    )


async def rows(sessions: Sessions) -> list[models.Brief]:
    async with sessions() as session:
        return list(await session.scalars(select(models.Brief)))


async def test_the_facts_carry_verdicts_evidence_and_checked_citations(sessions: Sessions) -> None:
    found, facts = await planted(sessions)
    c1, c2 = facts["claims"]
    assert (c1["verdict"], c2["verdict"]) == ("fail", "pass")
    [case] = c1["cases"]
    assert case["result_id"] == str(found.result_ids["c1"])
    assert case["input"] == {"total": 100, "people": 3}
    assert case["request"]["path"] == "/split"
    assert [a["actual"] for a in case["failed"]] == ["99.99"]
    [citation] = c1["audit"]["citations"]
    assert citation["url"].endswith("/server.js#L4-L9") and "/blob/" in citation["url"]
    assert c2["audit"] is None
    assert facts["audit"]["status"] == "ok"


async def test_an_audit_row_from_a_stale_attempt_is_not_shown(sessions: Sessions) -> None:
    found, _ = await planted(sessions)
    async with sessions.begin() as session:
        row = await session.scalar(select(models.Audit))
        assert row is not None
        row.attempt_id = found.submission_id  # any other ID: not the job's finished attempt
    async with sessions() as session:
        facts = await load_facts(session, found.submission_id)
    assert facts is not None
    assert facts["claims"][0]["audit"] is None
    assert facts["audit"]["status"] == "failed"


async def test_a_question_is_kept_only_when_its_ref_is_in_the_brief(sessions: Sessions) -> None:
    found, facts = await planted(sessions)
    assert known_refs(facts) == {
        "test_result": {str(i) for i in found.result_ids.values()},
        "citation": {"c1-1"},
    }
    good = answer(found, ("citation", "c1-1"), ("test_result", str(found.result_ids["c1"])))
    content, complete = kept(BriefOutput.model_validate(good), facts)
    assert complete and content["questions_from"] == "model"
    assert len(content["summary"].split()) <= 60
    assert content["claims"] == {"c1": "Shares add up to 99.99."}

    # A citation ID offered as a test result, and an invented one: neither is a ref.
    bad = answer(found, ("test_result", "c1-1"), ("citation", "c1-7"), ("citation", "c1-1"))
    content, complete = kept(BriefOutput.model_validate(bad), facts)
    assert not complete and content["questions_from"] == "template"
    assert content["demo_questions"] == template_questions(facts)


async def test_template_questions_start_from_the_failing_test(sessions: Sessions) -> None:
    found, facts = await planted(sessions)
    first, second = template_questions(facts)
    assert first["ref"] == {"kind": "test_result", "id": str(found.result_ids["c1"])}
    assert "99.99" in first["question"] and "100.0" in first["question"]
    assert second["ref"] == {"kind": "citation", "id": "c1-1"}


async def test_the_model_is_shown_verdicts_but_cannot_return_them(sessions: Sessions) -> None:
    _, facts = await planted(sessions)
    shown = render_input(facts)
    assert '"verdict": "fail"' in shown and "<brief_inputs>" in shown
    assert "verdict" not in BriefOutput.model_json_schema()["properties"]
    assert "verdict" not in str(BriefOutput.model_json_schema()["$defs"]["ClaimLineIn"])


async def test_the_job_stores_what_was_kept(sessions: Sessions) -> None:
    found, _ = await planted(sessions)
    job = await brief_job(sessions, found)
    model = Scripted(
        tool_reply("submit_brief", answer(found, ("citation", "c1-1"), ("citation", "c1-1")))
    )
    handler = make_brief_handler(sessions, LLMClient(model), SETTINGS)

    assert await handler(job) is None
    [row] = await rows(sessions)
    assert (row.ai_status, row.model, row.attempt_id) == ("ok", "brief-model", job.attempt_id)
    assert row.content["questions_from"] == "model"
    assert model.bodies[0]["model"] == "brief-model"

    # The same attempt again (a handler restarted after storing) spends nothing.
    assert await handler(job) is None
    assert len(await rows(sessions)) == 1 and len(model.bodies) == 1


async def test_bad_refs_get_one_retry_then_templates(sessions: Sessions) -> None:
    found, facts = await planted(sessions)
    job = await brief_job(sessions, found)
    wrong = tool_reply("submit_brief", answer(found, ("citation", "nope"), ("citation", "c1-1")))
    model = Scripted(wrong, wrong)
    handler = make_brief_handler(sessions, LLMClient(model), SETTINGS)

    assert await handler(job) is None
    assert len(model.bodies) == 2
    assert "exactly two questions" in model.user_messages()[1]
    [row] = await rows(sessions)
    assert row.ai_status == "ok" and row.content["questions_from"] == "template"
    assert row.content["demo_questions"] == template_questions(facts)
    assert row.content["summary"].startswith("A bill splitter.")


async def test_an_unreadable_answer_stores_a_raw_brief(sessions: Sessions) -> None:
    found, facts = await planted(sessions)
    job = await brief_job(sessions, found)
    handler = make_brief_handler(sessions, LLMClient(Scripted(text_reply("I refuse."))), SETTINGS)

    outcome = await handler(job)
    assert outcome is not None and outcome.error_class == "validation"
    [row] = await rows(sessions)
    assert row.ai_status == "failed" and row.content["summary"] is None
    assert row.content["demo_questions"] == template_questions(facts)


async def test_a_gateway_failure_is_left_to_the_job_loop(sessions: Sessions) -> None:
    found, _ = await planted(sessions)
    job = await brief_job(sessions, found)
    handler = make_brief_handler(
        sessions, LLMClient(Scripted(InfraError("gateway down"))), SETTINGS
    )
    with pytest.raises(InfraError):
        await handler(job)
    assert await rows(sessions) == []


async def test_a_brief_without_a_submission_is_a_validation_outcome(sessions: Sessions) -> None:
    job = await claimed(sessions, "brief")
    outcome = await make_brief_handler(sessions, LLMClient(Scripted()), SETTINGS)(job)
    assert outcome is not None and outcome.error_class == "validation"
