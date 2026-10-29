"""Static validation that needs no secrets and no network. Runs on every push/PR and in the
pre-commit hook. Collects every problem and fails once at the end."""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from .config import (
    REPO_ROOT, TEST_STACK_KEYS, OUTPUTS, load_layout, load_schema, load_test_values,
    source_files,
)
from .blocks import expand_blocks
from . import shop
from .workflowcheck import check_workflows
from .errors import McDeployError, fail_if
from .formats import detect_format, parse
from .knobs import KNOB_KINDS, knob_values, load_knobs
from .placeholders import find_tokens
from .scan import filter_allowed, load_scan_allowlist, scan_text
from .serverinfo import SERVER_KIND, check_server_tokens

# Second line of defence behind .gitignore: these must never be tracked, even with `git add -f`.
# (.zip is only ignored, not forbidden: the ResourcePackManager blueprint template is a tracked zip.)
FORBIDDEN_SUFFIXES = {
    ".jar", ".db", ".duckdb", ".log", ".bak", ".backup", ".mca", ".dat", ".pem", ".key",
}
FORBIDDEN_NAMES = {"forwarding.secret", "network-key", ".backend-id", "backend-id.txt"}
FORBIDDEN_SEGMENTS = {"playerdata"}
MAX_FILE_BYTES = 2 * 1024 * 1024

# Workflow / wrapper constructs that would let a failing step pass silently.
SWALLOW_PATTERNS = [
    (re.compile(r"continue-on-error"), "continue-on-error"),
    (re.compile(r"\|\|\s*true\b"), "'|| true'"),
    (re.compile(r"\bset\s+\+e\b"), "'set +e'"),
    (re.compile(r"\|\|\s*:\s*$", re.M), "'|| :'"),
]
SWALLOW_FILES = [".github/workflows/minecraft.yml", ".github/workflows/minecraft-sync.yml", "mc.sh", ".githooks/pre-commit"]

# ItsMyConfig is not installed: its placeholders, <p:...> tags and "$" parse marker would show as raw text.
ITSMYCONFIG_RE = re.compile(r"""itsmyconfig|<p:|["']\$<""", re.I)


def forbidden_reason(rel: str) -> str | None:
    p = PurePosixPath(rel)
    if p.name.endswith(".jar") or ".jar." in p.name:
        return "plugin/server jar"
    if p.suffix.lower() in FORBIDDEN_SUFFIXES:
        return f"forbidden file type {p.suffix}"
    if p.name in FORBIDDEN_NAMES:
        return "whole-file secret (server-only)"
    if FORBIDDEN_SEGMENTS & set(p.parts):
        return "player data"
    return None


