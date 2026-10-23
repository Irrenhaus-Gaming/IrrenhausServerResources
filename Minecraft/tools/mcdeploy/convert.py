"""One-time conversion of the raw server dump into masked, placeholder-based config.

Step 1 `convert-scan` writes a review plan and a values file OUTSIDE the repo.
Step 2 `convert-apply` masks every planned occurrence in place, proves that rendering the masked
file with the original values reproduces the original document, and updates the schema,
test values and scan allowlist.
"""
from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from pathlib import Path, PurePosixPath

from ruamel.yaml import YAML

from .config import (
    MC_DIR, REPO_ROOT, SCAN_ALLOWLIST_FILE, SCHEMA_FILE, TEST_VALUES_FILE, load_layout,
    load_yaml, parse_env_file, source_files,
)
from .errors import McDeployError, fail_if
from .formats import detect_format, parse
from .placeholders import quote_context, render_file
from .scan import ASSIGN_RE, Finding, _value_span, filter_allowed, load_scan_allowlist, scan_text, value_fingerprint

DB_HINT_RE = re.compile(r"mysql|maria|sql|database|storage|jdbc|db", re.I)


def _outside_repo(path: Path) -> Path:
    path = path.resolve()
    if path == REPO_ROOT or REPO_ROOT in path.parents:
        raise McDeployError(f"{path} is inside the repository; secret values must be kept outside it")
    return path


def candidate_files() -> dict[str, Path]:
    """Minecraft-relative path -> absolute path for every file of every target source."""
    files: dict[str, Path] = {}
    for source in load_layout().sources():
        for rel in source_files(source):
            files[f"{source}/{rel}"] = MC_DIR / source / rel
    return files


def _scope(paths: list[str]) -> str:
    scopes = set()
    for p in paths:
        parts = PurePosixPath(p).parts
        if len(parts) > 2 and parts[1] == "plugins":
            scopes.add(parts[2])
        else:
            scopes.add(PurePosixPath(p).stem.split("-")[0])
    return scopes.pop() if len(scopes) == 1 else "SHARED"


def _thing(key: str, reason: str) -> str:
    norm = re.sub(r"[^a-z0-9]", "", key.lower())
    if reason == "IP address":
        return f"{key}_HOST" if key else "HOST"
    for word, thing in (("password", "PASSWORD"), ("passwd", "PASSWORD"), ("pass", "PASSWORD"),
                        ("token", "TOKEN"), ("secret", "SECRET"), ("username", "USERNAME"),
                        ("user", "USERNAME"), ("webhook", "WEBHOOK"), ("host", "HOST"),
                        ("address", "HOST"), ("url", "URL"), ("uri", "URL")):
        if norm.endswith(word):
            return thing
    return key or "VALUE"


def _clean_name(raw: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Z0-9]", "_", raw.upper())).strip("_")


