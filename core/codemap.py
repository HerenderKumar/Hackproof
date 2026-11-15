"""The code map (section 16, D-012, D-027): where in the code each claim lives, for the judge.

Graphify 0.9.22 (`graphify update <src> --no-cluster --force`, no model calls) writes
`<src>/graphify-out/graph.json`: top-level `nodes` and `links`. A node has `id`, `label`,
`file_type` (`code`, `document`, `concept`, `rationale`), `source_file` (relative; empty for
something outside the repository) and `source_location` (`"L<n>"`, the start line only). A link
has `source`, `target`, `relation` (`contains`, `imports`, `imports_from`, `calls`, `references`,
`inherits`, …) and `confidence`; `EXTRACTED` is an explicit edge, anything else inferred. A link
may name a node that is not listed (a library).

Graphify gives no end line, so a function's range is worked out here from the file: it starts at
the function's line, or at the decorators just above it, and ends where the indentation closes
(the closing bracket included), never past the next node. Graphify makes no node for an anonymous
handler (an Express arrow function), so a route served by one anchors nothing and the route
search takes over.

Anchors of a claim: functions whose range overlaps a validated citation, or holds a route string
from the claim's target. Neighbours: one hop along `calls` (both ways) from an anchor, and along
`imports` and `imports_from` from the anchor's file to another file of the archive; at most 8,
explicit first. When nothing anchors a claim, a plain-text search for its route strings in the
code files is the fallback (`code_map_status = 'fallback'`); with neither, `failed`. The judge's
brief renders in all three cases.

A UI claim names no route (M11 design, part 10). Its strings are the literal names its test
looks for (a label, a button's name, a placeholder, a test ID) and the page paths it opens other
than `/`; they are searched like route strings, so the lines where the page names its controls
anchor the claim.
"""

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from core.archive_tools import MAX_FILE_BYTES, ArchiveTools, is_binary, normalise
from core.snapshots import counts_toward_lines
from core.templating import PLACEHOLDER

GRAPHIFY_TIMEOUT_S = 120.0
MAX_NEIGHBOURS = 8
MAX_HITS = 20
HIT_CHARS = 200
OUTPUT_DIR = "graphify-out"
_LOCATION = re.compile(r"^L(\d+)$")
_NOT_CODE = {".md", ".markdown", ".txt", ".rst", ".json", ".csv", ".svg", ".lock"}
_IMPORTS = ("imports", "imports_from")
_CLOSERS = ")]}"

Status = Literal["ok", "fallback", "failed"]


class GraphError(Exception):
    """Graphify could not give a graph: missing, failed, too slow, or wrote something else."""


@dataclass(frozen=True)
class GraphNode:
    id: str
    label: str
    path: str
    line: int


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    relation: str
    explicit: bool


@dataclass(frozen=True)
class Graph:
    nodes: dict[str, GraphNode]
    edges: list[GraphEdge]


# --- reading the graph ---------------------------------------------------------------------------


def parse_graph(data: Any) -> Graph:
    """Graphify's `graph.json`: code nodes with a file and a line, and every well-formed link."""
    if not isinstance(data, dict):
        raise GraphError("graph.json is not a JSON object")
    raw_nodes, raw_links = data.get("nodes"), data.get("links")
    if not isinstance(raw_nodes, list) or not isinstance(raw_links, list):
        raise GraphError("graph.json has no nodes and links lists")
    nodes: dict[str, GraphNode] = {}
    for raw in raw_nodes:
        node = _node(raw)
        if node is not None:
            nodes[node.id] = node
    edges = [
        GraphEdge(
            raw["source"], raw["target"], raw["relation"], raw.get("confidence") == "EXTRACTED"
        )
        for raw in raw_links
        if isinstance(raw, dict)
        and all(isinstance(raw.get(key), str) for key in ("source", "target", "relation"))
    ]
    return Graph(nodes, edges)


