"""The "possibly related" list with the worker's own model (M9 plan, Stage 5, Task 22).

Runs inside the worker image, where the model was downloaded at build and is read offline.
"""

from pathlib import Path

import pytest

from core.settings import Settings
from scripts.baselines import load_baseline_fixtures
from scripts.fixtures import FIXTURES_DIR
from scripts.run_baselines import run_fixture
from worker.embedder import fast_embedder

pytestmark = pytest.mark.integration

MIN_SCORE: float = Settings.model_fields["related_min_score"].default


def test_b6_is_possibly_related_with_the_real_model(tmp_path: Path) -> None:
    fixtures = {f.id: f for f in load_baseline_fixtures(FIXTURES_DIR)}
    report, _ = run_fixture(
        fixtures["B6-rewritten"], tmp_path / "B6", embedder_factory=fast_embedder,
        related_min_score=MIN_SCORE,
    )  # fmt: skip
    found = [m for m in report.matches if m.kind == "possibly_related"]
    assert report.totals.percent == 0.0
    assert found and all(m.score is not None and m.score >= MIN_SCORE for m in found)
    # Old paths are as the device had them (absolute); only their end is the fixture's.
    assert {m.final_path for m in found} == {"src/distance.js"}
    assert all(m.old_path.endswith("/laptop/projects/trail-tracker/geo.js") for m in found)
    # The haversine function, written again, is among them.
    assert any(m.final_start_line == 11 and m.old_start_line == 9 for m in found)


@pytest.mark.parametrize(
    "fixture_id", ["B1-copied", "B2-edited", "B3-common", "B4-starter", "B5-scan"]
)
def test_unrelated_code_is_not_listed_with_the_real_model(tmp_path: Path, fixture_id: str) -> None:
    (fixture,) = [f for f in load_baseline_fixtures(FIXTURES_DIR) if f.id == fixture_id]
    report, _ = run_fixture(
        fixture, tmp_path, embedder_factory=fast_embedder, related_min_score=MIN_SCORE
    )
    assert [m for m in report.matches if m.kind == "possibly_related"] == []