def run_convert_scan(out_dir: Path) -> None:
    out_dir = _outside_repo(out_dir)
    files = candidate_files()
    texts = {}
    for rel, path in files.items():
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue
        try:
            texts[rel] = data.decode("utf-8")
        except UnicodeDecodeError:
            raise McDeployError(f"{rel}: not UTF-8") from None
    allow = load_scan_allowlist()
    findings: list[Finding] = []
    for rel, text in texts.items():
        findings += filter_allowed(scan_text(rel, text), allow)

    groups: "OrderedDict[str, list[Finding]]" = OrderedDict()
    for f in findings:
        groups.setdefault(f.value, []).append(f)
    # Also catch every other occurrence of each value (e.g. the same password elsewhere).
    for value, group in groups.items():
        seen = {(f.path, f.line) for f in group}
        if len(value) < 4:
            continue
        for rel, text in texts.items():
            for lineno, line in enumerate(text.splitlines(), 1):
                if value in line and (rel, lineno) not in seen:
                    m = ASSIGN_RE.match(line)
                    idx = line.find(value)
                    group.append(Finding(rel, lineno, m.group("key").strip() if m else "", "same value", value, idx, idx + len(value)))
                    seen.add((rel, lineno))

    plan = []
    values = []
    used_names: set[str] = set()
    for idx, (value, group) in enumerate(groups.items(), 1):
        keys = {f.key for f in group if f.reason != "same value"}
        key = keys.pop() if len(keys) == 1 else ""
        primary = next(f for f in group if f.reason != "same value")
        name = _clean_name(f"MC_{_scope([f.path for f in group])}_{_thing(key, primary.reason)}")
        base, n = name, 2
        while name in used_names:
            name, n = f"{base}_{n}", n + 1
        used_names.add(name)
        vid = f"v{idx:02d}"
        plan.append({
            "id": vid,
            "name": name,
            "action": "mask",
            "reason": "",
            "fingerprint": f"sha256:{hashlib.sha256(value.encode()).hexdigest()[:10]} len={len(value)}",
            "occurrences": [{"file": f.path, "line": f.line, "key": f.key, "why": f.reason} for f in group],
        })
        values.append(f"{vid}={value}")
    out_dir.mkdir(parents=True, exist_ok=True)
    yaml = YAML()
    yaml.width = 200
    with (out_dir / "convert-plan.yml").open("w", encoding="utf-8") as fh:
        fh.write(
            "# Review every entry before running convert-apply.\n"
            "#  name:   placeholder name (A-Z, 0-9, _). Entries with the same name are merged.\n"
            "#  action: mask  -> replace with __SECRET_<name>__\n"
            "#          allow -> keep the value in the repo (false positive); 'reason' is then required\n"
            "#  scope:  (optional) keyed -> mask only the listed lines, for values that are also ordinary words\n"
        )
        yaml.dump({"secrets": plan}, fh)
    (out_dir / "values.env").write_text("\n".join(values) + "\n", encoding="utf-8")
    print(f"{len(plan)} distinct values in {len(findings)} findings.")
    print(f"Plan:   {out_dir / 'convert-plan.yml'}")
    print(f"Values: {out_dir / 'values.env'}  (secret! keep outside the repo)")


def _mask_line(line: str, value: str, token: str, fmt: str, where: str) -> str:
    """Replace every occurrence of `value` in one line with the token, quoting where needed."""
    if value not in line:
        raise McDeployError(f"{where}: value not found on this line (file changed since the scan?)")
    out = []
    pos = 0
    while True:
        idx = line.find(value, pos)
        if idx == -1:
            out.append(line[pos:])
            break
        context = quote_context(line[:idx], fmt)
        out.append(line[pos:idx])
        if context != "bare" or fmt in ("properties", "text"):
            out.append(token)
        else:
            m = ASSIGN_RE.match(line)
            if not m:
                raise McDeployError(f"{where}: unquoted value outside a key/value line; mask it by hand")
            start, end, scalar = _value_span(line, m)
            if fmt in ("toml", "json"):
                raise McDeployError(f"{where}: unquoted {fmt} value; mask it by hand")
            if not start <= idx < end:
                raise McDeployError(f"{where}: value outside the assigned value; mask it by hand")
            if (start, end) != (idx, idx + len(value)):
                # Part of a longer unquoted scalar (e.g. host:port): quote the whole scalar.
                if pos:
                    raise McDeployError(f"{where}: several masked values in one unquoted scalar; quote it by hand")
                quoted = scalar.replace("\\", "\\\\").replace('"', '\\"').replace(value, token)
                return line[:start] + f'"{quoted}"' + line[end:]
            # A whole numeric value (a port) stays unquoted so it still renders as a number.
            out.append(token if value.isdigit() else f'"{token}"')
        pos = idx + len(value)
    return "".join(out)