def _node(raw: Any) -> GraphNode | None:
    if not isinstance(raw, dict) or raw.get("file_type") != "code":
        return None
    node_id, label = raw.get("id"), raw.get("label")
    source, location = raw.get("source_file"), raw.get("source_location")
    if not all(isinstance(value, str) for value in (node_id, label, source, location)):
        return None
    found = _LOCATION.match(str(location))
    path = normalise(str(source))
    if found is None or not path:
        return None
    return GraphNode(str(node_id), str(label), path, int(found.group(1)))


async def run_graphify(src: Path, *, timeout_s: float = GRAPHIFY_TIMEOUT_S) -> Graph:
    """Run Graphify over `src` and read its graph. Raises `GraphError`.

    A `graphify-out` folder the team committed is removed first, so its graph is never read. The
    process gets only `PATH`, a fresh `HOME` and `LANG`: no key or database URL reaches it.
    """
    exe = shutil.which("graphify")
    if exe is None:
        raise GraphError("graphify is not installed")
    out = src / OUTPUT_DIR
    if out.is_symlink() or out.is_file():
        out.unlink()
    elif out.exists():
        await asyncio.to_thread(shutil.rmtree, out)
    with tempfile.TemporaryDirectory(prefix="hp-graphify-") as home:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "LANG": "C.UTF-8"}
        process = await asyncio.create_subprocess_exec(
            exe, "update", str(src), "--no-cluster", "--force",
            cwd=home, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )  # fmt: skip
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout_s)
        except TimeoutError:
            _kill(process)
            await process.wait()
            raise GraphError(f"graphify took longer than {timeout_s:g} s") from None
    if process.returncode != 0:
        tail = output.decode("utf-8", errors="replace").strip()[-300:]
        raise GraphError(f"graphify exited with code {process.returncode}: {tail}")
    try:
        data = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GraphError(f"graphify wrote no readable graph.json ({type(exc).__name__})") from None
    return parse_graph(data)


def _kill(process: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


# --- ranges, anchors and neighbours --------------------------------------------------------------


class _Sources:
    """Each file's lines, read once; empty for a file that cannot be read as text."""

    def __init__(self, tools: ArchiveTools) -> None:
        self._tools = tools
        self._lines: dict[str, list[str]] = {}

    def lines(self, path: str) -> list[str]:
        if path not in self._lines:
            self._lines[path] = _read_lines(self._tools, path)
        return self._lines[path]


def _read_lines(tools: ArchiveTools, path: str) -> list[str]:
    target = tools.resolve(path)
    if target is None or not target.is_file():
        return []
    try:
        if target.stat().st_size > MAX_FILE_BYTES or is_binary(target):
            return []
        return target.read_bytes().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _is_file_node(node: GraphNode) -> bool:
    return node.line == 1 and node.label == PurePosixPath(node.path).name


def _is_function(node: GraphNode) -> bool:
    return node.label.endswith("()") and not _is_file_node(node)


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip())


def _start(lines: list[str], line: int) -> int:
    """The node's first line, moved up over decorators (`@…`) directly above it."""
    start = line
    while start > 1 and lines[start - 2].strip().startswith("@"):
        start -= 1
    return start


def _end(lines: list[str], line: int, cap: int) -> int:
    """The last line of the block that starts at `line`, at most `cap`."""
    indent = _indent(lines[line - 1])
    last = line
    for number in range(line + 1, cap + 1):
        text = lines[number - 1]
        if not text.strip():
            continue
        if _indent(text) <= indent:
            return number if text.lstrip()[0] in _CLOSERS else last
        last = number
    return last


