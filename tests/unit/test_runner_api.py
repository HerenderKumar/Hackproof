"""The runner's API tests (section 14) against fake apps: evidence, steps, cases and errors.

The fake apps are `httpx.MockTransport` handlers, so nothing touches the network.
"""

import copy
import json
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx
import pytest

from core.specs import ApiSpec
from runner.api_tests import STORED_BODY_LIMIT, run_api_tests
from runner.main import load_request_text
from tests.spec_examples import F01, F02, F04

URL = "http://app:8080"
Handler = Callable[[httpx.Request], httpx.Response]


def spec(document: dict[str, Any], spec_id: str = "s1") -> dict[str, Any]:
    """The spec as the runner reads it from spec.json: through JSON, numbers as Decimal."""
    ApiSpec.model_validate(document)  # the examples obey the case rules
    return load_request_text(json.dumps(document | {"spec_id": spec_id}))


def client(handler: Handler, seen: list[httpx.Request] | None = None) -> httpx.Client:
    def record(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(record))


def body(request: httpx.Request) -> Any:
    return json.loads(request.content)


def good_splitter(request: httpx.Request) -> httpx.Response:
    data = body(request)
    paise, people = round(Decimal(str(data["total"])) * 100), int(data["people"])
    shares = [paise // people + (1 if i < paise % people else 0) for i in range(people)]
    return httpx.Response(200, json={"shares": [share / 100 for share in shares]})


def naive_splitter(request: httpx.Request) -> httpx.Response:
    data = body(request)
    share = (Decimal(str(data["total"])) / data["people"]).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return httpx.Response(200, json={"shares": [float(share)] * int(data["people"])})


def pantry(store: bool = True, id_value: Any = 7) -> Handler:
    items: dict[str, Any] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            item = body(request) | {"id": id_value}
            if store:
                items[str(id_value)] = item
            return httpx.Response(201, json=item)
        found = items.get(request.url.path.removeprefix("/items/"))
        return httpx.Response(200, json=found) if found else httpx.Response(404, json={})

    return handle


def by_case(entries: list[dict[str, Any]]) -> dict[str | None, dict[str, Any]]:
    return {entry["case_id"]: entry for entry in entries}


# --- evidence ------------------------------------------------------------------------------------


def test_a_passing_case_records_its_evidence() -> None:
    entries = run_api_tests([spec(F01)], "final", URL, client(good_splitter))

    f1 = by_case(entries)["f1"]
    assert f1["status"] == "pass"
    assert f1["spec_id"] == "s1"
    assert f1["error"] is None
    assert f1["steps"][0]["request"] == {
        "method": "POST",
        "path": "/split",
        "query": {},
        "headers": {},
        "json": {"total": 100, "people": 3},
    }
    assert f1["steps"][0]["response"]["status"] == 200
    assert f1["request"] == f1["steps"][0]["request"]
    assert [a["op"] for a in f1["assertions"]] == ["status_equals", "length_equals", "sum_equals"]
    assert all(a["step"] == "split" and a["passed"] for a in f1["assertions"])
    assert f1["duration_ms"] >= 0
    assert (f1["video_path"], f1["trace_path"]) == (None, None)


def test_the_failing_assertion_carries_expected_and_actual() -> None:
    entries = run_api_tests([spec(F01)], "final", URL, client(naive_splitter))

    f1 = by_case(entries)["f1"]
    assert f1["status"] == "fail"
    summed = next(a for a in f1["assertions"] if a["op"] == "sum_equals")
    assert Decimal(summed["expected"]) == Decimal("100.00")
    assert (summed["actual"], summed["passed"]) == ("99.99", False)


def test_final_mode_runs_the_hidden_cases_and_practice_mode_the_practice_case() -> None:
    final = run_api_tests([spec(F01)], "final", URL, client(good_splitter))
    practice = run_api_tests([spec(F01)], "practice", URL, client(good_splitter))

    assert [e["case_id"] for e in final] == ["f1", "f2", "f3"]
    assert [e["case_id"] for e in practice] == ["p1"]


# --- steps ---------------------------------------------------------------------------------------


def test_values_saved_from_one_step_feed_the_next() -> None:
    seen: list[httpx.Request] = []

    entries = run_api_tests([spec(F02)], "final", URL, client(pantry(), seen))

    assert {e["status"] for e in entries} == {"pass"}
    assert [r.url.path for r in seen[:2]] == ["/items", "/items/7"]


def test_a_failed_save_fails_the_step_and_skips_the_rest() -> None:
    def no_id(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={})

    f1 = by_case(run_api_tests([spec(F02)], "final", URL, client(no_id)))["f1"]

    assert f1["status"] == "fail"
    save = next(a for a in f1["assertions"] if a["op"] == "save")
    assert save["note"] == "nothing at $.id to save as id" and not save["passed"]
    assert f1["steps"][1] == {"id": "read", "skipped": True}


def test_a_case_stops_at_its_first_failing_step() -> None:
    seen: list[httpx.Request] = []

    def refuses_login(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "wrong username or password"})

    f1 = by_case(run_api_tests([spec(F04)], "final", URL, client(refuses_login, seen)))["f1"]

    assert f1["status"] == "fail"
    assert [s.get("skipped", False) for s in f1["steps"]] == [False, True, True]
    assert f1["response"]["status"] == 401
    assert len([r for r in seen if r.url.path == "/me"]) == 0


