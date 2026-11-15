"""An event's times: kickoff, the claims lock, the deadline (D-047 point 6).

Claims can change until the lock; after it, claims and their tests are frozen, and self-checks
and the final submission go on until the deadline. The organiser may set the lock; left unset,
it falls two thirds of the way from kickoff to deadline.
"""

from datetime import datetime

from core.db import models


def claims_lock_at(event: models.Event) -> datetime:
    """When the event's claims freeze: its own lock, or two thirds of the way through."""
    if event.claims_lock_at is not None:
        return event.claims_lock_at
    return event.kickoff_at + (event.deadline_at - event.kickoff_at) * 2 / 3
