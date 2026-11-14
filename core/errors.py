"""Errors the job loop understands (section 11.3)."""


class InfraError(Exception):
    """A failure outside the team's app: Docker, GitHub, Claude, Railpack exit 75, a crashed runner.

    The job goes back to the queue with backoff while attempts remain. Failures of the team's
    own app are never raised as errors: they are results, stored and completed normally.
    """
