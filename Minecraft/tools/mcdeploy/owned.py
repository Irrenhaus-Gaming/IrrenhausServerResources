"""Server-owned keys: top-level YAML keys whose value belongs to the server once a file exists there
(targets.yml `server_owned`), e.g. a hologram's position set in game with /dh movehere.

A key's block is its `key:` line at column 0 plus the indented (or `- `) lines below it. A key that
does not occur exactly once in a text is left alone, so an unusual file degrades to normal handling.
"""
from __future__ import annotations

import re
from fnmatch import fnmatchcase

from .config import Target


def owned_keys(target: Target, rel: str) -> tuple[str, ...]:
    keys: list[str] = []
    for glob, globbed in target.server_owned:
        if fnmatchcase(rel, glob):
            keys += [k for k in globbed if k not in keys]
    return tuple(keys)


def _is_continuation(line: str) -> bool:
    return line.startswith((" ", "\t", "- ")) or line.rstrip("\r\n") == "-"


def _blocks(lines: list[str], keys: tuple[str, ...]) -> dict[str, tuple[int, int]]:
    """key -> (first line, end line) for every key that occurs exactly once."""
    found: dict[str, list[tuple[int, int]]] = {}
    for i, line in enumerate(lines):
        for key in keys:
            if re.match(rf"{re.escape(key)}:(?:[ \t]|\r?\n|$)", line):
                end = i + 1
                while end < len(lines) and _is_continuation(lines[end]):
                    end += 1
                found.setdefault(key, []).append((i, end))
    return {k: spans[0] for k, spans in found.items() if len(spans) == 1}


def _decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def strip_owned(data: bytes, keys: tuple[str, ...]) -> bytes:
    """The file without its server-owned blocks (what drift checks and the manifest compare)."""
    text = _decode(data) if keys else None
    if text is None:
        return data
    lines = text.splitlines(keepends=True)
    for start, end in sorted(_blocks(lines, keys).values(), reverse=True):
        del lines[start:end]
    return "".join(lines).encode("utf-8")


def with_values_from(data: bytes, donor: bytes, keys: tuple[str, ...]) -> bytes:
    """`data` with the server-owned blocks of `donor` wherever both have the key."""
    text, donor_text = (_decode(data), _decode(donor)) if keys else (None, None)
    if text is None or donor_text is None:
        return data
    lines = text.splitlines(keepends=True)
    donor_lines = donor_text.splitlines(keepends=True)
    theirs = _blocks(donor_lines, keys)
    ours = _blocks(lines, keys)
    for key, (start, end) in sorted(ours.items(), key=lambda item: item[1], reverse=True):
        if key in theirs:
            d_start, d_end = theirs[key]
            block = donor_lines[d_start:d_end]
            if block and end < len(lines) and not block[-1].endswith("\n"):
                block[-1] += "\n"  # donor's block was its last line without a line break
            lines[start:end] = block
    return "".join(lines).encode("utf-8")
