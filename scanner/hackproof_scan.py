"""HackProof kickoff scan: records the source code already on this device, before the event.

Run it before kickoff, with the device code your team page gave you:

    python3 hackproof_scan.py --server https://<event address> --code <device code>

What it does, in order:
  1. Asks the server what to scan for (the event's file extensions and limits) and shows you.
  2. Walks this device's own disks. It never follows a link, and it skips dependency, build,
     version-control and system folders. Only files whose extension is on the event's list
     are opened; nothing else is read.
  3. Blanks anything that looks like a key or a password in those files, before anything is
     sent. The file's hash from before the blanking is kept, so it can still be recognised.
  4. Shows you the summary and the list of files, and asks again. Only then does it upload,
     in parts, and seal the scan. The server encrypts what it receives and deletes it after
     the event.

This file is the whole scanner. It needs Python 3.9 or later and nothing outside the
standard library, so you can read every line of it before you run it.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import os
import re
import stat
import sys
import tarfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache

SCANNER_VERSION = "1"
KEY_MARKER = "HACKPROOF_BLANKED"
PART_RAW_BYTES = 3_500_000  # a part's unpacked size stays under this, so it is under 4 MiB sent
NUL_PROBE_BYTES = 8192
MAX_PATH_CHARS = 4096
MANIFEST = "manifest.json"

# --- what is skipped ----------------------------------------------------------------------

DEPENDENCY_FOLDERS = frozenset(
    {
        "node_modules", "site-packages", "dist-packages", "vendor", "bower_components",
        "__pycache__", "Pods", ".m2", ".gradle", ".cargo", ".rustup", ".npm", ".yarn",
        ".pnpm-store", ".nvm", ".pyenv", ".cache",
    }
)  # fmt: skip
BUILD_FOLDERS = frozenset(
    {"dist", "build", "target", ".next", ".nuxt", ".output", ".turbo", ".parcel-cache", "out",
     "coverage"}
)  # fmt: skip
VCS_FOLDERS = frozenset({".git", ".hg", ".svn"})
MAC_SYSTEM = frozenset(
    {"/System", "/Library", "/Applications", "/Volumes", "/private", "/usr", "/bin", "/sbin",
     "/opt", "/cores", "/dev"}
)  # fmt: skip
LINUX_SYSTEM = frozenset(
    {"/proc", "/sys", "/dev", "/run", "/tmp", "/boot", "/usr", "/bin", "/sbin", "/lib", "/lib64",
     "/snap", "/var/lib", "/var/cache"}
)  # fmt: skip
LINUX_NOT_DISKS = frozenset(
    {"nfs", "nfs4", "cifs", "smbfs", "fuse.sshfs", "9p", "tmpfs", "overlay", "squashfs",
     "autofs", "proc", "sysfs", "devtmpfs"}
)  # fmt: skip
WINDOWS_ROOT_SYSTEM = frozenset(
    {"windows", "program files", "program files (x86)", "programdata", "$recycle.bin",
     "system volume information"}
)  # fmt: skip
SKIP_KINDS = (
    "dependency", "build", "vcs", "system", "too_large", "not_text", "cloud_placeholder", "link",
)  # fmt: skip

MAC_DATALESS = 0x40000000  # st_flags: an iCloud file whose bytes are not on this disk
WINDOWS_RECALL_ON_DATA_ACCESS = 0x400000  # st_file_attributes: a OneDrive placeholder


@dataclass(frozen=True)
class Limits:
    extensions: frozenset[str]  # lower-case, with the dot
    max_file_bytes: int
    part_raw_bytes: int
    max_bytes: int


@dataclass
class ScanReport:
    roots: list[str] = field(default_factory=list)
    files_found: int = 0
    bytes_found: int = 0
    skipped: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SKIP_KINDS, 0))
    keys_blanked: int = 0
    unreadable: list[dict[str, str]] = field(default_factory=list)
    size_limit_reached: bool = False
    limited_to: list[str] | None = None

    def cannot_read(self, path: str, kind: str, error: OSError) -> None:
        reason = error.strerror or type(error).__name__
        self.unreadable.append({"path": shown(path), "kind": kind, "reason": reason})


@dataclass(frozen=True)
class Found:
    path: str  # as shown and sent: a name that is not UTF-8 is backslash-escaped
    size: int  # of the bytes sent
    sha256_device: str  # of the bytes on this device
    sha256_uploaded: str  # of the bytes sent, keys blanked
    data: bytes
    keys_blanked: int


def shown(path: str) -> str:
    """A path as text, whatever bytes its name holds."""
    return os.fsencode(path).decode("utf-8", errors="backslashreplace")


# --- where to look ------------------------------------------------------------------------


def fixed_roots(platform: str = sys.platform) -> list[str]:
    """The device's own disks: `/` on macOS and Linux, each fixed drive on Windows."""
    if platform != "win32":
        return ["/"]
    import ctypes  # Windows only

    kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
    mask = kernel.GetLogicalDrives()
    roots = []
    for n in range(26):
        if mask & (1 << n):
            root = f"{chr(65 + n)}:\\"
            if kernel.GetDriveTypeW(ctypes.c_wchar_p(root)) == 3:  # DRIVE_FIXED
                roots.append(root)
    return roots


