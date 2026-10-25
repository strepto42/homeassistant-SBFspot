#!/usr/bin/env python3
"""Generate ``smabt/tag_names.py`` from SBFspot's ``TagListEN-US.txt``.

SBFspot (https://github.com/SBFspot/SBFspot, CC BY-NC-SA 3.0) ships tag lists
whose lines look like ``307=Ok\\0\\Ok``: ``<tag id>=<short>\\<lri>\\<long text>``.
This script mirrors ``TagDefs::readall()``:

* everything from the first ``#`` or CR on a line is discarded (comments, CRLF),
* the text after the first ``=`` must split into exactly three ``\\`` separated
  items, otherwise the line is skipped,
* tag id and LRI must be unsigned integers,
* short name and long text are stripped.

Only the long text is kept.  Usage::

    python scripts/gen_tag_names.py path/to/TagListEN-US.txt [output.py]

Run ``ruff format`` on the output afterwards if you change the layout below.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1] / "custom_components/sbfspot/smabt/tag_names.py"
)


def parse_tag_list(text: str) -> tuple[dict[int, str], list[str]]:
    """Return ``({tag id: long text}, [warnings])`` for a SBFspot tag list."""
    names: dict[int, str] = {}
    warnings: list[str] = []
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw
        for marker in ("#", "\r"):
            cut = line.find(marker)
            if cut != -1:
                line = line[:cut]
        if not line:
            continue
        tag_id, sep, rest = line.partition("=")
        items = rest.split("\\")
        if not sep or len(items) != 3:
            warnings.append(f"line {number}: wrong number of items")
            continue
        try:
            tag = int(tag_id)
            int(items[1])  # LRI, validated like SBFspot but not needed here
        except ValueError:
            warnings.append(f"line {number}: invalid tag id or LRI")
            continue
        # SBFspot's std::map::insert keeps the first definition of an id.
        names.setdefault(tag, items[2].strip())
    return names, warnings


def py_literal(text: str) -> str:
    """Python string literal for *text*, quoted the way ``ruff format`` prefers."""
    quote = "'" if text.count('"') > text.count("'") else '"'
    out = []
    for char in text:
        if char in (quote, "\\"):
            out.append("\\" + char)
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\x{ord(char):02x}")
        else:
            out.append(char)
    return quote + "".join(out) + quote


def render(names: dict[int, str], source: str) -> str:
    """Render the module source."""
    lines = [
        '"""Tag id -> English description, generated from SBFspot\'s tag list.',
        "",
        "GENERATED FILE - do not edit.  Regenerate with::",
        "",
        f"    python scripts/gen_tag_names.py {source}",
        "",
        "Source: SBFspot (https://github.com/SBFspot/SBFspot, CC BY-NC-SA 3.0),",
        f"``{source}`` ({len(names)} tags).",
        '"""',
        "",
        "# ruff: noqa: E501, RUF001",
        "",
        "from __future__ import annotations",
        "",
        "TAG_NAMES: dict[int, str] = {",
    ]
    lines += [f"    {tag}: {py_literal(text)}," for tag, text in sorted(names.items())]
    lines += ["}", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("taglist", type=Path, help="path to TagListEN-US.txt")
    parser.add_argument("output", type=Path, nargs="?", default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    names, warnings = parse_tag_list(args.taglist.read_text(encoding="utf-8"))
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    args.output.write_text(
        render(names, args.taglist.name), encoding="utf-8", newline="\n"
    )
    print(f"wrote {len(names)} tags to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
