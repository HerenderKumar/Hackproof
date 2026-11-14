"""structlog as JSON lines (section 19).

Standard fields, bound where known: service, event_id, team_id, submission_id, job_id,
attempt_id, commit_sha, stage, duration_ms, error_class, request_id.
"""

import logging
from collections.abc import MutableMapping
from typing import Any

import structlog


def configure_logging(service: str, level: str = "INFO") -> None:
    def add_service(
        _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        event_dict.setdefault("service", service)
        return event_dict

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            add_service,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        # Resolve sys.stdout at write time so redirected output (tests, containers) is honoured.
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
