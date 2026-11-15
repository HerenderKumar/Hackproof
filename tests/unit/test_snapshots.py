"""Snapshots (section 12.1): a commit becomes a content-addressed archive, read back safely."""

import hashlib
import io
import json
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.errors import InfraError
from core.repo import CommitNotFound, InvalidCommit
from core.snapshots import (
    UnsafeArchive,
    archive_commit,
    counts_toward_lines,
    extract_archive,
    read_archive,
)
from scripts.fixtures import HistoryCommit, HistoryFixture, Push, load_history_fixtures
from scripts.git_fixtures import PUSH_LOG, build_history_remote

KICKOFF = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
H1 = next(h for h in load_history_fixtures() if h.id == "H1")


def heads(repo: Path) -> list[str]:
    return [push["head"] for push in json.loads((repo / PUSH_LOG).read_text())]


def one_commit_repo(tmp_path: Path, files: dict[str, bytes]) -> tuple[Path, str]:
    commit = HistoryCommit("only", None, 1, 1, "Add files", dict(files))
    history = HistoryFixture("H9", "H9-test", "test", [], [commit], [Push(1, "only")])
    repo = build_history_remote(history, tmp_path / "remotes", KICKOFF)
    return repo, heads(repo)[0]


def member_names(archive: Path) -> list[str]:
    with tarfile.open(archive) as tar:
        return sorted(m.name for m in tar.getmembers() if m.isfile())


@pytest.fixture
def h1(tmp_path: Path) -> Path:
    return build_history_remote(H1, tmp_path / "remotes", KICKOFF)


# --- archiving --------------------------------------------------------------------------------


async def test_a_commit_becomes_an_archive_named_by_its_sha256(h1: Path, tmp_path: Path) -> None:
    head = heads(h1)[-1]

    files = await archive_commit(h1, head, tmp_path / "archives")

    assert files.commit_sha == head
    assert files.archive_path == tmp_path / "archives" / f"{files.archive_sha256}.tar.gz"
    assert hashlib.sha256(files.archive_path.read_bytes()).hexdigest() == files.archive_sha256
    assert member_names(files.archive_path) == ["NOTES.md", "README.md", "src/engine.js"]


async def test_an_earlier_commit_is_archived_as_it_was(h1: Path, tmp_path: Path) -> None:
    first = heads(h1)[0]

    files = await archive_commit(h1, first, tmp_path / "archives")

    assert member_names(files.archive_path) == ["README.md"]
    assert [c["sha"] for c in files.commit_history] == [first]


async def test_archiving_a_commit_twice_gives_the_same_file(h1: Path, tmp_path: Path) -> None:
    archives = tmp_path / "archives"

    first = await archive_commit(h1, heads(h1)[-1], archives)
    second = await archive_commit(h1, heads(h1)[-1], archives)

    assert first == second
    assert list(archives.iterdir()) == [first.archive_path]  # no temporary files left behind


async def test_the_history_is_newest_first_with_self_reported_dates(
    h1: Path, tmp_path: Path
) -> None:
    files = await archive_commit(h1, heads(h1)[-1], tmp_path / "archives")

    history = files.commit_history
    assert [c["message"] for c in history] == [
        "Add the routing engine",
        "Add design notes",
        "Kickoff: README",
    ]
    assert all(set(c) == {"sha", "author_date", "committer_date", "message"} for c in history)
    backdated = history[1]
    assert datetime.fromisoformat(backdated["author_date"]) == KICKOFF - timedelta(days=10)
    assert datetime.fromisoformat(backdated["committer_date"]) == KICKOFF + timedelta(minutes=3)


async def test_the_history_keeps_the_latest_200_commits(tmp_path: Path) -> None:
    commits = [
        HistoryCommit(
            f"c{n}", f"c{n - 1}" if n else None, n, n, f"commit {n}", {"n.txt": b"%d\n" % n}
        )
        for n in range(205)
    ]
    history = HistoryFixture("H9", "H9-long", "long", [], commits, [Push(300, "c204")])
    repo = build_history_remote(history, tmp_path / "remotes", KICKOFF)

    files = await archive_commit(repo, heads(repo)[0], tmp_path / "archives")

    assert len(files.commit_history) == 200
    assert files.commit_history[0]["message"] == "commit 204"


async def test_lines_total_counts_the_project_s_own_text(tmp_path: Path) -> None:
    repo, sha = one_commit_repo(
        tmp_path,
        {
            "src/app.js": b"one\ntwo\nthree\n",
            "README.md": b"# App\nIt works.\n",
            "package-lock.json": b"{\n}\n" * 25,
            "node_modules/left-pad/index.js": b"x\n" * 10,
            "dist/bundle.js": b"x\n" * 7,
            "vendor/lib.go": b"x\n" * 4,
            "build/out.txt": b"x\n" * 2,
            ".next/cache.json": b"x\n" * 2,
            "public/app.min.js": b"x\n" * 5,
            "logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x01",
        },
    )

    files = await archive_commit(repo, sha, tmp_path / "archives")

    assert files.lines_total == 5


@pytest.mark.parametrize(
    ("path", "counted"),
    [
        ("src/app.js", True),
        ("rebuild.sh", True),
        ("docs/building.md", True),
        ("minimal.js", True),
        ("node_modules/a.js", False),
        ("web/node_modules/a.js", False),
        ("vendor/x.go", False),
        ("dist/a.js", False),
        ("build/a.js", False),
        (".next/a.js", False),
        ("package-lock.json", False),
        ("web/yarn.lock", False),
        ("pnpm-lock.yaml", False),
        ("uv.lock", False),
        ("poetry.lock", False),
        ("go.sum", False),
        ("Cargo.lock", False),
        ("app.min.js", False),
        ("styles.min.css", False),
    ],
)
def test_which_paths_count_toward_lines(path: str, counted: bool) -> None:
    assert counts_toward_lines(path) is counted