@lru_cache(maxsize=1)
def _linux_other_mounts() -> frozenset[str]:
    """Mount points that are not disks of this device: network shares, memory, containers."""
    try:
        lines = open_file("/proc/mounts").decode("utf-8", errors="replace").splitlines()
    except OSError:
        return frozenset()
    points: set[str] = set()
    for line in lines:
        fields = line.split()
        if len(fields) >= 3 and fields[2] in LINUX_NOT_DISKS:
            points.add(fields[1].replace("\\040", " "))
    return frozenset(points)


def _home_child(path: str, homes: str) -> bool:
    parent = os.path.dirname(path)
    return os.path.dirname(parent) == homes or parent == os.path.expanduser("~")


def skip_reason(path: str, name: str) -> str | None:
    """Why a folder is not walked, or None to walk it."""
    if name in VCS_FOLDERS:
        return "vcs"
    if name in DEPENDENCY_FOLDERS or os.path.isfile(os.path.join(path, "pyvenv.cfg")):
        return "dependency"
    if name in BUILD_FOLDERS:
        return "build"
    if sys.platform == "darwin":
        library = name == "Library" and _home_child(path, "/Users")
        return "system" if path in MAC_SYSTEM or library else None
    if sys.platform == "win32":
        parent = os.path.dirname(path)
        at_drive_root = os.path.dirname(parent) == parent
        if at_drive_root and name.lower() in WINDOWS_ROOT_SYSTEM:
            return "system"
        if name == "AppData" and os.path.basename(os.path.dirname(parent)).lower() == "users":
            return "system"
        return None
    if path in LINUX_SYSTEM or path in _linux_other_mounts():
        return "system"
    return None


def listed(name: str, limits: Limits) -> bool:
    return os.path.splitext(name)[1].lower() in limits.extensions


def walk(roots: list[str], limits: Limits, report: ScanReport) -> Iterator[str]:
    """Every file under `roots` whose extension is on the list, without following a link."""
    stack = list(reversed(roots))
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as listing:
                entries = sorted(listing, key=lambda e: e.name)
        except OSError as error:
            report.cannot_read(folder, "folder", error)
            continue
        folders = []
        for entry in entries:
            try:
                if entry.is_symlink():
                    report.skipped["link"] += 1
                elif entry.is_dir(follow_symlinks=False):
                    reason = skip_reason(entry.path, entry.name)
                    if reason:
                        report.skipped[reason] += 1
                    else:
                        folders.append(entry.path)
                elif entry.is_file(follow_symlinks=False) and listed(entry.name, limits):
                    yield entry.path
            except OSError as error:
                report.cannot_read(entry.path, "file", error)
        stack.extend(reversed(folders))


# --- reading a file -----------------------------------------------------------------------


def stat_of(path: str) -> os.stat_result:
    return os.lstat(path)


def is_placeholder(info: object) -> bool:
    """A cloud file whose bytes are elsewhere: opening it would download it."""
    flags = getattr(info, "st_flags", 0) or 0
    attributes = getattr(info, "st_file_attributes", 0) or 0
    return bool(flags & MAC_DATALESS or attributes & WINDOWS_RECALL_ON_DATA_ACCESS)


def open_file(path: str) -> bytes:
    """The only place the scanner reads a file's bytes."""
    with open(path, "rb") as handle:
        return handle.read()


