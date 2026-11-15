"""Comparing two commits of one repository with git (section 13.2), for the repo watch."""

from pathlib import Path

from core.git import GitFailed, run_git
from core.repo import Comparison
from core.snapshots import commit_history, diff_counts


async def compare_commits(git_dir: Path, base: str | None, head: str) -> Comparison:
    """How `head` relates to `base`: what it adds and the commits that are new in it.

    `base=None` means the repository had no commit before, so all of `head` is new.
    A head that does not descend from the base is `diverged` (a rewritten history); its counts
    are those of the plain diff between the two.
    """
    if base is None:
        lines, files = await diff_counts(git_dir, None, head)
        return Comparison(
            status="ahead",
            lines_added=lines,
            files_changed=files,
            commits=await commit_history(git_dir, head),
        )
    if base == head:
        return Comparison(status="identical", lines_added=0, files_changed=0, commits=[])
    if await _is_ancestor(git_dir, head, base):
        return Comparison(status="behind", lines_added=0, files_changed=0, commits=[])
    status = "ahead" if await _is_ancestor(git_dir, base, head) else "diverged"
    lines, files = await diff_counts(git_dir, base, head)
    return Comparison(
        status=status,
        lines_added=lines,
        files_changed=files,
        commits=await commit_history(git_dir, f"{base}..{head}"),
    )


async def _is_ancestor(git_dir: Path, older: str, newer: str) -> bool:
    try:
        await run_git("merge-base", "--is-ancestor", older, newer, git_dir=git_dir)
    except GitFailed as exc:
        if exc.returncode == 1:  # git's "no"; anything else is a real failure
            return False
        raise
    return True
