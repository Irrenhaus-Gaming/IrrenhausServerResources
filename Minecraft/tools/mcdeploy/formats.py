"""Format detection and parsing of server config files.

Parsing is used twice: lint checks that every masked file is syntactically valid, and render
verifies that each substituted value ends up in the parsed document exactly as intended.
"""
from __future__ import annotations

import io
import json
import re
import tomllib
from pathlib import PurePosixPath

from pyhocon import ConfigFactory
from pyhocon.config_tree import ConfigTree
from ruamel.yaml import YAML

from .errors import McDeployError

EXTENSIONS = {
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".conf": "hocon",
    ".properties": "properties",
}


def detect_format(path: str) -> str:
    return EXTENSIONS.get(PurePosixPath(path).suffix.lower(), "text")


def _yaml() -> YAML:
    # Bukkit/Paper read YAML with SnakeYAML (YAML 1.1), so parse with 1.1 rules: "yes"/"on" are
    # booleans there and must not slip through verification as strings.
    yaml = YAML(typ="safe", pure=True)
    yaml.version = (1, 1)
    yaml.allow_duplicate_keys = True
    return yaml


_PROP_ESCAPES = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}


def parse_properties(text: str) -> dict[str, str]:
    """Java .properties parser (logical lines, continuations, escapes, = : or space separator)."""
    result: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].lstrip(" \t\f")
        i += 1
        if not line or line[0] in "#!":
            continue
        while _ends_with_continuation(line) and i < len(lines):
            line = line[:-1] + lines[i].lstrip(" \t\f")
            i += 1
        key_chars: list[str] = []
        j = 0
        while j < len(line):
            c = line[j]
            if c == "\\" and j + 1 < len(line):
                key_chars.append(_unescape_char(line, j))
                j += 6 if line[j + 1] == "u" else 2
                continue
            if c in "=: \t\f":
                break
            key_chars.append(c)
            j += 1
        while j < len(line) and line[j] in " \t\f":
            j += 1
        if j < len(line) and line[j] in "=:":
            j += 1
            while j < len(line) and line[j] in " \t\f":
                j += 1
        value_chars: list[str] = []
        while j < len(line):
            if line[j] == "\\" and j + 1 < len(line):
                value_chars.append(_unescape_char(line, j))
                j += 6 if line[j + 1] == "u" else 2
            else:
                value_chars.append(line[j])
                j += 1
        result["".join(key_chars)] = "".join(value_chars)
    return result


def _ends_with_continuation(line: str) -> bool:
    backslashes = len(line) - len(line.rstrip("\\"))
    return backslashes % 2 == 1


def _unescape_char(line: str, j: int) -> str:
    nxt = line[j + 1]
    if nxt == "u":
        try:
            return chr(int(line[j + 2:j + 6], 16))
        except ValueError:
            raise McDeployError(f"invalid \\u escape in properties line: {line!r}") from None
    return _PROP_ESCAPES.get(nxt, nxt)


def parse(fmt: str, text: str):
    """Parse text in the given format; raises McDeployError with the parser's message."""
    try:
        if fmt == "yaml":
            # A file may hold several documents; parse them all.
            return list(_yaml().load_all(io.StringIO(text)))
        if fmt == "toml":
            return tomllib.loads(text)
        if fmt == "json":
            return json.loads(text)
        if fmt == "hocon":
            return _hocon_to_python(ConfigFactory.parse_string(text, resolve=False))
        if fmt == "properties":
            return parse_properties(text)
        return text
    except McDeployError:
        raise
    except Exception as exc:  # parser libraries raise many unrelated exception types
        raise McDeployError(f"{fmt} parse error: {exc}") from None


def _hocon_to_python(value):
    if isinstance(value, ConfigTree):
        return {k: _hocon_to_python(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_hocon_to_python(v) for v in value]
    return value


def walk_compare(sentinel_doc, real_doc, sentinels: dict[str, str], where: str = "") -> list[str]:
    """Compare a document rendered with sentinels against the same document rendered with real values.

    Every scalar holding a sentinel must equal the sentinel-free expectation in the real document;
    the structure around it must be unchanged. Returns a list of problems (empty when verified).
    """
    problems: list[str] = []
    if isinstance(sentinel_doc, dict):
        bad_keys = [k for k in sentinel_doc if isinstance(k, str) and any(s in k for s in sentinels)]
        if bad_keys:
            return [f"{where}/{k}: placeholders are not allowed in keys" for k in bad_keys]
        if not isinstance(real_doc, dict) or list(sentinel_doc) != list(real_doc):
            return [f"{where or '<root>'}: document structure changed after substitution"]
        for key in sentinel_doc:
            problems += walk_compare(sentinel_doc[key], real_doc[key], sentinels, f"{where}/{key}")
        return problems
    if isinstance(sentinel_doc, list):
        if not isinstance(real_doc, list) or len(sentinel_doc) != len(real_doc):
            return [f"{where or '<root>'}: document structure changed after substitution"]
        for idx, (s_item, r_item) in enumerate(zip(sentinel_doc, real_doc)):
            problems += walk_compare(s_item, r_item, sentinels, f"{where}[{idx}]")
        return problems
    if isinstance(sentinel_doc, str) and any(s in sentinel_doc for s in sentinels):
        expected = sentinel_doc
        for sentinel, value in sentinels.items():
            expected = expected.replace(sentinel, value)
        if isinstance(real_doc, str) and real_doc == expected:
            return []
        # A bare placeholder that renders to a number (e.g. a port) is intended to be numeric.
        if (sentinel_doc in sentinels and isinstance(real_doc, (int, float))
                and not isinstance(real_doc, bool) and str(real_doc) == expected):
            return []
        return [f"{where}: value did not survive substitution intact (check quoting of the placeholder)"]
    if sentinel_doc != real_doc:
        return [f"{where or '<root>'}: value changed although it contains no placeholder"]
    return []


SAFE_BARE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]*$")
YAML11_SPECIAL = {
    "y", "yes", "n", "no", "true", "false", "on", "off", "null", "~",
}


def bare_value_ok(fmt: str, value: str) -> bool:
    """Whether a value may be substituted into an unquoted position without changing meaning."""
    if fmt in ("yaml", "hocon"):
        return bool(SAFE_BARE_RE.match(value)) and value.lower() not in YAML11_SPECIAL
    return True
