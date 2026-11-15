"""Fixtures as bare repositories under `LOCAL_REMOTES_DIR`, for `LocalGitProvider` (D-006, D-018).

Every repository is written in one `git fast-import` stream with fixed names, messages and
dates, so the same fixture and kickoff always give the same commit SHAs.

A local repository has no server to say when each head first appeared, so the builder writes
that down: `PUSH_LOG` inside the bare repository lists `{"at", "head"}` per push, in order, and
`refs/pushes/<n>` holds every pushed head so a rewritten history stays inspectable and
`git gc` cannot prune it. The fixture suite steps a fake clock through this log (section 20.3).
"""

import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from scripts.fixtures import HistoryFixture, Variant

PUSH_LOG = "hackproof-pushes.json"
APP_PUSHED_AT = 60  # minutes after kickoff: one ordinary push, too small for any signal
IDENTITY = "Fixture Team <team@fixtures.invalid>"


@dataclass(frozen=True)
class _Commit:
    name: str
    parent: str | None
    authored: int
    committed: int
    message: str
    files: dict[str, tuple[bytes, bool] | None]  # path: (content, executable); None deletes


def build_app_remote(
    variant: Variant, dest: Path, kickoff: datetime, *, name: str | None = None
) -> Path:
    """One commit holding the variant's files, pushed an hour after kickoff, as `name`.git
    (the variant's remote name by default)."""
    files: dict[str, tuple[bytes, bool] | None] = {
        file.relative_to(variant.path).as_posix(): (file.read_bytes(), _executable(file))
        for file in variant.files()
    }
    commit = _Commit(
        name="app",
        parent=None,
        authored=APP_PUSHED_AT,
        committed=APP_PUSHED_AT,
        # Neutral on purpose: the audit and the brief read commit messages, and the variant's
        # name ("broken") would hand them the answer.
        message="Hackathon submission\n",
        files=files,
    )
    return _build(name or variant.remote_name, [commit], [(APP_PUSHED_AT, "app")], dest, kickoff)


def build_history_remote(
    history: HistoryFixture, dest: Path, kickoff: datetime, *, name: str | None = None
) -> Path:
    """The scripted timeline: every commit, with `main` at the last pushed head, as `name`.git
    (the history's slug by default)."""
    commits = [
        _Commit(
            name=c.name,
            parent=c.parent,
            authored=c.authored,
            committed=c.committed,
            message=c.message if c.message.endswith("\n") else f"{c.message}\n",
            files={p: None if v is None else (v, False) for p, v in c.files.items()},
        )
        for c in history.commits
    ]
    pushes = [(p.at, p.head) for p in history.pushes]
    return _build(name or history.slug, commits, pushes, dest, kickoff)


def _build(
    name: str,
    commits: list[_Commit],
    pushes: list[tuple[int, str]],
    dest: Path,
    kickoff: datetime,
) -> Path:
    if kickoff.utcoffset() is None:
        raise ValueError("kickoff must be timezone-aware")
    if not name.replace("-", "").isalnum():
        raise ValueError(f"not a repository name: {name!r}")
    repo = dest / f"{name}.git"
    if repo.exists():
        shutil.rmtree(repo)  # a rebuild replaces the repository, never adds to it
    dest.mkdir(parents=True, exist_ok=True)
    # Repositories are built on the host and read by the worker's older git (2.39), so pin the
    # formats it understands instead of inheriting the host's defaults (reftable, SHA-256).
    _git(
        repo,
        "-c",
        "init.defaultRefFormat=files",
        "init",
        "--quiet",
        "--bare",
        "--object-format=sha1",
        "--initial-branch=main",
        str(repo),
        git_dir=False,
    )

    marks = {commit.name: n for n, commit in enumerate(commits, start=1)}
    stream = bytearray()
    for commit in commits:
        stream += b"reset refs/heads/main\n"  # the parent is always explicit below
        stream += b"commit refs/heads/main\n"
        stream += f"mark :{marks[commit.name]}\n".encode()
        stream += f"author {IDENTITY} {_raw_date(kickoff, commit.authored)}\n".encode()
        stream += f"committer {IDENTITY} {_raw_date(kickoff, commit.committed)}\n".encode()
        stream += _data(commit.message.encode())
        if commit.parent is not None:
            stream += f"from :{marks[commit.parent]}\n".encode()
        for path, change in sorted(commit.files.items()):
            if change is None:
                stream += f"D {path}\n".encode()
            else:
                content, executable = change
                stream += f"M {'100755' if executable else '100644'} inline {path}\n".encode()
                stream += _data(content)
        stream += b"\n"
    for n, (_, head) in enumerate(pushes, start=1):
        stream += f"reset refs/pushes/{n:02d}\nfrom :{marks[head]}\n\n".encode()
    stream += f"reset refs/heads/main\nfrom :{marks[pushes[-1][1]]}\n\n".encode()
    subprocess.run(
        ["git", "--git-dir", str(repo), "fast-import", "--quiet", "--date-format=raw"],
        input=bytes(stream),
        check=True,
        capture_output=True,
    )

    log = [
        {
            "at": (kickoff + timedelta(minutes=at)).isoformat(),
            "head": _git(repo, "rev-parse", f"refs/pushes/{n:02d}"),
        }
        for n, (at, _) in enumerate(pushes, start=1)
    ]
    (repo / PUSH_LOG).write_text(json.dumps(log, indent=2) + "\n")
    return repo


def _raw_date(kickoff: datetime, minutes: int) -> str:
    """Git's raw date format, always in UTC so the result does not depend on the machine."""
    return f"{int((kickoff + timedelta(minutes=minutes)).timestamp())} +0000"


def _data(content: bytes) -> bytes:
    return f"data {len(content)}\n".encode() + content + b"\n"


def _executable(path: Path) -> bool:
    return bool(path.stat().st_mode & 0o111)


def _git(repo: Path, *args: str, git_dir: bool = True) -> str:
    command = ["git", "--git-dir", str(repo), *args] if git_dir else ["git", *args]
    done = subprocess.run(command, check=True, capture_output=True, text=True)
    return done.stdout.strip()