def run_convert_apply(plan_path: Path, values_path: Path) -> None:
    plan_path = _outside_repo(plan_path)
    values_path = _outside_repo(values_path)
    plan = (load_yaml(plan_path) or {}).get("secrets") or []
    raw_values = parse_env_file(values_path)
    files = candidate_files()

    problems: list[str] = []
    masks: dict[str, str] = {}        # name -> value
    kinds: dict[str, str] = {}        # name -> "secret" | "var"
    keyed: set[str] = set()           # names masked only at the listed key/value lines
    edits: dict[str, list[tuple[int, str, str]]] = {}  # file -> [(line, value, token)]
    allows: list[dict] = []
    name_targets: dict[str, set[str]] = {}
    name_hint: dict[str, str] = {}
    for entry in plan:
        vid, name, action = entry.get("id"), entry.get("name"), entry.get("action")
        if vid not in raw_values:
            problems.append(f"{vid}: no value in {values_path.name}")
            continue
        value = raw_values[vid]
        if action == "allow":
            if not str(entry.get("reason") or "").strip():
                problems.append(f"{vid}: action allow needs a reason")
            for occ in entry["occurrences"]:
                if occ["key"]:
                    allows.append({"path": occ["file"], "key": occ["key"],
                                   "value_sha256": value_fingerprint(value), "reason": entry["reason"]})
                else:
                    problems.append(f"{vid}: {occ['file']}:{occ['line']} has no key; allowlisting needs one, mask it instead")
            continue
        if action != "mask":
            problems.append(f"{vid}: action must be mask or allow")
            continue
        if not isinstance(name, str) or not re.match(r"^[A-Z0-9]+(?:_[A-Z0-9]+)*$", name):
            problems.append(f"{vid}: invalid name {name!r}")
            continue
        if name in masks and masks[name] != value:
            problems.append(f"{vid}: name {name} is used for two different values")
        kind = entry.get("kind", "secret")
        if kind not in ("secret", "var"):
            problems.append(f"{vid}: kind must be secret or var")
        if kinds.setdefault(name, kind) != kind:
            problems.append(f"{vid}: name {name} is used as both secret and var")
        masks[name] = value
        scope = entry.get("scope", "everywhere")
        if scope not in ("everywhere", "keyed"):
            problems.append(f"{vid}: scope must be everywhere or keyed")
        if scope == "keyed" or kind == "var":
            keyed.add(name)
        for occ in entry["occurrences"]:
            edits.setdefault(occ["file"], []).append((occ["line"], value, f"__{kind.upper()}_{name}__"))
            source = PurePosixPath(occ["file"]).parts[0]
            name_targets.setdefault(name, set()).add(source)
            name_hint.setdefault(name, f"{occ['key'] or 'value'} in {occ['file']}")
    fail_if(problems, "convert plan is invalid:")

    new_texts: dict[str, str] = {}
    for rel, file_edits in edits.items():
        if rel not in files:
            problems.append(f"{rel}: not a managed file")
            continue
        original = files[rel].read_text(encoding="utf-8")
        lines = original.splitlines(keepends=True)
        fmt = detect_format(rel)
        # Longest values first so a value contained in another one is not split.
        for lineno, value, token in sorted(file_edits, key=lambda e: -len(e[1])):
            body = lines[lineno - 1].rstrip("\r\n")
            ending = lines[lineno - 1][len(body):]
            try:
                lines[lineno - 1] = _mask_line(body, value, token, fmt, f"{rel}:{lineno}") + ending
            except McDeployError as exc:
                problems.append(str(exc))
        masked = "".join(lines)
        # Prove the round trip: rendering with the original values must give the original document.
        try:
            rendered = render_file(rel, masked, masks)
            if fmt == "text":
                if rendered != original:
                    problems.append(f"{rel}: masked file does not render back to the original")
            elif parse(fmt, rendered) != parse(fmt, original):
                problems.append(f"{rel}: masked file does not render back to the original document")
        except McDeployError as exc:
            problems.append(f"{rel}: {exc}")
        new_texts[rel] = masked
    fail_if(problems, "convert-apply failed, nothing was written:")

    # Every masked value must be gone from every managed file.
    for rel, path in files.items():
        text = new_texts.get(rel)
        if text is None:
            data = path.read_bytes()
            if b"\0" in data[:8192]:
                continue
            text = data.decode("utf-8")
        for name, value in masks.items():
            if name not in keyed and len(value) >= 4 and value in text:
                problems.append(f"{rel}: still contains the value of {name}")
    fail_if(problems, "convert-apply failed, nothing was written:")

    for rel, text in new_texts.items():
        files[rel].write_bytes(text.encode("utf-8"))
    _update_schema(masks, kinds, name_targets, name_hint, keyed)
    _update_test_values(masks, kinds, name_hint)
    _update_allowlist(allows)
    print(f"Masked {len(masks)} values in {len(new_texts)} files; schema, test values and allowlist updated.")
    print("Next: review `git diff`, run `mc lint`, then load the values into GitHub Environment secrets.")


