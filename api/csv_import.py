"""Parsing the team import file (section 5.1).

Header: `team_name,contact_email,repo_url`. Only `team_name` is required; the other two
columns, and their values, are optional. Parsing reports every problem with its line number
and never touches the database.
"""

import csv
import io
import re
from dataclasses import dataclass

MAX_BYTES = 1_000_000
MAX_ROWS = 500
COLUMNS = ("team_name", "contact_email", "repo_url")
MAX_LENGTH = {"team_name": 100, "contact_email": 254, "repo_url": 300}

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class TeamRow:
    line: int
    name: str
    contact_email: str | None
    repo_url: str | None


@dataclass(frozen=True)
class RowError:
    line: int
    message: str


def parse_teams_csv(data: bytes) -> tuple[list[TeamRow], list[RowError]]:
    """The valid rows and the problems found. Import nothing unless the problems are empty."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return [], [RowError(1, "the file is not UTF-8 text")]

    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    if header is None or not any(cell.strip() for cell in header):
        return [], [RowError(1, "the file is empty")]

    columns = [cell.strip().lower() for cell in header]
    unknown = [c for c in columns if c not in COLUMNS]
    if unknown:
        allowed = ", ".join(COLUMNS)
        return [], [RowError(1, f"unknown column {unknown[0]!r}; the columns are {allowed}")]
    if "team_name" not in columns:
        return [], [RowError(1, "the header must include team_name")]

    rows: list[TeamRow] = []
    errors: list[RowError] = []
    first_seen: dict[str, int] = {}
    for cells in reader:
        line = reader.line_num
        if not any(cell.strip() for cell in cells):
            continue
        if len(rows) + len(errors) >= MAX_ROWS:
            return [], [RowError(1, f"the file has more than {MAX_ROWS} teams")]
        if len(cells) > len(columns):
            errors.append(RowError(line, f"expected {len(columns)} columns, found {len(cells)}"))
            continue
        values = dict(zip(columns, (cell.strip() for cell in cells), strict=False))
        problem = _problem(values, first_seen)
        if problem:
            errors.append(RowError(line, problem))
            continue
        first_seen[values["team_name"].casefold()] = line
        rows.append(
            TeamRow(
                line=line,
                name=values["team_name"],
                contact_email=values.get("contact_email") or None,
                repo_url=values.get("repo_url") or None,
            )
        )

    if not rows and not errors:
        return [], [RowError(1, "the file has no teams")]
    return rows, errors


def _problem(values: dict[str, str], first_seen: dict[str, int]) -> str | None:
    name = values.get("team_name", "")
    if not name:
        return "team_name is empty"
    for column, limit in MAX_LENGTH.items():
        if len(values.get(column, "")) > limit:
            return f"{column} is longer than {limit} characters"
    email = values.get("contact_email")
    if email and not _EMAIL.match(email):
        return "contact_email is not an email address"
    earlier = first_seen.get(name.casefold())
    if earlier is not None:
        return f'team_name "{name}" appears more than once (first on line {earlier})'
    return None
