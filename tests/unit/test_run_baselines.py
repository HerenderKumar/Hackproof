"""`make baselines` (M9, Task 11): each fixture B1 to B5 gives its expected report."""

import json
import shutil
from pathlib import Path

import pytest

from scripts.baselines import BaselineFixture, load_baseline_fixtures
from scripts.run_baselines import EMBEDDERS, differences, main, run_fixture


@pytest.mark.parametrize("fixture", load_baseline_fixtures(), ids=lambda f: f.id)
def test_a_baseline_fixture_gives_its_expected_report(
    fixture: BaselineFixture, tmp_path: Path
) -> None:
    factory, min_score = EMBEDDERS["fake"]()  # as `make baselines` runs them
    report, scans = run_fixture(
        fixture, tmp_path, embedder_factory=factory, related_min_score=min_score
    )
    assert differences(fixture, report, scans, tmp_path) == []


def test_there_are_six_fixtures_and_each_names_its_percent() -> None:
    fixtures = load_baseline_fixtures()
    assert [f.id[:2] for f in fixtures] == ["B1", "B2", "B3", "B4", "B5", "B6"]
    assert all("percent" in f.expected and "matches" in f.expected for f in fixtures)


def test_the_runner_reports_a_difference_and_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    [b1] = [f for f in load_baseline_fixtures() if f.id.startswith("B1")]
    copy = tmp_path / "baseline" / b1.id
    shutil.copytree(b1.root, copy)
    expected = json.loads((copy / "expected.json").read_text())
    (copy / "expected.json").write_text(json.dumps({**expected, "identical_files": 2}))

    assert main(["--root", str(tmp_path)]) == 1
    assert "identical_files: expected 2, got 3" in capsys.readouterr().out