def _update_schema(masks, kinds, name_targets, name_hint, keyed) -> None:
    yaml = YAML()
    data = load_yaml(SCHEMA_FILE) if SCHEMA_FILE.is_file() else None
    data = data or {}
    placeholders = data.get("placeholders") or {}
    for name in sorted(masks):
        entry = placeholders.get(name) or {"kind": kinds[name], "description": f"Converted from {name_hint[name]}"}
        entry.setdefault("scope", "environment")
        entry["sources"] = sorted(set(entry.get("sources") or []) | name_targets[name])
        if name in keyed and kinds[name] == "secret":
            entry["mask"] = "keyed"
        placeholders[name] = entry
    with SCHEMA_FILE.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(SCHEMA_HEADER)
        yaml.dump({"placeholders": placeholders}, fh)


def _update_test_values(masks, kinds, name_hint) -> None:
    existing = parse_env_file(TEST_VALUES_FILE) if TEST_VALUES_FILE.is_file() else {}
    lines = TEST_VALUES_FILE.read_text(encoding="utf-8").splitlines() if TEST_VALUES_FILE.is_file() else [TEST_VALUES_HEADER.rstrip()]
    root_pw = existing.get("TEST_MYSQL_ROOT_PASSWORD", "mcdeploy-test-root")
    for key, default in (("TEST_MYSQL_ROOT_PASSWORD", root_pw), ("TEST_MYSQL_DATABASES", "")):
        if key not in existing:
            lines.append(f"{key}={default}")
    for name in sorted(masks):
        if name in existing:
            continue
        hint = name_hint[name]
        lowered = name.lower()
        if "password" in lowered and DB_HINT_RE.search(hint + name):
            value = root_pw
        elif "username" in lowered and DB_HINT_RE.search(hint + name):
            value = "root"
        elif lowered.endswith("_host"):
            value = "127.0.0.1"  # the boot test shares MySQL's network namespace
        elif lowered.endswith("_port"):
            value = "3306"
        else:
            value = f"test-{lowered.replace('_', '-')}"
        lines.append(f"{name}={value}")
    TEST_VALUES_FILE.parent.mkdir(parents=True, exist_ok=True)
    TEST_VALUES_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _update_allowlist(allows) -> None:
    if not allows:
        return
    yaml = YAML()
    data = (load_yaml(SCAN_ALLOWLIST_FILE) if SCAN_ALLOWLIST_FILE.is_file() else None) or {}
    entries = data.get("allow") or []
    known = {(e["path"], e["key"], e["value_sha256"]) for e in entries}
    entries += [a for a in allows if (a["path"], a["key"], a["value_sha256"]) not in known]
    with SCAN_ALLOWLIST_FILE.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(ALLOWLIST_HEADER)
        yaml.dump({"allow": entries}, fh)


SCHEMA_HEADER = """\
# Every placeholder used in Minecraft/<source>/ files must be declared here (mc lint enforces it).
#   kind: secret -> GitHub Environment secret, written as __SECRET_<NAME>__
#         var    -> GitHub Environment variable, written as __VAR_<NAME>__
#   sources: config folders under Minecraft/ whose files use it.
# A test value for every placeholder lives in Minecraft/test/test-values.env.
"""

TEST_VALUES_HEADER = """\
# Dummy values for the boot test. NOT secret: never put real values here.
# TEST_MYSQL_* configure the throwaway MySQL container; everything else fills a placeholder.
"""

ALLOWLIST_HEADER = """\
# Reviewed false positives of the secret scanner: values that look secret but are safe to publish
# (plugin defaults of disabled features, public URLs, ...). Each entry needs a reason.
# value_sha256 = first 12 hex chars of sha256(value); if the value changes the scanner flags it again.
"""
