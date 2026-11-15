"""An organiser's starter kit, read from a `.zip` or `.tar.gz` (M9, Task 15).

Code a starter holds is excluded from every comparison: a team that used it has not brought
prior work. Only the starter's runs are kept, never its files. The archive is untrusted input
like any other, so it is read without extracting, and a link, or a path that leaves it, refuses
the whole archive.
"""

import io
import stat
import tarfile
import zipfile
from collections.abc import Callable, Collection
from pathlib import PurePosixPath

from core.priorwork.compare import eligible

STARTER_MAX_BYTES = 5_000_000  # of eligible files, in all


class BadStarter(Exception):
    """Not a zip or a gzip tar, or a member that is a link or leaves the archive."""


class StarterTooLarge(Exception):
    """The starter's eligible files pass STARTER_MAX_BYTES."""


def _safe(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise BadStarter(f"{name}: the path leaves the archive")
    return path.as_posix()


Wanted = Callable[[str, int], bool]  # whether to read the file at this path, of this size


def _tar_files(data: bytes, wanted: Wanted) -> list[tuple[str, bytes]]:
    found = []
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                path = _safe(member.name)
                if member.issym() or member.islnk():
                    raise BadStarter(f"{member.name}: a link")
                try:
                    tarfile.data_filter(member, "/starter")
                except tarfile.FilterError as exc:
                    raise BadStarter(f"{member.name}: {exc}") from None
                if member.isreg() and wanted(path, member.size):
                    stream = tar.extractfile(member)
                    assert stream is not None  # a regular file has one
                    found.append((path, stream.read()))
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BadStarter(f"not a readable .tar.gz: {exc}") from exc
    return found


def _zip_files(data: bytes, wanted: Wanted) -> list[tuple[str, bytes]]:
    found = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                path = _safe(info.filename)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise BadStarter(f"{info.filename}: a link")
                if not info.is_dir() and wanted(path, info.file_size):
                    found.append((path, archive.read(info)))
    except (zipfile.BadZipFile, OSError, EOFError) as exc:
        raise BadStarter(f"not a readable .zip: {exc}") from exc
    return found


def starter_files(
    data: bytes, extensions: Collection[str], max_file_bytes: int
) -> list[tuple[str, bytes]]:
    """The eligible files of a starter archive, as (path, bytes), in path order."""
    total = 0

    def wanted(path: str, size: int) -> bool:
        nonlocal total
        if not eligible(path, size, extensions, max_file_bytes):
            return False
        total += size
        if total > STARTER_MAX_BYTES:
            raise StarterTooLarge(f"the starter's code is over {STARTER_MAX_BYTES} bytes")
        return True

    if data[:2] == b"\x1f\x8b":
        return sorted(_tar_files(data, wanted))
    if data[:4] == b"PK\x03\x04":
        return sorted(_zip_files(data, wanted))
    raise BadStarter("a starter is a .zip or a .tar.gz")
