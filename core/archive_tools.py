"""The audit's tools over an extracted archive (section 15.2).

`list_dir`, `read_file` and `grep` answer the model in text and never raise on what it asks:
a bad request gets a sentence starting `error:` that the model can act on. Every path is
normalised and must stay inside the archive's root, links included, so nothing outside the
team's code can be read (invariant 6: the model's arguments are as untrusted as the files).

`grep` searches plain text, case-insensitively, never a regular expression: a pattern written by
the model and run over files written by a team could otherwise take unbounded time (D-053).
"""

import fnmatch
import os
from pathlib import Path, PurePosixPath

MAX_LINES = 400
MAX_CHARS = 40_000
MAX_HITS = 50
MAX_ENTRIES = 200
MAX_FILE_BYTES = 1_048_576
MAX_PATTERN = 200
HIT_CHARS = 200
SKIPPED_DIRS = frozenset(
    {"node_modules", "vendor", "dist", "build", ".next", ".git", "__pycache__", ".venv", "venv"}
)
_SNIFF = 8192


def escape_attribute(text: str) -> str:
    """Text safe inside a double-quoted tag attribute: it cannot close the attribute or the tag."""
    return (
        text.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    )


def normalise(path: str) -> str | None:
    """`path` as a relative POSIX path inside the root ("" for the root), or None."""
    if "\x00" in path:
        return None
    pure = PurePosixPath(path.replace("\\", "/"))
    if pure.is_absolute():
        return None
    parts = [part for part in pure.parts if part not in ("", ".")]
    if ".." in parts:
        return None
    return "/".join(parts)


def is_binary(path: Path) -> bool:
    with path.open("rb") as handle:
        return b"\x00" in handle.read(_SNIFF)


def count_lines(path: Path) -> int:
    """Lines as an editor numbers them: a last line without a newline still counts."""
    lines = 0
    last = b"\n"
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            lines += chunk.count(b"\n")
            last = chunk[-1:]
    return lines + (0 if last == b"\n" else 1)


class ArchiveTools:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def resolve(self, path: str) -> Path | None:
        """The file or folder `path` names, or None when it would lead out of the archive."""
        relative = normalise(path)
        if relative is None:
            return None
        try:
            real = (self.root / relative).resolve()
        except (OSError, RuntimeError):
            return None
        if real != self.root and not real.is_relative_to(self.root):
            return None
        return real

    def line_count(self, path: str) -> int | None:
        """The file's number of lines; None for a missing, binary or unreadable file."""
        target = self.resolve(path)
        if target is None or not target.is_file():
            return None
        try:
            return None if is_binary(target) else count_lines(target)
        except OSError:
            return None

    # --- the tools ---------------------------------------------------------------------------

    def list_dir(self, path: str) -> str:
        target = self.resolve(path)
        if target is None or not target.is_dir():
            return f"error: no such folder: {path}"
        entries = sorted(target.iterdir(), key=lambda entry: entry.name)
        lines = [self._entry(entry) for entry in entries[:MAX_ENTRIES]]
        if len(entries) > MAX_ENTRIES:
            lines.append(f"… and {len(entries) - MAX_ENTRIES} more")
        return "\n".join(lines) if lines else "(empty folder)"

    def read_file(self, path: str, start_line: int, end_line: int) -> str:
        relative = normalise(path)
        target = self.resolve(path)
        if relative is None or target is None or not target.is_file():
            return f"error: no such file: {path}"
        try:
            if is_binary(target):
                return f"error: {relative} is a binary file"
            total = count_lines(target)
            first = max(1, start_line)
            last = min(end_line, total, first + MAX_LINES - 1)
            if total == 0:
                return f"error: {relative} is empty"
            if first > total:
                return f"error: {relative} has only {total} lines"
            if last < first:
                return f"error: end_line {end_line} comes before start_line {start_line}"
            body, shown, cut_by_chars = self._lines(target, first, last)
        except OSError as exc:
            return f"error: {relative} could not be read ({type(exc).__name__})"
        tag = f'<file path="{escape_attribute(relative)}" lines="{first}-{shown}" of="{total}">'
        text = "\n".join([tag, *body, "</file>"])
        if cut_by_chars:
            text += f"\n(stopped at {MAX_CHARS} characters: ask for lines {shown + 1} onwards next)"
        elif shown < min(end_line, total):
            text += f"\n(stopped after {MAX_LINES} lines: ask for lines {shown + 1} onwards next)"
        return text

    def grep(self, pattern: str, glob: str = "*") -> str:
        if not pattern or len(pattern) > MAX_PATTERN:
            return f"error: the pattern must be 1 to {MAX_PATTERN} characters of plain text"
        needle = pattern.casefold()
        hits: list[str] = []
        for relative, file in self.files():
            name = relative.rsplit("/", 1)[-1]
            if not (fnmatch.fnmatch(relative, glob) or fnmatch.fnmatch(name, glob)):
                continue
            try:
                if file.stat().st_size > MAX_FILE_BYTES or is_binary(file):
                    continue
                text = file.read_bytes().decode("utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if needle in line.casefold():
                    if len(hits) == MAX_HITS:
                        return "\n".join([*hits, "… more hits not shown"])
                    hits.append(f"{relative}:{number}: {line.strip()[:HIT_CHARS]}")
        return "\n".join(hits) if hits else "no matches"

    # --- helpers -----------------------------------------------------------------------------

    def files(self) -> list[tuple[str, Path]]:
        """Every regular file outside the skipped folders, as (relative path, path), sorted."""
        found: list[tuple[str, Path]] = []
        for folder, dirs, names in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS)
            base = Path(folder)
            for name in sorted(names):
                file = base / name
                if file.is_symlink() or not file.is_file():
                    continue
                found.append((file.relative_to(self.root).as_posix(), file))
        return found

    def _entry(self, entry: Path) -> str:
        if entry.is_symlink():
            return f"{entry.name} (link)"
        if entry.is_dir():
            if entry.name in SKIPPED_DIRS:
                return f"{entry.name}/ (skipped: dependencies or build output)"
            return f"{entry.name}/"
        return f"{entry.name} ({entry.stat().st_size} bytes)"

    def _lines(self, target: Path, first: int, last: int) -> tuple[list[str], int, bool]:
        body: list[str] = []
        used = 0
        shown = first - 1
        with target.open("rb") as handle:
            for number, raw in enumerate(handle, start=1):
                if number < first:
                    continue
                if number > last:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                line = f"{number:>4} | {text}"
                if used + len(line) > MAX_CHARS and body:
                    return body, shown, True
                body.append(line[:MAX_CHARS])
                used += len(line) + 1
                shown = number
        return body, shown, False
