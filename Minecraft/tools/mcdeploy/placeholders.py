"""Placeholder tokens (__SECRET_NAME__ / __VAR_NAME__, knobs: __STYLE_NAME__ / __REWARD_NAME__, __KEEP_NAME__) and
rendering them into real values.

Rendering is plain text substitution so comments and formatting survive. Each value is escaped
for the quoting context it lands in, and the result is then verified by parsing: the file is
rendered a second time with unique sentinels and both parse trees must agree value for value.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import McDeployError
from .formats import bare_value_ok, detect_format, parse, walk_compare

TOKEN_RE = re.compile(r"__(SECRET|VAR|STYLE|REWARD|KEEP)_([A-Z0-9]+(?:_[A-Z0-9]+)*)__")
# Anything that looks like an attempted token but does not match TOKEN_RE is a typo.
TOKEN_LIKE_RE = re.compile(r"__(?:SECRET|VAR|STYLE|REWARD|KEEP)_")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class TokenUse:
    path: str
    line: int
    kind: str  # "secret", "var", a knob kind ("style", "reward") or "keep"
    name: str


def find_tokens(path: str, text: str) -> tuple[list[TokenUse], list[str]]:
    """Return token uses and malformed-token problems in one file."""
    uses: list[TokenUse] = []
    problems: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        valid_spans = []
        for m in TOKEN_RE.finditer(line):
            uses.append(TokenUse(path, lineno, m.group(1).lower(), m.group(2)))
            valid_spans.append(m.span())
        for m in TOKEN_LIKE_RE.finditer(line):
            if not any(start <= m.start() < end for start, end in valid_spans):
                problems.append(f"{path}:{lineno}: malformed placeholder near '{line[m.start():m.start() + 40]}'")
    return uses, problems


def quote_context(prefix: str, fmt: str) -> str:
    """Quoting state at the end of `prefix` (the line text before a token): '"', "'" or 'bare'."""
    if fmt in ("properties", "text"):
        return "bare"
    state = None
    i = 0
    while i < len(prefix):
        c = prefix[i]
        if state is None:
            if c == '"':
                state = '"'
            elif c == "'" and fmt in ("yaml", "toml"):
                state = "'"
        elif state == '"':
            if c == "\\":
                i += 1
            elif c == '"':
                state = None
        elif state == "'":
            if c == "'":
                if fmt == "yaml" and i + 1 < len(prefix) and prefix[i + 1] == "'":
                    i += 1
                else:
                    state = None
        i += 1
    return state or "bare"


def escape_value(value: str, fmt: str, context: str, where: str) -> str:
    if CONTROL_RE.search(value):
        raise McDeployError(f"{where}: value contains control characters (newlines etc.), refusing to render")
    if context == '"':
        return value.replace("\\", "\\\\").replace('"', '\\"')
    if context == "'":
        if fmt == "toml" and "'" in value:
            raise McDeployError(f"{where}: TOML literal strings cannot contain ', use a \"...\" string")
        return value.replace("'", "''")
    if fmt == "properties":
        if value[:1].isspace():
            raise McDeployError(f"{where}: properties values cannot start with whitespace")
        return value.replace("\\", "\\\\")
    if not bare_value_ok(fmt, value):
        raise McDeployError(
            f"{where}: value needs quoting in {fmt}; put the placeholder inside double quotes"
        )
    return value


def substitute(path: str, text: str, values: dict[str, str]) -> str:
    """Replace every token with its (escaped) value. `values` maps token NAME -> value."""
    fmt = detect_format(path)
    out_lines = []
    for lineno, line in enumerate(text.splitlines(keepends=True), 1):
        pieces = []
        last = 0
        for m in TOKEN_RE.finditer(line):
            name = m.group(2)
            if name not in values:
                raise McDeployError(f"{path}:{lineno}: no value for placeholder {name}")
            where = f"{path}:{lineno} ({name})"
            context = quote_context(line[:m.start()], fmt)
            pieces.append(line[last:m.start()])
            pieces.append(escape_value(values[name], fmt, context, where))
            last = m.end()
        pieces.append(line[last:])
        out_lines.append("".join(pieces))
    return "".join(out_lines)


def render_file(path: str, text: str, values: dict[str, str]) -> str:
    """Render one file and verify the result. Raises McDeployError on any doubt."""
    uses, problems = find_tokens(path, text)
    if problems:
        raise McDeployError("\n".join(problems))
    if not uses:
        return text
    rendered = substitute(path, text, values)
    leftover = TOKEN_LIKE_RE.search(rendered)
    if leftover:
        raise McDeployError(f"{path}: placeholder text remains after rendering")
    fmt = detect_format(path)
    if fmt != "text":
        names = sorted({u.name for u in uses})
        sentinels = {f"MCDSENTINEL{i:04d}X": n for i, n in enumerate(names)}
        sentinel_text = substitute(path, text, {n: s for s, n in sentinels.items()})
        try:
            sentinel_doc = parse(fmt, sentinel_text)
            real_doc = parse(fmt, rendered)
        except McDeployError as exc:
            raise McDeployError(f"{path}: rendered file does not parse: {exc}") from None
        problems = walk_compare(sentinel_doc, real_doc, {s: values[n] for s, n in sentinels.items()})
        if problems:
            raise McDeployError(f"{path}: render verification failed:\n" + "\n".join(f"  {p}" for p in problems))
    return rendered


def token_text(kind: str, name: str) -> str:
    return f"__{kind.upper()}_{name}__"


def mask(text: str, tokens: dict[str, str]) -> str:
    """Replace known values by their token text (longest values first). `tokens` maps token -> value."""
    for token, value in sorted(tokens.items(), key=lambda kv: -len(kv[1])):
        if value:
            text = text.replace(value, token)
    return text
