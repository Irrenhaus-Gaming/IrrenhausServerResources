"""List placeholders: one placeholder line that expands into several TOML key/value entries.

Used for Velocity's [servers] table so servers can be added in GitHub settings without
changing the schema. The value is a JSON object, e.g. {"Lobby": "10.0.0.5:25565", "Adventure": "10.0.0.6:25565"};
the line holding only the placeholder becomes one `Name = "address"` line per entry, keeping
the placeholder line's indentation and the object's order.
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass

from .errors import McDeployError
from .formats import detect_format
from .placeholders import CONTROL_RE, TOKEN_RE

EXPANSIONS = {"toml-entries"}
ENTRY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
TABLE_RE = re.compile(r"^\s*\[(?P<array>\[)?\s*(?P<name>[A-Za-z0-9_.-]+)\s*\]")


class _Pairs(list):
    """A JSON object kept as its ordered key/value pairs (duplicates stay visible)."""


def parse_entries(name: str, raw: str) -> list[tuple[str, str]]:
    """Validate a list value: a non-empty JSON object of bare-key -> string."""
    try:
        data = json.loads(raw, object_pairs_hook=_Pairs)
    except json.JSONDecodeError as exc:
        raise McDeployError(f"{name}: value is not valid JSON ({exc.msg} at char {exc.pos})") from None
    if not isinstance(data, _Pairs) or not data:
        raise McDeployError(f'{name}: value must be a non-empty JSON object like {{"Lobby": "host:port"}}')
    seen: set[str] = set()
    for key, value in data:
        if not ENTRY_KEY_RE.match(key):
            raise McDeployError(f"{name}: entry name {key!r} may only contain letters, digits, '_' and '-'")
        if key in seen:
            raise McDeployError(f"{name}: entry {key!r} is listed twice")
        seen.add(key)
        if not isinstance(value, str) or not value or CONTROL_RE.search(value):
            raise McDeployError(f"{name}: entry {key!r} must be a non-empty single-line string")
    return data


@dataclass
class Expansion:
    table: tuple[str, ...]
    entries: list[tuple[str, str]]
    lines: list[str]  # the generated lines, without line endings


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def expand_blocks(path: str, text: str, values: dict[str, str], expansions: dict[str, str]) -> tuple[str, list[Expansion]]:
    """Replace every list placeholder line in `text`. `expansions` maps NAME -> expansion type."""
    if not expansions or not any(name in text for name in expansions):
        return text, []
    out: list[str] = []
    done: list[Expansion] = []
    table: tuple[str, ...] = ()
    in_array_table = False
    for lineno, line in enumerate(text.splitlines(keepends=True), 1):
        body = line.rstrip("\r\n")
        header = TABLE_RE.match(body)
        if header:
            table = tuple(header["name"].split("."))
            in_array_table = bool(header["array"])
        for m in TOKEN_RE.finditer(body):
            name = m.group(2)
            if name not in expansions:
                continue
            where = f"{path}:{lineno}"
            if detect_format(path) != "toml":
                raise McDeployError(f"{where}: list placeholder {name} is only supported in TOML files")
            if body.strip() != m.group(0):
                raise McDeployError(f"{where}: list placeholder {name} must stand alone on its line")
            if in_array_table:
                raise McDeployError(f"{where}: list placeholders are not supported inside [[array tables]]")
            if name not in values:
                raise McDeployError(f"{where}: no value for list placeholder {name}")
            entries = parse_entries(name, values[name])
            indent = body[: len(body) - len(body.lstrip())]
            ending = line[len(body):] or "\n"
            lines = [f"{indent}{key} = {_toml_string(value)}" for key, value in entries]
            out.extend(l + ending for l in lines)
            done.append(Expansion(table, entries, lines))
            break
        else:
            out.append(line)
    return "".join(out), done


def entries_from_server(path: str, server_text: str, repo_text: str, name: str) -> str | None:
    """Sync-back helper: rebuild a list placeholder's value from the server's own file.

    Takes the string entries of the table holding the placeholder, minus keys the repo file
    defines itself in that table. Returns None when the repo file has no such placeholder."""
    table: tuple[str, ...] | None = None
    current: tuple[str, ...] = ()
    repo_without: list[str] = []
    for line in repo_text.splitlines(keepends=True):
        header = TABLE_RE.match(line)
        if header:
            current = tuple(header["name"].split("."))
        if line.strip() == f"__SECRET_{name}__" or line.strip() == f"__VAR_{name}__":
            table = current
            continue
        repo_without.append(line)
    if table is None:
        return None
    try:
        server_doc = tomllib.loads(server_text)
        repo_doc = tomllib.loads("".join(repo_without))
    except tomllib.TOMLDecodeError as exc:
        raise McDeployError(f"{path}: TOML parse error while reading the server list: {exc}") from None
    server_table, repo_table = server_doc, repo_doc
    for part in table:
        server_table = server_table.get(part, {}) if isinstance(server_table, dict) else {}
        repo_table = repo_table.get(part, {}) if isinstance(repo_table, dict) else {}
    entries = {k: v for k, v in server_table.items() if isinstance(v, str) and k not in repo_table}
    if not entries:
        raise McDeployError(f"{path}: the server's [{'.'.join(table)}] table has no entries for {name}")
    return json.dumps(entries, separators=(",", ":"))


def verify_blocks(path: str, rendered: str, done: list[Expansion]) -> None:
    """The rendered TOML must contain every generated entry with its exact value."""
    if not done:
        return
    try:
        doc = tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as exc:
        raise McDeployError(f"{path}: TOML invalid after expanding list placeholders: {exc}") from None
    for exp in done:
        node = doc
        for part in exp.table:
            node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            raise McDeployError(f"{path}: table [{'.'.join(exp.table)}] missing after expansion")
        for key, value in exp.entries:
            if node.get(key) != value:
                raise McDeployError(f"{path}: entry {key} in [{'.'.join(exp.table)}] did not survive expansion")