def take(path: str, limits: Limits, report: ScanReport) -> Found | None:
    """The file as it will be sent, or None when it is skipped or cannot be read."""
    try:
        info = stat_of(path)
    except OSError as error:
        report.cannot_read(path, "file", error)
        return None
    if is_placeholder(info):
        report.skipped["cloud_placeholder"] += 1
        return None
    if info.st_size > limits.max_file_bytes:
        report.skipped["too_large"] += 1
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    name = shown(path)
    if len(name) > MAX_PATH_CHARS:
        report.unreadable.append(
            {"path": name[:200] + "...", "kind": "file", "reason": "path too long"}
        )
        return None
    try:
        data = open_file(path)
    except OSError as error:
        report.cannot_read(path, "file", error)
        return None
    if len(data) > limits.max_file_bytes:
        report.skipped["too_large"] += 1
        return None
    if b"\0" in data[:NUL_PROBE_BYTES]:
        report.skipped["not_text"] += 1
        return None
    try:
        text, codec = data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        text, codec = data.decode("latin-1"), "latin-1"
    blanked, count = blank_keys(text)
    sent = blanked.encode(codec) if count else data
    report.files_found += 1
    report.bytes_found += len(data)
    report.keys_blanked += count
    return Found(
        path=name,
        size=len(sent),
        sha256_device=hashlib.sha256(data).hexdigest(),
        sha256_uploaded=hashlib.sha256(sent).hexdigest(),
        data=sent,
        keys_blanked=count,
    )


# --- keys ---------------------------------------------------------------------------------

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.DOTALL
)
_SHAPES = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}"
    r"|gh[pousr]_[A-Za-z0-9]{36,}"
    r"|github_pat_[A-Za-z0-9_]{60,}"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|(?:sk|rk)_live_[A-Za-z0-9]{16,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AIza[A-Za-z0-9_-]{35}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"
    r")"
)
_ASSIGNED = re.compile(
    r"(?i)([A-Za-z0-9_.-]*(?:key|secret|token|password|passwd|pwd|credential)[A-Za-z0-9_.-]*"
    r"['\"]?\s*[:=]\s*)(['\"`])([^'\"`\s]{8,})\2"
)
_LONG_STRING = re.compile(r"(['\"`])([A-Za-z0-9+/=_-]{32,})\1")


def _entropy(text: str) -> float:
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def blank_keys(text: str) -> tuple[str, int]:
    """Replace what looks like a key with KEY_MARKER, keeping every newline and the quotes."""
    count = 0

    def block(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return KEY_MARKER + "\n" * match.group(0).count("\n")

    def shape(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return KEY_MARKER

    def assigned(match: re.Match[str]) -> str:
        nonlocal count
        if match.group(3) == KEY_MARKER:
            return match.group(0)
        count += 1
        return match.group(1) + match.group(2) + KEY_MARKER + match.group(2)

    def long_string(match: re.Match[str]) -> str:
        nonlocal count
        if _entropy(match.group(2)) < 4.0:
            return match.group(0)
        count += 1
        return match.group(1) + KEY_MARKER + match.group(1)

    text = _PRIVATE_KEY.sub(block, text)
    text = _SHAPES.sub(shape, text)
    text = _ASSIGNED.sub(assigned, text)
    text = _LONG_STRING.sub(long_string, text)
    return text, count


# --- parts --------------------------------------------------------------------------------

_MEMBER_OVERHEAD = 1024  # a tar header and the padding after the bytes


def _part(entries: list[dict[str, object]], members: list[tuple[str, bytes]]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in [(MANIFEST, json.dumps(entries).encode("utf-8")), *members]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


def pack_parts(found: Iterable[Found], limits: Limits, report: ScanReport) -> Iterator[bytes]:
    """Gzip tars of A4's shape, one per `next`. Each content goes once, in the first part
    whose files hold it; later files with the same content get an entry only."""
    sent_contents: set[str] = set()
    sent_bytes = 0
    entries: list[dict[str, object]] = []
    members: list[tuple[str, bytes]] = []
    raw = 0
    for item in found:
        new = item.sha256_uploaded not in sent_contents
        entry: dict[str, object] = {
            "path": item.path,
            "size": item.size,
            "sha256_device": item.sha256_device,
            "sha256_uploaded": item.sha256_uploaded,
            "keys_blanked": item.keys_blanked,
        }
        grows = len(json.dumps(entry)) + 2 + (_MEMBER_OVERHEAD + item.size if new else 0)
        if new and sent_bytes + item.size > limits.max_bytes:
            report.size_limit_reached = True
            break
        if entries and raw + grows + _MEMBER_OVERHEAD > limits.part_raw_bytes:
            yield _part(entries, members)
            entries, members, raw = [], [], 0
        entries.append(entry)
        raw += grows
        if new:
            members.append((item.sha256_uploaded, item.data))
            sent_contents.add(item.sha256_uploaded)
            sent_bytes += item.size
    if entries:
        yield _part(entries, members)
