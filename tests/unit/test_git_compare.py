"""Comparing two commits (section 13.2): `compare_commits` and `RepoProvider.compare`.

H2's four pushed heads give every relation: A then B (ahead), C then D (a rewrite, diverged),
C then A (behind). GitHub's clone path runs over `file://`, as in `test_provider_snapshots.py`.
"""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from core.errors import InfraError
from core.git_compare import compare_commits
from core.providers.github import GitHubProvider
from core.providers.local import LocalGitProvider
from core.repo import Comparison, RepoInfo
from core.snapshots import HISTORY_LIMIT
from scripts.fixtures import load_history_fixtures
from scripts.git_fixtures import PUSH_LOG, build_history_remote
from tests.fakes import FakeClock

KICKOFF = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
H2 = next(h for h in load_history_fixtures() if h.id == "H2")


def git(git_dir: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "--git-dir", str(git_dir), *args], check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def added(git_dir: Path, base: str, head: str) -> int:
    out = git(git_dir, "diff", "--numstat", base, head)
    return sum(int(line.split("\t")[0]) for line in out.splitlines())


@pytest.fixture
def h2(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """H2's bare repository and its heads by commit name."""
    repo = build_history_remote(H2, tmp_path / "remotes", KICKOFF)
    pushed = [push["head"] for push in json.loads((repo / PUSH_LOG).read_text())]
    return repo, dict(zip("ABCD", pushed, strict=True))


async def test_a_descendant_is_ahead_with_its_new_commits(h2: tuple[Path, dict[str, str]]) -> None:
    repo, sha = h2
    result = await compare_commits(repo, sha["A"], sha["B"])

    assert result.status == "ahead"
    assert [c["sha"] for c in result.commits] == [sha["B"]]
    assert result.lines_added == added(repo, sha["A"], sha["B"]) > 0
    assert result.files_changed == 1


async def test_a_rewritten_head_is_diverged(h2: tuple[Path, dict[str, str]]) -> None:
    repo, sha = h2
    result = await compare_commits(repo, sha["C"], sha["D"])

    assert result.status == "diverged"
    assert [c["sha"] for c in result.commits] == [sha["D"]]
    assert result.lines_added == added(repo, sha["C"], sha["D"])
    names = git(repo, "diff", "--name-only", sha["C"], sha["D"]).splitlines()
    assert result.files_changed == len(names)


async def test_an_ancestor_is_behind_and_adds_nothing(h2: tuple[Path, dict[str, str]]) -> None:
    repo, sha = h2
    assert await compare_commits(repo, sha["C"], sha["A"]) == Comparison(
        status="behind", lines_added=0, files_changed=0, commits=[]
    )


async def test_the_same_commit_is_identical(h2: tuple[Path, dict[str, str]]) -> None:
    repo, sha = h2
    assert (await compare_commits(repo, sha["B"], sha["B"])).status == "identical"


async def test_no_base_counts_everything_in_the_head(h2: tuple[Path, dict[str, str]]) -> None:
    repo, sha = h2
    result = await compare_commits(repo, None, sha["A"])

    total = sum(len(v.splitlines()) for v in H2.commits[0].files.values() if v is not None)
    assert (result.status, result.lines_added, result.files_changed) == ("ahead", total, 2)
    assert [c["sha"] for c in result.commits] == [sha["A"]]
    assert result.commits[0]["message"] == "Start the meal planner"


def fast_import(git_dir: Path, stream: bytes) -> str:
    subprocess.run(
        ["git", "--git-dir", str(git_dir), "fast-import", "--quiet", "--date-format=raw"],
        input=stream,
        check=True,
        capture_output=True,
    )
    return git(git_dir, "rev-parse", "refs/heads/main")


def bare(tmp_path: Path) -> Path:
    repo = tmp_path / "r.git"
    subprocess.run(
        ["git", "init", "--quiet", "--bare", "--initial-branch=main", str(repo)], check=True
    )
    return repo


def data(content: bytes) -> bytes:
    return f"data {len(content)}\n".encode() + content + b"\n"


async def test_lines_skip_lockfiles_dependency_folders_and_binary_files(tmp_path: Path) -> None:
    repo = bare(tmp_path)
    files = {
        "package-lock.json": b"{\n" * 500,
        "node_modules/x.js": b"x;\n" * 300,
        "logo.png": b"\x89PNG\r\n\x00\x00binary\n",
        "src/a.js": b"a;\n" * 10,
    }
    stream = b"commit refs/heads/main\ncommitter T <t@t> 1700000000 +0000\n" + data(b"one\n")
    for path, content in files.items():
        stream += f"M 100644 inline {path}\n".encode() + data(content)
    head = fast_import(repo, stream)

    result = await compare_commits(repo, None, head)

    assert (result.lines_added, result.files_changed) == (10, 4)


async def test_new_commits_are_capped_at_the_history_limit(tmp_path: Path) -> None:
    repo = bare(tmp_path)
    stream = b""
    for n in range(HISTORY_LIMIT + 5):
        stream += b"commit refs/heads/main\n"
        stream += f"committer T <t@t> {1700000000 + n} +0000\n".encode() + data(f"c{n}\n".encode())
        stream += b"M 100644 inline f.txt\n" + data(f"{n}\n".encode())
    head = fast_import(repo, stream)

    result = await compare_commits(repo, None, head)

    assert len(result.commits) == HISTORY_LIMIT
    assert result.commits[0]["sha"] == head


# --- the providers -------------------------------------------------------------------------------


def info(provider: str, url: str) -> RepoInfo:
    return RepoInfo(
        provider=provider,  # type: ignore[arg-type]
        id="4242",
        url=url,
        default_branch="main",
        created_at=KICKOFF,
        is_public=True,
    )


GITHUB_H2 = info("github", "https://github.com/acme/H2-force-push")


async def test_local_compare_reads_the_bare_repository(h2: tuple[Path, dict[str, str]]) -> None:
    repo, sha = h2
    provider = LocalGitProvider(repo.parent, FakeClock(KICKOFF))
    local = info("local", "local://H2-force-push")

    assert (await provider.compare(local, sha["C"], sha["D"])).status == "diverged"
    assert (await provider.compare(local, None, sha["A"])).status == "ahead"
    gone = await provider.compare(local, "f" * 40, sha["D"])
    assert gone == Comparison(status="diverged", lines_added=0, files_changed=0, commits=[])


def commit_json(sha: str, when: str, message: str) -> dict[str, object]:
    return {
        "sha": sha,
        "commit": {"author": {"date": when}, "committer": {"date": when}, "message": message},
    }


async def test_github_compare_maps_the_api_answer() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "status": "ahead",
                "files": [
                    {"filename": "package-lock.json", "additions": 900},
                    {"filename": "src/app.py", "additions": 42},
                ],
                "commits": [  # GitHub lists them oldest first
                    commit_json("1" * 40, "2026-11-14T09:10:00Z", "first\n\nbody"),
                    commit_json("2" * 40, "2026-11-14T09:20:00Z", "second"),
                ],
            },
        )

    provider = GitHubProvider("t", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = await provider.compare(GITHUB_H2, "a" * 40, "b" * 40)

    assert seen == [
        f"https://api.github.com/repos/acme/H2-force-push/compare/{'a' * 40}...{'b' * 40}"
    ]
    assert (result.status, result.lines_added, result.files_changed) == ("ahead", 42, 2)
    assert [c["sha"] for c in result.commits] == ["2" * 40, "1" * 40]
    assert result.commits[1] == {
        "sha": "1" * 40,
        "author_date": "2026-11-14T09:10:00Z",
        "committer_date": "2026-11-14T09:10:00Z",
        "message": "first",
    }


@pytest.fixture
def origin(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """H2 laid out as GitHub holds it (`acme/H2-force-push`), every head fetchable by SHA."""
    repo = build_history_remote(H2, tmp_path / "origin" / "acme", KICKOFF)
    git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    pushed = [push["head"] for push in json.loads((repo / PUSH_LOG).read_text())]
    return repo, dict(zip("ABCD", pushed, strict=True))


def cloning(tmp_path: Path, handler: httpx.MockTransport) -> GitHubProvider:
    return GitHubProvider(
        "t",
        httpx.AsyncClient(transport=handler),
        clone_base=f"file://{tmp_path / 'origin'}",
        work_dir=tmp_path / "clones",
    )


async def test_github_compare_of_300_files_counts_lines_from_a_clone(
    origin: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    repo, sha = origin
    many = [{"filename": f"f{n}.txt", "additions": 1} for n in range(300)]
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"status": "ahead", "files": many, "commits": []})
    )

    result = await cloning(tmp_path, transport).compare(GITHUB_H2, sha["A"], sha["B"])

    assert result.lines_added == added(repo, sha["A"], sha["B"])
    assert [c["sha"] for c in result.commits] == [sha["B"]]
    assert list((tmp_path / "clones").iterdir()) == []


async def test_github_compare_without_a_base_uses_a_clone(
    origin: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    _repo, sha = origin

    def no_api(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"the API was called: {request.url}")

    result = await cloning(tmp_path, httpx.MockTransport(no_api)).compare(GITHUB_H2, None, sha["D"])

    assert result.status == "ahead"
    assert [c["sha"] for c in result.commits] == [sha["D"], sha["A"]]


async def test_github_compare_whose_base_is_gone_is_diverged_with_no_counts(
    origin: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    _repo, sha = origin
    transport = httpx.MockTransport(lambda _: httpx.Response(404, json={"message": "Not Found"}))

    result = await cloning(tmp_path, transport).compare(GITHUB_H2, "f" * 40, sha["D"])

    assert result == Comparison(status="diverged", lines_added=0, files_changed=0, commits=[])


async def test_github_compare_rate_limit_is_an_infrastructure_error() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(403, json={}, headers={"x-ratelimit-remaining": "0"})
    )
    provider = GitHubProvider("t", httpx.AsyncClient(transport=transport))

    with pytest.raises(InfraError):
        await provider.compare(GITHUB_H2, "a" * 40, "b" * 40)
