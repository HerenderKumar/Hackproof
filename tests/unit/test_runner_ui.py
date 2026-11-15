"""The UI runner's paths that need no browser (M11 design, part 3)."""

from pathlib import Path
from typing import Any

from runner import ui_tests
from tests import spec_examples


def f05(spec_id: str = "u1") -> dict[str, Any]:
    return spec_examples.F05 | {"spec_id": spec_id}


def test_a_browser_that_will_not_start_is_tried_twice_and_every_case_is_unverified(
    tmp_path: Path,
) -> None:
    tries: list[int] = []

    def launch() -> Any:
        tries.append(1)
        raise RuntimeError("no display")

    entries = ui_tests.run_ui_tests([f05()], "final", "http://10.0.0.2:8080", tmp_path, launch)

    assert len(tries) == 2
    assert [(e["case_id"], e["status"], e["error"]["type"]) for e in entries] == [
        ("f1", "error", "runner"), ("f2", "error", "runner"), ("f3", "error", "runner"),
    ]  # fmt: skip
    assert "no display" in entries[0]["error"]["message"]


def test_a_run_without_ui_specs_never_launches(tmp_path: Path) -> None:
    def launch() -> Any:
        raise AssertionError("launched")

    assert ui_tests.run_ui_tests([spec_examples.F07], "final", "http://x", tmp_path, launch) == []


def test_a_malformed_ui_spec_is_reported_without_running(tmp_path: Path) -> None:
    broken = f05() | {"steps": [{"action": "hover"}]}

    (entry,) = ui_tests.run_ui_tests([broken], "final", "http://x", tmp_path, object)

    assert entry["status"] == "error" and entry["error"]["type"] == "spec"


def test_case_folders_never_take_a_path_from_the_spec_id() -> None:
    assert ui_tests._safe("../../etc") == "etc"
    assert ui_tests._safe("4f1c-9a") == "4f1c-9a"
    assert ui_tests._safe("") == "spec"
