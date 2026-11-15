"""The UI runner in a real browser (M11 design, parts 3 and 4), against small local pages.

Run with `make runner-test` (the runner image plus pytest). On the host Playwright is not
installed and these are skipped. Chromium runs without its own sandbox here; the integration
tests start it with the sandbox, in the run container's exact settings.
"""

import copy
import functools
import http.server
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("playwright")  # only the runner image has it

from playwright.sync_api import sync_playwright

from runner import ui_tests
from tests import spec_examples

FIXTURE = Path(__file__).resolve().parents[1].parent / "fixtures" / "F05-static-packing-list"
PRACTICE = [{"id": "p1", "input": {"item": "Sunscreen"}, "expected": {}, "boundary": False}]


class Site:
    """A folder of pages served on 127.0.0.1, one per test."""

    def __init__(self, root: Path) -> None:
        handler = functools.partial(_QuietHandler, directory=str(root))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.root = root
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def page(self, name: str, html: str) -> None:
        (self.root / name).write_text(html, encoding="utf-8")


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    root = tmp_path / "site"
    root.mkdir()
    served = Site(root)
    yield served
    served.server.shutdown()


@pytest.fixture(scope="module")
def playwright() -> Iterator[Any]:
    with sync_playwright() as started:
        yield started


@pytest.fixture(autouse=True)
def short_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ui_tests, "STEP_TIMEOUT_S", 1.5)
    monkeypatch.setenv("RUNNER_BROWSER_SANDBOX", "false")