async def test_a_commit_the_repository_does_not_have_is_reported(h1: Path, tmp_path: Path) -> None:
    with pytest.raises(CommitNotFound):
        await archive_commit(h1, "0" * 40, tmp_path / "archives")


@pytest.mark.parametrize("sha", ["main", "HEAD", "abc1234", "--output=/tmp/x", "g" * 40, ""])
async def test_only_a_full_commit_sha_is_accepted(h1: Path, tmp_path: Path, sha: str) -> None:
    with pytest.raises(InvalidCommit):
        await archive_commit(h1, sha, tmp_path / "archives")


async def test_a_commit_with_a_link_out_of_the_project_is_refused(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()

    def run(*args: str) -> str:
        env = {
            "GIT_AUTHOR_DATE": "2026-11-14T10:00:00Z",
            "GIT_COMMITTER_DATE": "2026-11-14T10:00:00Z",
        }
        done = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t.invalid", *args],
            cwd=work,
            check=True,
            capture_output=True,
            text=True,
            env={**env, "PATH": "/usr/bin:/bin"},
        )
        return done.stdout.strip()

    run("init", "--quiet", "-b", "main")
    (work / "README.md").write_text("hi\n")
    (work / "passwords").symlink_to("/etc/passwd")
    run("add", ".")
    run("commit", "--quiet", "-m", "link")

    with pytest.raises(UnsafeArchive, match="passwords"):
        await archive_commit(work / ".git", run("rev-parse", "HEAD"), tmp_path / "archives")
    assert not list((tmp_path / "archives").glob("*"))


# --- extracting ------------------------------------------------------------------------------


async def test_an_archive_extracts_to_the_files_of_its_commit(h1: Path, tmp_path: Path) -> None:
    files = await archive_commit(h1, heads(h1)[-1], tmp_path / "archives")
    dest = tmp_path / "work" / "attempt" / "src"

    extract_archive(files.archive_path, files.archive_sha256, dest)

    assert (dest / "README.md").read_text().startswith("# Route planner")
    assert len((dest / "src" / "engine.js").read_text().splitlines()) == 3000


async def test_an_archive_that_no_longer_matches_its_hash_is_refused(
    h1: Path, tmp_path: Path
) -> None:
    files = await archive_commit(h1, heads(h1)[-1], tmp_path / "archives")
    files.archive_path.write_bytes(files.archive_path.read_bytes() + b"tampered")
    dest = tmp_path / "src"

    with pytest.raises(InfraError, match="SHA-256"):
        extract_archive(files.archive_path, files.archive_sha256, dest)
    assert not dest.exists()


def tar_with(member: tarfile.TarInfo, content: bytes = b"") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.addfile(member, io.BytesIO(content) if member.isfile() else None)
    return buffer.getvalue()


def file_member(name: str, size: int) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.size = size
    return member


def link_member(name: str, target: str, kind: bytes) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.type = kind
    member.linkname = target
    return member


@pytest.mark.parametrize(
    "member",
    [
        file_member("../escape.txt", 3),
        file_member("/escape.txt", 3),
        link_member("passwords", "/etc/passwd", tarfile.SYMTYPE),
        link_member("up", "../../escape.txt", tarfile.SYMTYPE),
        link_member("hard", "/etc/passwd", tarfile.LNKTYPE),
    ],
    ids=["dot-dot path", "absolute path", "absolute symlink", "symlink upward", "hard link"],
)
def test_an_archive_that_reaches_outside_its_folder_is_refused(
    tmp_path: Path, member: tarfile.TarInfo
) -> None:
    data = tar_with(member, b"bad")
    archive = tmp_path / "bad.tar.gz"
    archive.write_bytes(data)
    dest = tmp_path / "deep" / "src"

    with pytest.raises(UnsafeArchive):
        extract_archive(archive, hashlib.sha256(data).hexdigest(), dest)
    assert not (tmp_path / "deep" / "escape.txt").exists()
    assert not (tmp_path / "escape.txt").exists()


# --- reading without extracting (M9) -------------------------------------------------------


async def test_an_archives_files_are_read_without_extracting(tmp_path: Path) -> None:
    repo, sha = one_commit_repo(
        tmp_path, {"src/a.js": b"let a = 1;\n", "README.md": b"# r\n", "big.js": b"x" * 5000}
    )
    files = await archive_commit(repo, sha, tmp_path / "archives")
    before = sorted(p.name for p in tmp_path.rglob("*"))

    asked: list[tuple[str, int]] = []

    def wanted(path: str, size: int) -> bool:
        asked.append((path, size))
        return path.endswith(".js") and size < 1000

    read = dict(read_archive(files.archive_path, files.archive_sha256, wanted))

    assert read == {"src/a.js": b"let a = 1;\n"}
    assert ("big.js", 5000) in asked and ("README.md", 4) in asked
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_reading_an_archive_that_changed_or_is_unsafe_is_refused(tmp_path: Path) -> None:
    data = tar_with(link_member("up", "../../escape.txt", tarfile.SYMTYPE))
    archive = tmp_path / "bad.tar.gz"
    archive.write_bytes(data)
    with pytest.raises(UnsafeArchive):
        list(read_archive(archive, hashlib.sha256(data).hexdigest(), lambda _p, _s: True))
    with pytest.raises(InfraError, match="SHA-256"):
        list(read_archive(archive, "0" * 64, lambda _p, _s: True))
