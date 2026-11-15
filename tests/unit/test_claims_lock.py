"""The claims lock (D-047 point 6) and the claims hash over the start text (M7 design, part 1)."""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.claims import CLAIM_FIELDS, claims_hash
from core.db import models
from core.events import claims_lock_at
from core.hashing import hash_json
from tests.factories import CLAIM_ITEMS, T0, make_claims, make_event
from tests.helpers import Sessions

DAY = timedelta(hours=24)


def test_the_lock_defaults_to_two_thirds_of_the_event() -> None:
    event = make_event(kickoff_at=T0, deadline_at=T0 + DAY)

    assert claims_lock_at(event) == T0 + timedelta(hours=16)


def test_an_events_own_lock_is_used() -> None:
    event = make_event(kickoff_at=T0, deadline_at=T0 + DAY, claims_lock_at=T0 + timedelta(hours=20))

    assert claims_lock_at(event) == T0 + timedelta(hours=20)


@pytest.mark.parametrize("offset", [timedelta(seconds=-1), DAY + timedelta(seconds=1)])
async def test_a_lock_outside_the_event_is_refused_by_the_database(
    sessions: Sessions, offset: timedelta
) -> None:
    with pytest.raises(IntegrityError, match="events_claims_lock_in_event"):
        async with sessions.begin() as session:
            session.add(make_event(kickoff_at=T0, deadline_at=T0 + DAY, claims_lock_at=T0 + offset))


async def test_a_lock_may_sit_on_either_edge_of_the_event_or_be_left_out(
    sessions: Sessions,
) -> None:
    async with sessions.begin() as session:
        for lock in (T0, T0 + DAY, None):
            session.add(make_event(kickoff_at=T0, deadline_at=T0 + DAY, claims_lock_at=lock))

    async with sessions() as session:
        stored = list(await session.scalars(select(models.Event.claims_lock_at)))
    assert sorted(stored, key=str) == sorted([T0, T0 + DAY, None], key=str)


def test_the_claims_hash_covers_the_start_text() -> None:
    digest = claims_hash("p", "e", CLAIM_ITEMS)

    assert digest == hash_json({"problem_statement": "p", "explanation": "e", "items": CLAIM_ITEMS})
    changed_item = [{**CLAIM_ITEMS[0], "text": "Another claim."}, *CLAIM_ITEMS[1:]]
    others = {
        claims_hash("p2", "e", CLAIM_ITEMS),
        claims_hash("p", "e2", CLAIM_ITEMS),
        claims_hash("p", "e", changed_item),
    }
    assert len(others) == 3 and digest not in others


def test_a_claim_has_five_fields_and_the_factory_hashes_like_the_api() -> None:
    assert CLAIM_FIELDS == ("id", "text", "target", "example_input", "expected_behaviour")
    row = make_claims(None, problem_statement="p", explanation="e")  # type: ignore[arg-type]
    assert row.claims_hash == claims_hash("p", "e", CLAIM_ITEMS)