def run_lint() -> None:
    layout = load_layout()
    schema = load_schema(layout)
    test_values = load_test_values()
    allow = load_scan_allowlist()
    expansions = {n: e.expand for n, e in schema.items() if e.expand}
    problems: list[str] = []
    used: dict[str, set[str]] = {}       # placeholder name -> source folders using it
    kinds: dict[str, set[str]] = {}      # placeholder name -> token kinds seen
    knobs: dict[str, str] = {}           # knob name -> kind (style/reward)
    file_count = 0

    for source, profile in layout.sources().items():
        files = source_files(source)
        if not files:
            problems.append(f"no managed files under Minecraft/{source}")
        for rel in files:
            full_rel = f"{source}/{rel}"
            path = REPO_ROOT / "Minecraft" / source / rel
            data = path.read_bytes()
            is_binary = b"\0" in data[:8192]
            text = None if is_binary else data.decode("utf-8", errors="strict") if _is_utf8(data) else None
            if text is not None:
                uses, token_problems = find_tokens(full_rel, text)
                token_problems += check_server_tokens(uses, profile)
                for u in uses:
                    if u.kind in KNOB_KINDS:
                        knobs[u.name] = u.kind
                        continue
                    if u.kind == SERVER_KIND:
                        continue
                    used.setdefault(u.name, set()).add(source)
                    kinds.setdefault(u.name, set()).add(u.kind)
            file_count += 1
            reason = forbidden_reason(rel)
            if reason:
                problems.append(f"{full_rel}: {reason} must not be tracked")
                continue
            if len(data) > MAX_FILE_BYTES:
                problems.append(f"{full_rel}: {len(data)} bytes; config files above 2 MiB are suspicious, exclude it")
            if is_binary:
                continue
            if text is None:
                problems.append(f"{full_rel}: not valid UTF-8")
                continue
            problems += token_problems
            for lineno, line in enumerate(text.splitlines(), 1):
                if ITSMYCONFIG_RE.search(line):
                    problems.append(f"{full_rel}:{lineno}: ItsMyConfig syntax; use __STYLE_/__REWARD_ knobs (knobs.yml) instead")
            fmt = detect_format(rel)
            if fmt != "text":
                try:
                    # List placeholders are expanded with their test values so the file parses.
                    expanded, _ = expand_blocks(full_rel, text, test_values, expansions)
                    parse(fmt, expanded)
                except McDeployError as exc:
                    problems.append(f"{full_rel}: {exc}")
            hits = filter_allowed(scan_text(full_rel, text), allow)
            for f in hits:
                problems.append(
                    f"{full_rel}:{f.line}: possible secret ({f.reason}, key '{f.key}'); replace it with a "
                    f"placeholder or add a reviewed entry to secret-scan-allowlist.yml"
                )

    for name, entry in schema.items():
        token_kinds = kinds.get(name, set())
        if not token_kinds:
            problems.append(f"schema: {name} is declared but not used in any file")
            continue
        if token_kinds != {entry.kind}:
            problems.append(f"schema: {name} is declared as {entry.kind} but used as {sorted(token_kinds)}")
        missing = used[name] - set(entry.sources)
        if missing:
            problems.append(f"schema: {name} is used in {sorted(missing)} but not listed in its sources")
        extra = set(entry.sources) - used[name]
        if extra:
            problems.append(f"schema: {name} lists sources {sorted(extra)} that never use it")
    try:
        knob_values(knobs, load_knobs())  # validates knobs.yml even when no file uses a knob
    except McDeployError as exc:
        problems.append(str(exc))
    for name in sorted(set(knobs) & (set(used) | set(schema))):
        problems.append(f"{name} is used both as a knob and as a secret/variable placeholder; rename one")
    for name in sorted(set(used) - set(schema)):
        problems.append(f"placeholder {name} is used but not declared in secrets.schema.yml")
    for name in sorted(set(schema) - set(test_values)):
        problems.append(f"test-values.env: no test value for {name}")
    for name in sorted(set(test_values) - set(schema) - TEST_STACK_KEYS):
        problems.append(f"test-values.env: {name} is not a declared placeholder")
    for key in sorted(TEST_STACK_KEYS - set(test_values)):
        problems.append(f"test-values.env: {key} is required by the boot test")

    for rel in SWALLOW_FILES:
        path = REPO_ROOT / rel
        if not path.is_file():
            problems.append(f"{rel}: missing")
            continue
        text = path.read_text(encoding="utf-8")
        for pattern, label in SWALLOW_PATTERNS:
            for m in pattern.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{rel}:{line}: {label} would hide failures; not allowed")

    problems += check_workflows()
    problems += shop.check()

    fail_if(problems, f"Lint failed ({len(problems)} problem(s)):")
    OUTPUTS.add_summary(
        f"### Lint passed\n{file_count} config files, {len(schema)} placeholders, {len(knobs)} knob tokens, "
        f"{len(layout.sources())} config folders."
    )
    print(f"lint ok: {file_count} files, {len(schema)} placeholders, {len(knobs)} knob tokens, "
          f"{len(layout.sources())} config folders")


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False
