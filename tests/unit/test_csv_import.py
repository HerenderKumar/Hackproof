"""Team import CSV (section 5.1): header `team_name,contact_email,repo_url`."""

from api.csv_import import MAX_ROWS, RowError, TeamRow, parse_teams_csv


def parse(text: str) -> tuple[list[TeamRow], list[RowError]]:
    return parse_teams_csv(text.encode())


def test_rows_are_parsed_with_optional_email_and_repo() -> None:
    rows, errors = parse(
        "team_name,contact_email,repo_url\n"
        "Rocket,ada@example.com,https://github.com/acme/rocket\n"
        "Comet,,\n"
    )
    assert errors == []
    assert rows == [
        TeamRow(2, "Rocket", "ada@example.com", "https://github.com/acme/rocket"),
        TeamRow(3, "Comet", None, None),
    ]


def test_the_repo_and_email_columns_may_be_left_out() -> None:
    rows, errors = parse("team_name\nRocket\n")
    assert errors == []
    assert rows == [TeamRow(2, "Rocket", None, None)]


def test_spreadsheet_exports_are_accepted() -> None:
    # Excel writes a byte-order mark, CRLF line ends and quotes around commas.
    data = '﻿Team_Name , contact_email,repo_url\r\n"Rocket, Inc", ada@example.com ,\r\n\r\n'
    rows, errors = parse_teams_csv(data.encode("utf-8"))
    assert errors == []
    assert rows == [TeamRow(2, "Rocket, Inc", "ada@example.com", None)]


def test_a_missing_team_name_column_is_reported() -> None:
    rows, errors = parse("name,contact_email\nRocket,\n")
    assert rows == []
    assert [e.line for e in errors] == [1]
    assert "team_name" in errors[0].message


def test_an_unknown_column_is_reported() -> None:
    _, errors = parse("team_name,phone\nRocket,123\n")
    assert [e.line for e in errors] == [1]
    assert "phone" in errors[0].message


def test_each_bad_row_is_reported_with_its_line_number() -> None:
    rows, errors = parse(
        "team_name,contact_email,repo_url\n"
        ",ada@example.com,\n"
        "Rocket,not-an-email,\n"
        "Comet,,\n"
        "comet,,\n"
    )
    assert [r.name for r in rows] == ["Comet"]
    assert [(e.line, e.message) for e in errors] == [
        (2, "team_name is empty"),
        (3, "contact_email is not an email address"),
        (5, 'team_name "comet" appears more than once (first on line 4)'),
    ]


def test_a_row_with_too_many_cells_is_reported() -> None:
    _, errors = parse("team_name,contact_email\nRocket,ada@example.com,extra\n")
    assert [(e.line, e.message) for e in errors] == [(2, "expected 2 columns, found 3")]


def test_overlong_values_are_reported() -> None:
    _, errors = parse(f"team_name\n{'x' * 101}\n")
    assert [(e.line, e.message) for e in errors] == [(2, "team_name is longer than 100 characters")]


def test_an_empty_file_is_reported() -> None:
    assert parse("")[1] == [RowError(1, "the file is empty")]
    assert parse("team_name\n")[1] == [RowError(1, "the file has no teams")]


def test_a_file_that_is_not_utf8_is_reported() -> None:
    _, errors = parse_teams_csv("team_name\nCaf\xe9\n".encode("latin-1"))
    assert errors == [RowError(1, "the file is not UTF-8 text")]


def test_too_many_rows_are_reported() -> None:
    body = "".join(f"Team {i}\n" for i in range(MAX_ROWS + 1))
    _, errors = parse("team_name\n" + body)
    assert errors == [RowError(1, f"the file has more than {MAX_ROWS} teams")]
