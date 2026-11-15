"""Snapshots (section 12.1): one commit as a content-addressed archive, and reading it back.

Once a snapshot exists nothing reads the live repository again. Every later step extracts the
archive into its own work directory with `extract_archive`, which checks the hash first and
refuses any path or link that would land outside that directory.
"""

import asyncio
import hashlib
import os
import re
import tarfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath

from core.errors import InfraError
from core.git import has_commit, run_git
from core.repo import CommitNotFound, InvalidCommit, SnapshotFiles

HISTORY_LIMIT = 200
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

# What line counts leave out (section 13.2), besides binary files.
_SKIPPED_DIRS = {"node_modules", "vendor", "dist", "build", ".next"}
_LOCKFILES = {
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lock",
    "bun.lockb",
    "poetry.lock",
    "Pipfile.lock",
    "uv.lock",
    "pdm.lock",
    "Cargo.lock",
    "go.sum",
    "composer.lock",
    "Gemfile.lock",
    "mix.lock",
    "pubspec.lock",
    "Podfile.lock",
    "packages.lock.json",
}


class UnsafeArchive(Exception):
    """The commit holds a path or link that would leave the folder it is extracted into."""


class ArchiveChanged(InfraError):
    """A stored archive no longer matches the SHA-256 recorded for it."""


def check_commit_sha(sha: str) -> str:
    if not _SHA.match(sha):
        raise InvalidCommit("a snapshot needs a full commit SHA, not a branch name or short SHA")
    return sha


def counts_toward_lines(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts or _SKIPPED_DIRS & set(parts[:-1]):
        return False
    name = parts[-1]
    return name not in _LOCKFILES and ".min." not in name


async def archive_commit(git_dir: Path, sha: str, archives_dir: Path) -> SnapshotFiles:
    """Store `sha` as `archives_dir/<sha256>.tar.gz` and describe it."""
    check_commit_sha(sha)
    if not await has_commit(git_dir, sha):
        raise CommitNotFound(f"the repository has no commit {sha}")

    await asyncio.to_thread(archives_dir.mkdir, parents=True, exist_ok=True)
    partial = archives_dir / f".{uuid.uuid4().hex}.partial"
    try:
        await run_git("archive", "--format=tar.gz", f"--output={partial}", sha, git_dir=git_dir)
        archive, digest = await asyncio.to_thread(_seal, partial, archives_dir)
    finally:
        await asyncio.to_thread(partial.unlink, missing_ok=True)

    return SnapshotFiles(
        commit_sha=sha,
        archive_path=archive,
        archive_sha256=digest,
        commit_history=await commit_history(git_dir, sha),
        lines_total=(await diff_counts(git_dir, None, sha))[0],
    )


def extract_archive(archive: Path, expected_sha256: str, dest: Path) -> None:
    """Extract a stored snapshot into `dest`, after checking it is the archive recorded."""
    if _sha256_file(archive) != expected_sha256:
        raise ArchiveChanged(f"{archive.name} does not match its recorded SHA-256")
    with tarfile.open(archive, "r:gz") as tar:
        _check(tar)  # before writing anything, so a bad archive leaves nothing behind
        dest.mkdir(parents=True, exist_ok=True)
        tar.extractall(dest, filter="data")


def read_archive(
    archive: Path, expected_sha256: str, wanted: Callable[[str, int], bool]
) -> Iterator[tuple[str, bytes]]:
    """The regular files of a stored snapshot that `wanted(path, size)` asks for, read without
    extracting anything. The archive is checked as `extract_archive` checks it."""
    if _sha256_file(archive) != expected_sha256:
        raise ArchiveChanged(f"{archive.name} does not match its recorded SHA-256")
    with tarfile.open(archive, "r:gz") as tar:
        _check(tar)
        for member in tar.getmembers():
            path = PurePosixPath(member.name).as_posix()
            if not member.isreg() or not wanted(path, member.size):
                continue
            stream = tar.extractfile(member)
            assert stream is not None  # a regular file always has one
            yield path, stream.read()


# --- helpers -------------------------------------------------------------------------------------


async def commit_history(git_dir: Path, *revisions: str) -> list[dict[str, str]]:
    """Self-reported commits of `revisions` (a SHA, or `base..head`), newest first, capped."""
    out = await run_git(
        "log", f"-n{HISTORY_LIMIT}", "-z", "--format=%H%x1f%aI%x1f%cI%x1f%s", *revisions,
        "--", git_dir=git_dir,
    )  # fmt: skip
    history = []
    for record in filter(None, out.split("\0")):
        commit, author_date, committer_date, message = record.strip("\n").split("\x1f", 3)
        history.append(
            {
                "sha": commit,
                "author_date": author_date,
                "committer_date": committer_date,
                "message": message,
            }
        )
    return history


async def diff_counts(git_dir: Path, base: str | None, head: str) -> tuple[int, int]:
    """Lines added that count (section 13.2) and every path changed, from `base` to `head`.

    With no base, the diff is from the empty tree, so everything in `head` counts.
    """
    if base is None:
        base = await run_git("hash-object", "-t", "tree", os.devnull, git_dir=git_dir)
    out = await run_git(
        "diff", "--numstat", "-z", "--no-renames", "--no-ext-diff", "--no-textconv",
        base, head, git_dir=git_dir,
    )  # fmt: skip
    lines = files = 0
    for record in filter(None, out.split("\0")):
        added, _deleted, path = record.split("\t", 2)
        files += 1
        if added != "-" and counts_toward_lines(path):  # "-" marks a binary file
            lines += int(added)
    return lines, files


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _seal(partial: Path, archives_dir: Path) -> tuple[Path, str]:
    """Check a fresh archive and move it to its content address. Returns the path and hash."""
    with tarfile.open(partial, "r:gz") as tar:
        _check(tar)
    digest = _sha256_file(partial)
    archive = archives_dir / f"{digest}.tar.gz"
    os.replace(partial, archive)  # atomic; the same commit always gives the same bytes
    return archive, digest


def _check(tar: tarfile.TarFile) -> None:
    """Refuse members that leave the destination, before extracting any of them."""
    for member in tar.getmembers():
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts:
            raise UnsafeArchive(f"{member.name}: the path leaves the project folder")
        try:
            tarfile.data_filter(member, "/snapshot")
        except tarfile.FilterError as exc:
            raise UnsafeArchive(f"{member.name}: {exc}") from None
