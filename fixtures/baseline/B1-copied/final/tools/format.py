"""Print the notes a running server holds, newest first."""

import json
import sys
from urllib.request import urlopen


def fetch(base: str) -> list[dict]:
    with urlopen(f"{base}/notes") as response:
        return json.load(response)["notes"]


def line(note: dict) -> str:
    tags = ", ".join(sorted(note["tags"])) or "no tags"
    title = note["title"] if len(note["title"]) <= 40 else note["title"][:37] + "..."
    return f"{note['id']:>4}  {title:<40}  {tags}"


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3000"
    for note in fetch(base):
        print(line(note))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