def spec(steps: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    document = copy.deepcopy(spec_examples.F05) | {"spec_id": "u1", "steps": steps}
    return document | extra


def run(
    playwright: Any, site: Site, tmp_path: Path, specs: list[dict[str, Any]], mode: str = "practice"
) -> list[dict[str, Any]]:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    launch = ui_tests.launcher(playwright)
    return ui_tests.run_ui_tests(specs, mode, site.origin, work, launch)  # type: ignore[arg-type]


def f05(site: Site, variant: str) -> None:
    for item in (FIXTURE / variant).iterdir():
        shutil.copy(item, site.root / item.name)


# --- F05 -----------------------------------------------------------------------------------------


def test_f05_good_passes_every_hidden_case_with_its_files(
    playwright: Any, site: Site, tmp_path: Path
) -> None:
    f05(site, "good")
    document = copy.deepcopy(spec_examples.F05) | {"spec_id": "u1"}

    entries = run(playwright, site, tmp_path, [document], mode="final")

    assert [(e["case_id"], e["status"]) for e in entries] == [
        ("f1", "pass"), ("f2", "pass"), ("f3", "pass"),
    ]  # fmt: skip
    for number, entry in enumerate(entries, start=1):
        folder = tmp_path / "work" / "ui" / "u1" / str(number)
        assert (folder / "video.webm").read_bytes()[:4] == bytes.fromhex("1a45dfa3")
        assert (folder / "trace.zip").read_bytes()[:4] == b"PK\x03\x04"
        assert (folder / "poster.jpg").read_bytes()[:3] == b"\xff\xd8\xff"
        assert entry["video_path"] == f"ui/u1/{number}/video.webm"
        assert [s["status"] for s in entry["steps"]] == ["pass"] * 5
        assert entry["page"] is None
    assert entries[1]["steps"][1]["words"] == (
        "Type “Socks & sandals <2 pairs>” into the field labelled “New item”"
    )


def test_f05_broken_fails_on_the_list_with_what_it_held(
    playwright: Any, site: Site, tmp_path: Path
) -> None:
    f05(site, "broken")

    (entry,) = run(playwright, site, tmp_path, [spec(spec_examples.F05["steps"])])

    assert entry["status"] == "fail" and entry["error"] is None
    assert [s["status"] for s in entry["steps"]] == ["pass", "pass", "pass", "fail", "skipped"]
    failed = entry["steps"][3]
    assert (failed["expected"], failed["actual"]) == ("Sunscreen", "")
    assert entry["assertions"][-1] | {"path": None} == {
        "op": "expect_text", "path": None, "expected": "Sunscreen", "actual": "",
        "passed": False, "note": None, "step": "listed",
    }  # fmt: skip
    assert 'list "Items"' in entry["page"]["outline"]


# --- what a failed step means --------------------------------------------------------------------


BUTTONS = """<!doctype html><title>t</title><main>
<button>Add</button><button>Add</button><button disabled>Save</button>
<p id="out"></p></main>"""


@pytest.mark.parametrize(
    ("locator", "kind"),
    [
        ({"role": "button", "name": "Delete"}, "locator_not_found"),
        ({"role": "button", "name": "Add"}, "locator_ambiguous"),
        ({"role": "button", "name": "Save"}, "timeout"),
    ],
    ids=["missing", "two", "disabled"],
)
def test_a_control_that_cannot_be_used_is_unverified_not_failed(
    playwright: Any, site: Site, tmp_path: Path, locator: dict[str, Any], kind: str
) -> None:
    site.page("index.html", BUTTONS)
    steps = [
        {"action": "goto", "path": "/"},
        {"action": "click", "locator": locator},
        {"action": "expect_text", "locator": {"text": "x"}, "text": "{{case.input.item}}"},
    ]

    (entry,) = run(playwright, site, tmp_path, [spec(steps)])

    assert entry["status"] == "error"
    assert entry["error"]["type"] == kind
    assert entry["page"]["outline"].count('button "Add"') == 2


def test_a_page_that_is_not_there_fails(playwright: Any, site: Site, tmp_path: Path) -> None:
    steps = [
        {"action": "goto", "path": "/missing.html"},
        {"action": "expect_visible", "locator": {"text": "{{case.input.item}}"}},
    ]

    (entry,) = run(playwright, site, tmp_path, [spec(steps)])

    assert entry["status"] == "fail"
    assert entry["steps"][0]["message"] == "/missing.html answered HTTP 404"


def test_an_element_that_is_there_but_hidden_fails_a_visible_check(
    playwright: Any, site: Site, tmp_path: Path
) -> None:
    site.page(
        "index.html", "<!doctype html><title>t</title><p hidden>Sunscreen</p><button>Go</button>"
    )
    steps = [
        {"action": "goto", "path": "/"},
        {"action": "click", "locator": {"role": "button", "name": "Go"}},
        {"action": "expect_visible", "locator": {"text": "{{case.input.item}}"}},
    ]

    (entry,) = run(playwright, site, tmp_path, [spec(steps)])

    assert entry["status"] == "fail"
    assert (entry["steps"][2]["expected"], entry["steps"][2]["actual"]) == ("visible", "hidden")


# --- containment ---------------------------------------------------------------------------------


SHOP = """<!doctype html><title>t</title>
<script src="https://cdn.example.com/lib.js"></script>
<label>Size <select><option>S</option><option>M</option><option>L</option></select></label>
<label>Note <input></label>
<button onclick="alert('sure?'); document.getElementById('out').textContent =
  document.querySelector('select').value + ' ' + document.querySelector('input').value;
  window.open('/other.html')">Order</button>
<a href="/done.html">Finish</a>
<p id="out" data-testid="out"></p>"""


def test_outside_requests_are_refused_and_alerts_and_popups_are_noted(
    playwright: Any, site: Site, tmp_path: Path
) -> None:
    site.page("index.html", SHOP)
    site.page("other.html", "<p>other</p>")
    site.page("done.html", "<p>done</p>")
    steps = [
        {"action": "goto", "path": "/"},
        {"action": "select", "locator": {"label": "Size"}, "value": "M"},
        {"action": "fill", "locator": {"label": "Note"}, "value": "{{case.input.item}}"},
        {"action": "press", "key": "Tab"},
        {"action": "click", "locator": {"role": "button", "name": "Order"}},
        {"action": "expect_text", "locator": {"test_id": "out"}, "text": "M {{case.input.item}}"},
        {"action": "click", "locator": {"role": "link", "name": "Finish"}},
        {"action": "expect_url", "path": "/done.html"},
    ]

    (entry,) = run(playwright, site, tmp_path, [spec(steps)])

    assert entry["status"] == "pass", entry
    assert entry["page"]["refused"] == ["https://cdn.example.com/lib.js"]
    assert entry["page"]["dialogs"] == [{"type": "alert", "message": "sure?"}]
    assert entry["page"]["popups"][0].endswith("/other.html")


def test_a_wrong_address_fails_with_the_address_found(
    playwright: Any, site: Site, tmp_path: Path
) -> None:
    site.page("index.html", SHOP)
    steps = [
        {"action": "goto", "path": "/"},
        {"action": "fill", "locator": {"label": "Note"}, "value": "{{case.input.item}}"},
        {"action": "expect_url", "path": "/{{case.input.item}}"},
    ]

    (entry,) = run(playwright, site, tmp_path, [spec(steps)])

    assert entry["status"] == "fail"
    assert entry["steps"][2]["actual"] == "/"


def test_each_case_starts_with_a_fresh_browser_context(
    playwright: Any, site: Site, tmp_path: Path
) -> None:
    site.page(
        "index.html",
        """<!doctype html><title>t</title><label>Item <input></label>
        <button onclick="localStorage.setItem('seen', (localStorage.getItem('seen') || '') + 'x');
          document.getElementById('n').textContent = localStorage.getItem('seen').length">
          Add</button>
        <p id="n" data-testid="n"></p>""",
    )
    steps = [
        {"action": "goto", "path": "/"},
        {"action": "fill", "locator": {"label": "Item"}, "value": "{{case.input.item}}"},
        {"action": "press", "key": "Enter", "locator": {"label": "Item"}},
        {"action": "click", "locator": {"role": "button", "name": "Add"}},
        {"action": "expect_text", "locator": {"test_id": "n"}, "text": "1"},
        {"action": "expect_value", "locator": {"label": "Item"}, "value": "{{case.input.item}}"},
    ]

    entries = run(playwright, site, tmp_path, [spec(steps)], mode="final")

    assert [e["status"] for e in entries] == ["pass", "pass", "pass"]


def test_a_case_stops_after_its_time(
    playwright: Any, site: Site, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ui_tests, "CASE_TIMEOUT_S", 0.5)
    site.page("index.html", BUTTONS)
    steps = [
        {"action": "goto", "path": "/"},
        {"action": "wait_for", "locator": {"text": "never"}},
        {"action": "expect_text", "locator": {"text": "x"}, "text": "{{case.input.item}}"},
    ]

    (entry,) = run(playwright, site, tmp_path, [spec(steps)])

    assert entry["status"] == "error"
    assert entry["error"]["type"] in ("timeout", "locator_not_found")
    assert entry["duration_ms"] < 5000