def function_ranges(graph: Graph, tools: ArchiveTools) -> dict[str, tuple[str, int, int]]:
    """Each function node's `(path, first line, last line)`; nodes past their file are left out."""
    sources = _Sources(tools)
    by_file: dict[str, list[GraphNode]] = {}
    for node in graph.nodes.values():
        if not _is_file_node(node):
            by_file.setdefault(node.path, []).append(node)
    ranges: dict[str, tuple[str, int, int]] = {}
    for path, nodes in by_file.items():
        lines = sources.lines(path)
        placed = sorted(
            ((_start(lines, n.line), n) for n in nodes if n.line <= len(lines)),
            key=lambda item: (item[0], item[1].line, item[1].id),
        )
        for index, (start, node) in enumerate(placed):
            if not _is_function(node):
                continue
            later = [s for s, _ in placed[index + 1 :] if s > node.line]
            cap = (later[0] - 1) if later else len(lines)
            ranges[node.id] = (path, start, _end(lines, node.line, cap))
    return ranges


def _anchors(
    graph: Graph,
    ranges: dict[str, tuple[str, int, int]],
    sources: _Sources,
    routes: Sequence[str],
    citations: Sequence[dict[str, Any]],
) -> list[GraphNode]:
    """Functions a citation overlaps, then those whose lines hold a route string."""
    cited: list[tuple[str, int, GraphNode]] = []
    routed: list[tuple[str, int, GraphNode]] = []
    for node_id, (path, start, end) in ranges.items():
        node = graph.nodes[node_id]
        if any(
            c.get("path") == path and c["start_line"] <= end and c["end_line"] >= start
            for c in citations
        ):
            cited.append((path, start, node))
        elif any(
            route in line for line in sources.lines(path)[start - 1 : end] for route in routes
        ):
            routed.append((path, start, node))
    return [node for _, _, node in sorted(cited, key=_place) + sorted(routed, key=_place)]


def _place(item: tuple[str, int, GraphNode]) -> tuple[str, int, str]:
    return item[0], item[1], item[2].id


def _neighbours(graph: Graph, anchors: Sequence[GraphNode]) -> list[dict[str, Any]]:
    anchor_ids = {node.id for node in anchors}
    files = {
        node.id: node
        for node in graph.nodes.values()
        if _is_file_node(node) and node.path in {a.path for a in anchors}
    }
    found: dict[str, dict[str, Any]] = {}

    def add(node_id: str, edge: GraphEdge, direction: str) -> None:
        node = graph.nodes.get(node_id)
        if node is None or node_id in anchor_ids:
            return
        known = found.get(node_id)
        if known is not None and (known["explicit"] or not edge.explicit):
            return
        found[node_id] = {"path": node.path, "line": node.line, "label": node.label,
                          "relation": edge.relation, "direction": direction,
                          "explicit": edge.explicit}  # fmt: skip

    for edge in graph.edges:
        if edge.relation == "calls":
            if edge.source in anchor_ids:
                add(edge.target, edge, "out")
            if edge.target in anchor_ids:
                add(edge.source, edge, "in")
        elif edge.relation in _IMPORTS and edge.source in files:
            target = graph.nodes.get(edge.target)
            if target is not None and target.path != files[edge.source].path:
                add(edge.target, edge, "out")
    ordered = sorted(
        found.values(), key=lambda n: (not n["explicit"], n["path"], n["line"], n["label"])
    )
    return ordered[:MAX_NEIGHBOURS]


# --- the route-string fallback -------------------------------------------------------------------


def route_strings(target: str) -> list[str]:
    """The paths a claim's target names, cut before a query, a `{placeholder}` or a `:param`."""
    found: list[str] = []
    for word in re.split(r"[\s,;]+", target):
        if not word.startswith("/"):
            continue
        for stop in "?{:":
            word = word.split(stop, 1)[0]
        word = word.rstrip(".)")
        if len(word) > 1 and word not in found:
            found.append(word)
    return found


def _is_code(path: str) -> bool:
    if path.split("/", 1)[0] == OUTPUT_DIR or not counts_toward_lines(path):
        return False
    return PurePosixPath(path).suffix.lower() not in _NOT_CODE


