"""The audit's tools over an extracted archive (section 15.2): inside it, and nowhere else."""

import os
import time
from pathlib import Path

import pytest

from core.archive_tools import MAX_CHARS, MAX_HITS, MAX_LINES, ArchiveTools


@pytest.fixture
def root(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "lib").mkdir(parents=True)
    (src / "node_modules" / "left").mkdir(parents=True)
    (src / "server.js").write_text(
        'const app = require("express")();\napp.post("/split", handler);\nmodule.exports = app;\n'
    )
    (src / "lib" / "split.js").write_text("function split(total) {\n  return [total];\n}\n")
    (src / "node_modules" / "left" / "index.js").write_text('app.post("/split")\n')
    (src / "README.md").write_text("# Splitter\nPOST /split splits a bill.\n")
    (tmp_path / "secret.txt").write_text("outside the archive\n")
    return src


def test_list_dir_lists_sorted_entries_and_marks_skipped_folders(root: Path) -> None:
    listing = ArchiveTools(root).list_dir("")

    lines = listing.splitlines()
    assert lines == [
        f"README.md ({(root / 'README.md').stat().st_size} bytes)",
        "lib/",
        "node_modules/ (skipped: dependencies or build output)",
        f"server.js ({(root / 'server.js').stat().st_size} bytes)",
    ]
    assert ArchiveTools(root).list_dir("lib") == "split.js (44 bytes)"


def test_list_dir_of_a_file_or_a_missing_folder_is_an_error(root: Path) -> None:
    assert ArchiveTools(root).list_dir("server.js").startswith("error:")
    assert ArchiveTools(root).list_dir("nope").startswith("error:")


def test_read_file_numbers_lines_and_wraps_them_in_a_file_tag(root: Path) -> None:
    text = ArchiveTools(root).read_file("lib/split.js", 2, 3)

    assert text == (
        '<file path="lib/split.js" lines="2-3" of="3">\n   2 |   return [total];\n   3 | }\n</file>'
    )


def test_read_file_clamps_the_range_to_the_file(root: Path) -> None:
    text = ArchiveTools(root).read_file("lib/split.js", 0, 99)

    assert text.startswith('<file path="lib/split.js" lines="1-3" of="3">')


def test_read_file_caps_lines_and_characters_and_refuses_binary(root: Path) -> None:
    (root / "long.py").write_text("".join(f"x = {n}\n" for n in range(1000)))
    (root / "wide.py").write_text("".join("y" * 1000 + "\n" for _ in range(100)))
    (root / "logo.png").write_bytes(b"\x89PNG\x00\x00binary")
    tools = ArchiveTools(root)

    long = tools.read_file("long.py", 1, 1000)
    assert f'lines="1-{MAX_LINES}"' in long
    assert "stopped after 400 lines" in long
    wide = tools.read_file("wide.py", 1, 100)
    assert len(wide) < MAX_CHARS + 500
    assert "stopped at 40000 characters" in wide
    assert tools.read_file("logo.png", 1, 5) == "error: logo.png is a binary file"


def test_no_path_leaves_the_archive(root: Path) -> None:
    os.symlink(root.parent / "secret.txt", root / "link.txt")
    os.symlink("/etc", root / "etc")
    tools = ArchiveTools(root)

    for path in ("/etc/passwd", "../secret.txt", "lib/../../secret.txt", "link.txt", "etc/hosts"):
        assert tools.resolve(path) is None, path
        assert tools.read_file(path, 1, 5).startswith("error:"), path
        assert "outside the archive" not in tools.read_file(path, 1, 5)
    assert tools.list_dir("../").startswith("error:")
    assert tools.line_count("../secret.txt") is None
    assert tools.resolve("./lib//split.js") == (root / "lib" / "split.js").resolve()


def test_grep_is_plain_text_and_case_insensitive(root: Path) -> None:
    hits = ArchiveTools(root).grep('APP.POST("/split"')

    assert hits == 'server.js:2: app.post("/split", handler);'
    assert ArchiveTools(root).grep("split(.*)") == "no matches"


def test_grep_honours_the_glob_and_skips_dependencies(root: Path) -> None:
    tools = ArchiveTools(root)

    assert (
        tools.grep("split", "*.md")
        == "README.md:1: # Splitter\nREADME.md:2: POST /split splits a bill."
    )
    assert "node_modules" not in tools.grep("/split")
    assert tools.grep("split", "lib/*") == "lib/split.js:1: function split(total) {"


def test_grep_is_plain_text_and_a_pathological_pattern_returns_quickly(root: Path) -> None:
    (root / "a.txt").write_text("a" * 50_000 + "\n")

    started = time.monotonic()
    result = ArchiveTools(root).grep("(a+)+$")

    assert time.monotonic() - started < 1.0
    assert result == "no matches"


def test_grep_caps_its_hits_and_cuts_long_lines(root: Path) -> None:
    (root / "many.py").write_text("".join(f"needle {n} " + "z" * 300 + "\n" for n in range(80)))

    result = ArchiveTools(root).grep("needle")

    lines = result.splitlines()
    assert len(lines) == MAX_HITS + 1
    assert lines[-1] == "… more hits not shown"
    assert all(len(line) <= 220 for line in lines)


def test_an_empty_or_overlong_pattern_is_an_error(root: Path) -> None:
    assert ArchiveTools(root).grep("").startswith("error:")
    assert ArchiveTools(root).grep("x" * 201).startswith("error:")


def test_a_path_with_angle_brackets_cannot_close_the_file_tag(root: Path) -> None:
    (root / 'a"><b>.js').write_text("x\n")

    text = ArchiveTools(root).read_file('a"><b>.js', 1, 1)

    assert text.splitlines()[0] == '<file path="a&quot;&gt;&lt;b&gt;.js" lines="1-1" of="1">'


def test_line_count_is_none_for_missing_and_binary_files(root: Path) -> None:
    (root / "b.bin").write_bytes(b"\x00\x01")
    tools = ArchiveTools(root)

    assert tools.line_count("lib/split.js") == 3
    assert tools.line_count("missing.js") is None
    assert tools.line_count("b.bin") is None
    assert tools.line_count("lib") is None
