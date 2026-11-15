"""A `Baseline` over a dictionary of path to text, for the comparison's tests."""

import hashlib
from collections.abc import Collection, Mapping
from pathlib import PurePosixPath

from core.priorwork.fingerprint import run_hashes, winnow
from core.priorwork.lexer import tokenize
from core.priorwork.model import MatchSettings, OldRef, SourceFile

DEFAULT_SETTINGS = MatchSettings()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class MemoryBaseline:
    def __init__(
        self,
        id: str,
        files: dict[str, str],
        *,
        device_label: str = "laptop",
        late: bool = False,
        settings: MatchSettings = DEFAULT_SETTINGS,
    ) -> None:
        self.id, self.device_label, self.late = id, device_label, late
        self._paths: dict[str, list[str]] = {}
        self._texts: dict[str, str] = {}
        for path in sorted(files):
            content = _sha(files[path])
            self._paths.setdefault(content, []).append(path)
            self._texts[content] = files[path]
        self._prints: dict[str, set[int]] = {}
        for content, text in self._texts.items():
            ext = PurePosixPath(self._paths[content][0]).suffix
            syms = [t.sym for t in tokenize(text, ext)]
            self._prints[content] = {h for _, h in winnow(run_hashes(syms, settings.k), settings.w)}

    def _ref(self, content: str) -> OldRef:
        paths = self._paths[content]
        return OldRef(self.id, content, paths[0], len(paths) - 1)

    def with_hash(self, sha256_device: str) -> list[OldRef]:
        return [self._ref(sha256_device)] if sha256_device in self._texts else []

    def candidates(self, hashes: Collection[int], limit: int) -> list[tuple[OldRef, int]]:
        wanted = set(hashes)
        found = [
            (self._ref(content), len(prints & wanted))
            for content, prints in self._prints.items()
            if prints & wanted
        ]
        found.sort(key=lambda pair: (-pair[1], pair[0].path))
        return found[:limit]

    def text(self, ref: OldRef) -> str:
        return self._texts[ref.content]


def source(path: str, text: str) -> SourceFile:
    return SourceFile(path, _sha(text), text)


def nobody(hashes: Collection[int]) -> Mapping[int, int]:
    return {}