def search_routes(tools: ArchiveTools, routes: Sequence[str]) -> list[dict[str, Any]]:
    """Lines of code files that hold one of the route strings, at most 20."""
    if not routes:
        return []
    hits: list[dict[str, Any]] = []
    for path, _ in tools.files():
        if not _is_code(path):
            continue
        for number, line in enumerate(_read_lines(tools, path), start=1):
            if any(route in line for route in routes):
                hits.append({"path": path, "line": number, "text": line.strip()[:HIT_CHARS]})
                if len(hits) == MAX_HITS:
                    return hits
    return hits


# --- the map -------------------------------------------------------------------------------------


_UI_NAMES = ("name", "label", "placeholder", "text", "test_id")
_MIN_UI_STRING = 3


def ui_strings(spec: Mapping[str, Any] | None) -> list[str]:
    """The literal strings a UI test looks for on the page, each once, placeholders left out."""
    found: list[str] = []
    for step in (spec or {}).get("steps", []):
        if not isinstance(step, dict):
            continue
        locator = step.get("locator") or {}
        values = [locator.get(name) for name in _UI_NAMES if isinstance(locator, dict)]
        if step.get("action") in ("goto", "expect_url") and step.get("path") != "/":
            values.append(step.get("path"))
        for value in values:
            if (
                isinstance(value, str)
                and len(value.strip()) >= _MIN_UI_STRING
                and not PLACEHOLDER.search(value)
                and value not in found
            ):
                found.append(value)
    return found


def claim_map(
    graph: Graph | None,
    tools: ArchiveTools,
    target: str,
    citations: Sequence[dict[str, Any]],
    *,
    ranges: dict[str, tuple[str, int, int]] | None = None,
    extra: Sequence[str] = (),
) -> dict[str, Any]:
    """One claim's anchors and neighbours, or its route-search hits. `ranges` may be shared.
    `extra` adds strings to search for beside the target's routes: a UI test's names."""
    routes = route_strings(target)
    routes += [text for text in extra if text not in routes]
    if graph is not None:
        spans = ranges if ranges is not None else function_ranges(graph, tools)
        anchors = _anchors(graph, spans, _Sources(tools), routes, citations)
        if anchors:
            return {
                "method": "graph",
                "routes": routes,
                "anchors": [
                    {"path": a.path, "line": a.line, "end_line": spans[a.id][2], "label": a.label}
                    for a in anchors
                ],
                "neighbours": _neighbours(graph, anchors),
                "hits": [],
            }
    hits = search_routes(tools, routes)
    return {
        "method": "search" if hits else "none",
        "routes": routes,
        "anchors": [],
        "neighbours": [],
        "hits": hits,
    }


def code_map(
    graph: Graph | None,
    graph_error: str | None,
    tools: ArchiveTools,
    claims: Sequence[dict[str, str]],
    audit_content: dict[str, Any],
    ui_specs: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], Status]:
    """The code map of every claim, and `code_map_status`. `ui_specs` holds the UI test of
    each UI claim, by claim ID."""
    cited = {
        entry["claim_id"]: entry.get("citations", []) for entry in audit_content.get("claims", [])
    }
    ranges = function_ranges(graph, tools) if graph is not None else None
    per_claim = {
        claim["id"]: claim_map(
            graph,
            tools,
            claim.get("target", ""),
            cited.get(claim["id"], []),
            ranges=ranges,
            extra=ui_strings((ui_specs or {}).get(claim["id"])),
        )
        for claim in claims
    }
    methods = [mapped["method"] for mapped in per_claim.values()]
    status: Status
    if methods and all(method == "graph" for method in methods):
        status = "ok"
    elif any(method in ("graph", "search") for method in methods):
        status = "fallback"
    else:
        status = "failed"
    summary = {
        "ok": graph is not None,
        "detail": graph_error,
        "nodes": len(graph.nodes) if graph is not None else 0,
        "edges": len(graph.edges) if graph is not None else 0,
    }
    return {"graphify": summary, "claims": per_claim}, status