def test_templated_path_values_are_percent_encoded() -> None:
    seen: list[httpx.Request] = []

    run_api_tests([spec(F02)], "final", URL, client(pantry(id_value="a b/c"), seen))

    assert seen[1].url.raw_path == b"/items/a%20b%2Fc"


def test_headers_are_sent_with_their_templates_filled() -> None:
    seen: list[httpx.Request] = []

    def login_app(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={"token": "t0k"})
        if request.headers.get("authorization") == "Bearer t0k":
            return httpx.Response(200, json={"username": "demo"})
        return httpx.Response(401, json={})

    entries = run_api_tests([spec(F04)], "final", URL, client(login_app, seen))

    assert {e["status"] for e in entries} == {"pass"}
    assert seen[1].headers["authorization"] == "Bearer t0k"


# --- what goes wrong -----------------------------------------------------------------------------


def test_an_html_error_page_is_a_failure_with_evidence() -> None:
    def crashes(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="<html>Internal Server Error</html>")

    f1 = by_case(run_api_tests([spec(F01)], "final", URL, client(crashes)))["f1"]

    assert f1["status"] == "fail"
    assert f1["error"] is None
    assert f1["response"]["status"] == 500
    assert "Internal Server Error" in f1["response"]["body"]


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectError("refused"), httpx.RemoteProtocolError("closed"), httpx.ReadError("reset")],
)
def test_a_refused_or_dropped_connection_is_connection_refused(exc: Exception) -> None:
    def dies(request: httpx.Request) -> httpx.Response:
        raise exc

    f1 = by_case(run_api_tests([spec(F01)], "final", URL, client(dies)))["f1"]

    assert f1["status"] == "error"
    assert f1["error"]["type"] == "connection_refused"
    assert f1["error"]["message"].startswith("the app stopped answering")


def test_a_timeout_is_a_timeout_error() -> None:
    def hangs(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no answer", request=request)

    f1 = by_case(run_api_tests([spec(F01)], "final", URL, client(hangs)))["f1"]

    assert (f1["status"], f1["error"]["type"]) == ("error", "timeout")


def test_a_missing_case_field_is_a_spec_error() -> None:
    broken = copy.deepcopy(F01)
    broken["steps"][0]["request"]["json"]["total"] = "{{case.input.nope}}"

    f1 = by_case(run_api_tests([spec(broken)], "final", URL, client(good_splitter)))["f1"]

    assert (f1["status"], f1["error"]["type"]) == ("error", "spec")


def test_one_cases_crash_does_not_stop_the_others() -> None:
    def crashes_on_a_paisa(request: httpx.Request) -> httpx.Response:
        if body(request)["total"] == 0.01:
            raise RuntimeError("the fake app broke")
        return good_splitter(request)

    entries = by_case(run_api_tests([spec(F01)], "final", URL, client(crashes_on_a_paisa)))

    assert entries["f2"]["error"]["type"] == "runner"
    assert entries["f1"]["status"] == entries["f3"]["status"] == "pass"


def test_a_spec_without_cases_for_the_mode_runs_once() -> None:
    no_cases = spec(F01) | {"final_cases": []}
    no_cases["steps"][0]["request"]["json"] = {"total": 10, "people": 2}
    no_cases["steps"][0]["assertions"] = [{"op": "status_equals", "value": 200}]

    entries = run_api_tests([no_cases], "final", URL, client(good_splitter))

    assert [(e["case_id"], e["status"]) for e in entries] == [(None, "pass")]


def test_ui_specs_are_left_to_the_browser() -> None:
    ui = {"spec_id": "u1", "kind": "ui", "claim_id": "c2", "claim_type": "ui_workflow", "steps": []}

    assert run_api_tests([ui], "final", URL, client(good_splitter)) == []


def test_startup_specs_are_not_api_tests() -> None:
    startup = {
        "spec_id": "x",
        "kind": "startup",
        "claim_type": "startup",
        "steps": [{"health_path": "/"}],
    }

    assert run_api_tests([startup], "final", URL, client(good_splitter)) == []


def test_bodies_are_read_up_to_1_mib_and_stored_up_to_16_kb() -> None:
    def floods(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (2 << 20))

    f1 = by_case(run_api_tests([spec(F01)], "final", URL, client(floods)))["f1"]

    assert f1["response"]["truncated"] is True
    assert len(f1["response"]["body"]) == STORED_BODY_LIMIT


def test_redirects_are_not_followed() -> None:
    seen: list[httpx.Request] = []

    def redirects(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://elsewhere.example/"})

    f1 = by_case(run_api_tests([spec(F01)], "final", URL, client(redirects, seen)))["f1"]

    assert f1["response"]["status"] == 302
    assert all(r.url.host == "app" for r in seen)
