"""Check one uploaded part of a kickoff scan before anything of it is kept (M9, A4).

A part comes from a participant's device, so every byte is untrusted: a changed scanner could
send a gzip that unpacks to gigabytes, a link, or a member named like a path. A part is a gzip
tar whose first member is `manifest.json`, followed by regular files each named by the SHA-256
of its bytes. The tar is read as a stream and never extracted; reading stops at
MAX_UNPACKED_BYTES. Member names are never used as file names.
"""

import gzip
import hashlib
import io
import json
import re
import tarfile
import zlib
from dataclasses import dataclass
from typing import Any

MAX_UNPACKED_BYTES = 8 * 1024 * 1024
MAX_PATH_CHARS = 4096
MANIFEST = "manifest.json"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_FIELDS = {"path", "size", "sha256_device", "sha256_uploaded", "keys_blanked"}


class BadPart(Exception):
    """The part is refused; the message says why."""


@dataclass(frozen=True)
class Entry:
    """One file the device found. `size` and `sha256_uploaded` are of the bytes sent (keys
    blanked); `sha256_device` is of the bytes as they were on the device."""

    path: str
    size: int
    sha256_device: str
    sha256_uploaded: str
    keys_blanked: int


@dataclass(frozen=True)
class Part:
    entries: tuple[Entry, ...]
    contents: dict[str, bytes]  # sha256_uploaded to bytes, for contents first sent in this part


class _Limited(io.RawIOBase):
    """Reads the unpacked stream and refuses to go past the limit."""

    def __init__(self, stream: gzip.GzipFile, limit: int) -> None:
        self._stream, self._limit, self._read = stream, limit, 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        view = memoryview(buffer)
        data = self._stream.read(min(len(view), self._limit + 1 - self._read))
        self._read += len(data)
        if self._read > self._limit:
            raise BadPart(f"the part unpacks to more than {self._limit} bytes")
        view[: len(data)] = data
        return len(data)


def _entry(raw: object) -> Entry:
    if not isinstance(raw, dict) or set(raw) != _FIELDS:
        raise BadPart(f"a manifest entry must have exactly the fields {sorted(_FIELDS)}")
    path, size, device, uploaded, blanked = (
        raw["path"], raw["size"], raw["sha256_device"], raw["sha256_uploaded"], raw["keys_blanked"]
    )  # fmt: skip
    if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS:
        raise BadPart(f"a manifest path must be text of 1 to {MAX_PATH_CHARS} characters")
    for number in (size, blanked):
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            raise BadPart("a manifest size or key count must be a whole number")
    for digest in (device, uploaded):
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            raise BadPart("a manifest hash must be 64 lowercase hex characters")
    return Entry(path, size, device, uploaded, blanked)


def _manifest(tar: tarfile.TarFile) -> tuple[Entry, ...]:
    first = tar.next()
    if first is None or first.name != MANIFEST or not first.isreg():
        raise BadPart(f"the first member of a part must be {MANIFEST}")
    stream = tar.extractfile(first)
    assert stream is not None  # a regular file always has a stream
    try:
        raw = json.loads(stream.read())
    except (ValueError, UnicodeDecodeError) as exc:
        raise BadPart(f"{MANIFEST} is not JSON") from exc
    if not isinstance(raw, list):
        raise BadPart(f"{MANIFEST} must be a list of entries")
    return tuple(_entry(item) for item in raw)


def check_part(data: bytes, max_file_bytes: int) -> Part:
    """The part's entries and the contents it carries, or BadPart with the reason."""
    try:
        unpacked = _Limited(gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb"), MAX_UNPACKED_BYTES)
        with tarfile.open(fileobj=io.BufferedReader(unpacked), mode="r|") as tar:
            entries = _manifest(tar)
            named = {e.sha256_uploaded for e in entries}
            contents: dict[str, bytes] = {}
            # Not `for member in tar`: that starts again from the manifest.
            while (member := tar.next()) is not None:
                contents.update(_member(tar, member, named, max_file_bytes))
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise BadPart(f"the part is not a gzip tar: {exc}") from exc
    return Part(entries, contents)


def _member(
    tar: tarfile.TarFile, member: tarfile.TarInfo, named: set[str], max_file_bytes: int
) -> dict[str, bytes]:
    if not member.isreg():
        raise BadPart("every member of a part must be a regular file")
    if not _HEX64.fullmatch(member.name):
        raise BadPart("a member must be named by the SHA-256 of its bytes")
    if member.size > max_file_bytes:
        raise BadPart(f"a member is over the file limit of {max_file_bytes} bytes")
    if member.name not in named:
        raise BadPart("the part carries content that no entry names")
    stream = tar.extractfile(member)
    assert stream is not None
    content = stream.read()
    if hashlib.sha256(content).hexdigest() != member.name:
        raise BadPart("a member must be named by the SHA-256 of its bytes")
    return {member.name: content}
