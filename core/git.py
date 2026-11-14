"""Running git. Every git command in HackProof goes through `run_git`."""

import asyncio
import os
from pathlib import Path

from core.errors import InfraError

TIMEOUT_S = 120.0


class GitFailed(InfraError):
    def __init__(self, args: tuple[str, ...], returncode: int, stderr: str) -> None:
        super().__init__(f"git {args[0]} failed ({returncode}): {stderr}")
        self.returncode = returncode
        self.stderr = stderr


async def run_git(
    *args: str,
    git_dir: Path | None = None,
    cwd: Path | None = None,
    check: bool = True,
    timeout_s: float = TIMEOUT_S,
) -> str:
    """Run git and return its stdout, stripped.

    With `check`, a non-zero exit raises `GitFailed`. A command still running after `timeout_s`
    is killed and raises `InfraError`. Git never prompts: a repository that wants credentials
    fails instead of hanging.
    """
    command = ["git", *(["--git-dir", str(git_dir)] if git_dir else []), *args]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise InfraError(f"git {args[0]} timed out after {timeout_s:.0f} s") from None
    if check and process.returncode != 0:
        message = stderr.decode(errors="replace").strip()[-500:]
        raise GitFailed(args, process.returncode or 1, message)
    return stdout.decode(errors="replace").strip()


async def has_commit(git_dir: Path, sha: str) -> bool:
    try:
        await run_git("cat-file", "-e", f"{sha}^{{commit}}", git_dir=git_dir)
    except GitFailed:
        return False
    return True
