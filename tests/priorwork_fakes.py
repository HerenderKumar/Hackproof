"""A `Baseline` over a dictionary of path to text, for the comparison's tests."""

import hashlib
import random
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


def block(seed: int, lines: int = 12) -> str:
    """A function of distinct-looking JavaScript, about 20 tokens a line.

    Names, strings and numbers all become one symbol, so only operators and keywords tell two
    blocks apart. Each line opens with four random operators and its statement is at most 14
    symbols, so any 25 symbols in a row cross some line's operators: two unrelated blocks share
    such a stretch about once in a thousand pairs, and the tests' seeds share none.
    """
    rng = random.Random(seed)
    names = ["total", "count", "price", "items", "share", "rest", "sum", "value", "index"]
    ops = ["+", "-", "*", "/", "%", "&", "|", "^"]
    body = []
    for n in range(lines):
        a, b = rng.choice(names), rng.choice(names)
        o1, o2, o3, o4, op = (rng.choice(ops) for _ in range(5))
        cmp = rng.choice(["<", ">", "==", "!="])
        mix = f"mix {o1}= {a} {o2} {b} {o3} {n} {o4} {a};"
        shape = rng.randrange(6)
        if shape == 0:
            statement = f"if ({a} {cmp} {n}) {{ {b}--; }}"
        elif shape == 1:
            statement = f"{a}{n} = ({b} {op} {rng.randint(1, 9)}) * {a};"
        elif shape == 2:
            statement = f"for (const x of {a}) {b}.push(x);"
        elif shape == 3:
            statement = f"return {a}.{b}[{n}];"
        elif shape == 4:
            statement = f"while ({a} {cmp} {b}) {a} = {b} {op} {n};"
        else:
            statement = f'console.log("{a}", {b});'
        body.append(f"  {mix} {statement}")
    return f"function f{seed}(" + ", ".join(names[:3]) + ") {\n" + "\n".join(body) + "\n}\n"
