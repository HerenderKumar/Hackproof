"""A final snapshot against stored baselines in one picklable call (M9, Task 11)."""

import pickle
import uuid
from pathlib import Path

from core.priorwork.model import MatchSettings
from core.priorwork.run import BaselineRef, CompareRequest, run_comparison, starter_runs
from tests.baseline_flow import archive_of, stored_baseline
from tests.priorwork_fakes import block

KEY = bytes(range(32))
S = MatchSettings()
EXTENSIONS = (".js", ".py")


def request(
    tmp_path: Path,
    final: dict[str, bytes],
    own: tuple[BaselineRef, ...],
    others: tuple[BaselineRef, ...] = (),
    starters: tuple[bytes, ...] = (),
    min_teams: int = 3,
) -> CompareRequest:
    archive, digest = archive_of(tmp_path / "archives", final)
    return CompareRequest(
        archive=archive,
        archive_sha256=digest,
        extensions=EXTENSIONS,
        max_file_bytes=1024 * 1024,
        store_root=tmp_path,
        key=KEY,
        own=own,
        others=others,
        starter_runs=starters,
        min_teams=min_teams,
        settings=S,
    )


def ref(baseline: uuid.UUID, team: str = "own", late: bool = False) -> BaselineRef:
    return BaselineRef(baseline, team, f"laptop of {team}", late)


def test_a_request_survives_pickling(tmp_path: Path) -> None:
    built = request(tmp_path, {"a.js": b"x"}, (ref(uuid.uuid4()),), starters=(b"\0" * 8,))
    assert pickle.loads(pickle.dumps(built)) == built


def test_the_team_s_own_code_is_found_and_the_rest_is_new(tmp_path: Path) -> None:
    old, new = block(1), block(2)
    mine = stored_baseline(tmp_path, KEY, {"old.js": old.encode()})
    report = run_comparison(
        request(tmp_path, {"src/app.js": (old + new).encode(), "notes.txt": b"x"}, (ref(mine),))
    )
    [match] = report.matches
    assert (match.final_path, match.old_path, match.kind) == ("src/app.js", "old.js", "unchanged")
    assert match.baseline_id == str(mine) and match.device_label == "laptop of own"
    assert [f.path for f in report.files] == ["src/app.js"]  # the .txt file is not eligible


def test_other_teams_baselines_only_decide_what_is_common(tmp_path: Path) -> None:
    theirs = block(3)
    a = stored_baseline(tmp_path, KEY, {"t.js": theirs.encode()})
    b = stored_baseline(tmp_path, KEY, {"t.js": theirs.encode()})
    mine = stored_baseline(tmp_path, KEY, {"mine.js": block(4).encode()})
    final = {"app.js": (theirs + block(5)).encode()}

    report = run_comparison(
        request(tmp_path, final, (ref(mine),), (ref(a, "team-a"), ref(b, "team-b")))
    )

    assert report.matches == () and report.totals.matched == 0
    assert report.totals.excluded_common == 0  # two teams hold it, and three make it common


def test_code_three_teams_hold_is_common_and_excluded(tmp_path: Path) -> None:
    shared = block(6)
    mine = stored_baseline(tmp_path, KEY, {"s.js": shared.encode()})
    a = stored_baseline(tmp_path, KEY, {"s.js": shared.encode()})
    b = stored_baseline(tmp_path, KEY, {"s.js": shared.encode()})
    final = {"app.js": (shared + block(7)).encode()}

    report = run_comparison(
        request(tmp_path, final, (ref(mine),), (ref(a, "team-a"), ref(b, "team-b")))
    )

    assert report.matches == () and report.totals.excluded_common > 0


def test_two_devices_of_one_team_count_as_one_team(tmp_path: Path) -> None:
    shared = block(8)
    laptop = stored_baseline(tmp_path, KEY, {"s.js": shared.encode()})
    desktop = stored_baseline(tmp_path, KEY, {"s.js": shared.encode()})
    other = stored_baseline(tmp_path, KEY, {"s.js": shared.encode()})
    final = {"app.js": (shared + block(9)).encode()}

    report = run_comparison(
        request(tmp_path, final, (ref(laptop), ref(desktop)), (ref(other, "team-b"),))
    )

    assert report.totals.excluded_common == 0  # two teams, not three devices
    assert report.totals.matched > 0


def test_starter_code_is_excluded(tmp_path: Path) -> None:
    starter = block(10)
    mine = stored_baseline(tmp_path, KEY, {"starter.js": starter.encode()})
    runs = starter_runs([("index.js", starter.encode())], S)
    report = run_comparison(
        request(
            tmp_path, {"app.js": (starter + block(11)).encode()}, (ref(mine),), starters=(runs,)
        )
    )
    assert report.matches == () and report.totals.excluded_starter > 0
