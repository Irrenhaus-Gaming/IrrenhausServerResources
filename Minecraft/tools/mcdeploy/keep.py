"""Keep tokens (__KEEP_NAME__): a value the server generated itself and that must survive deploys,
e.g. a plugin's per-instance id.

The repo line holds the token; at deploy the value is read from the same line in the server's
current file (same text before and after the token, optional quotes around the value). A server
without that file gets a new random UUID, as the plugin would generate on first start. Boot tests
and `mc render` use a dummy value.
"""
from __future__ import annotations

import re
import uuid

from .errors import McDeployError, fail_if
from .placeholders import CONTROL_RE, TOKEN_RE

KEEP_KIND = "keep"
QUOTED_RE = re.compile(r"""^(["'])(.*)\1$""")


def keep_lines(rel: str, text: str) -> dict[str, tuple[str, str]]:
    """Keep token NAME -> (text before, text after) on its repo line. One token per line and name."""
    found: dict[str, tuple[str, str]] = {}
    problems: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        tokens = list(TOKEN_RE.finditer(line))
        keeps = [m for m in tokens if m.group(1) == "KEEP"]
        if not keeps:
            continue
        if len(tokens) > 1:
            problems.append(f"{rel}:{lineno}: a __KEEP_ token must be the only placeholder on its line")
            continue
        m = keeps[0]
        if m.group(2) in found:
            problems.append(f"{rel}:{lineno}: __KEEP_{m.group(2)}__ is used more than once")
        found[m.group(2)] = (line[:m.start()], line[m.end():])
    fail_if(problems, "Invalid keep placeholders:")
    return found


def value_from_server(rel: str, name: str, before: str, after: str, server_text: str) -> str:
    values = set()
    for line in server_text.splitlines():
        if line.startswith(before) and line.endswith(after) and len(line) >= len(before) + len(after):
            raw = line[len(before):len(line) - len(after)]
            quoted = QUOTED_RE.match(raw)
            values.add(quoted.group(2) if quoted else raw)
    if len(values) != 1:
        found = "no line" if not values else f"{len(values)} different lines"
        raise McDeployError(
            f"{rel}: __KEEP_{name}__: expected exactly one server line like '{before.strip()}<value>{after}', "
            f"found {found}. Fix the server file or pull it first."
        )
    value = values.pop()
    if not value or CONTROL_RE.search(value) or value != value.strip():
        raise McDeployError(f"{rel}: __KEEP_{name}__: the server value is empty or has surrounding whitespace")
    return value


def keep_values(files: dict[str, str], read_remote) -> dict[str, str]:
    """Value of every keep token. `files` maps rel path -> repo text (only files with keep tokens);
    `read_remote(rel)` returns the server's bytes or None."""
    values: dict[str, str] = {}
    problems: list[str] = []
    for rel, text in sorted(files.items()):
        lines = keep_lines(rel, text)
        data = read_remote(rel)
        for name, (before, after) in lines.items():
            if name in values:
                problems.append(f"{rel}: __KEEP_{name}__ is also used in another file")
                continue
            if data is None:
                values[name] = str(uuid.uuid4())
                print(f"{rel}: not on the server yet, __KEEP_{name}__ gets a new id")
                continue
            try:
                values[name] = value_from_server(rel, name, before, after, data.decode("utf-8", errors="replace"))
            except McDeployError as exc:
                problems.append(str(exc))
    fail_if(problems, "Cannot read kept values from the server (nothing was uploaded):")
    return values


def dummy_keep_values(names) -> dict[str, str]:
    """Stand-in values where no server is involved (boot test, mc render)."""
    return {n: str(uuid.uuid5(uuid.NAMESPACE_URL, f"mcdeploy-keep:{n}")) for n in names}
