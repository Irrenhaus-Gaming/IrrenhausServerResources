"""Sync-back: bring files changed on the server into the repo, with every secret masked again."""
from __future__ import annotations

import json
import re

from .blocks import entries_from_server
from .config import OUTPUTS, Target, deploy_values, generated_files, load_schema, managed_files
from .deploy import (
    MANIFEST, Masker, content_hash, load_expansions, load_mask_scopes, manifest_hash, render_text, resolve_values,
    same_content, target_placeholders,
)
from .errors import McDeployError, fail_if
from .formats import detect_format, parse
from .owned import owned_keys, with_values_from
from .remote import SftpRemote, verify_instance
from .scan import filter_allowed, load_scan_allowlist, scan_text

# A plugin rewriting its config may drop the quotes around a value; put them back around tokens.
BARE_TOKEN_RE = re.compile(r"^(?P<head>[^#\r\n]*?[:=][ \t]+)(?P<token>__(?:SECRET|VAR|STYLE|REWARD|SERVER)_[A-Z0-9_]+__)(?P<tail>[ \t]*(?:#.*)?)$", re.M)


def requote(text: str, fmt: str, repo_lines: frozenset[str] = frozenset()) -> str:
    """Quote bare placeholders the plugin unquoted, except on lines the repo has verbatim
    (e.g. a deliberately bare port placeholder that must render as a number)."""
    if fmt not in ("yaml", "hocon"):
        return text

    def fix(m: re.Match) -> str:
        if m.group(0) in repo_lines:
            return m.group(0)
        return f'{m["head"]}"{m["token"]}"{m["tail"]}'

    return BARE_TOKEN_RE.sub(fix, text)


def run_pull(target: Target, extra_paths: list[str]) -> None:
    values = deploy_values()
    names = target_placeholders(target)
    # Collected placeholders (the Velocity server list) only exist inside a deploy run; here they
    # are read back from the server's own file, per file below.
    collected = {n for n, e in load_schema().items() if e.scope == "collected"}
    base_resolved = resolve_values(target, {n: k for n, k in names.items() if n not in collected}, values, f"target {target.name}")
    expansions = load_expansions()
    scopes = load_mask_scopes()
    allow = load_scan_allowlist()
    listed: list[str] = []
    generated = generated_files(target)
    extra = [p.strip("/") for p in extra_paths]
    refused = [p for p in extra if p in generated]
    fail_if([f"{p}: generated from Minecraft/shop/ at render time, never stored in the repo" for p in refused],
            f"{target.name}: --add cannot import these files:")
    paths = managed_files(target, include_untracked=False) + extra
    remote = SftpRemote(values)
    try:
        verify_instance(target, remote, values)
    except McDeployError:
        remote.close()
        raise
    problems: list[str] = []
    updates: dict[str, bytes] = {}
    try:
        remote_files = remote.read_many(paths)
        deployed = json.loads(remote.read(MANIFEST) or b"{}") if generated else {}
        for rel in paths:
            data = remote_files[rel]
            if rel in generated:
                # Built from Minecraft/shop/, never written into the repo. Not deployed yet: the next
                # deploy creates it or replaces the plugin's stock file. Deployed and edited since:
                # the edit has to be made in Minecraft/shop/ instead.
                keys = owned_keys(target, rel)
                expected = deployed.get("files", {}).get(rel)
                edited = (data is not None and expected is not None and manifest_hash(data, keys) != expected
                          and content_hash(rel, data, keys) != deployed.get("content", {}).get(rel))
                if edited:
                    problems.append(f"{rel}: changed on the server, but it is generated from Minecraft/shop/; "
                                    f"make the change there (the next deploy then replaces the server's copy)")
                continue
            if data is None:
                problems.append(f"{rel}: missing on the server")
                continue
            local = target.source_dir / rel
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                if not local.is_file() or local.read_bytes() != data:
                    updates[rel] = data
                continue
            fmt = detect_format(rel)
            repo_text = local.read_text(encoding="utf-8") if local.is_file() else ""
            resolved = dict(base_resolved)
            try:
                for name in sorted(collected & set(names)):
                    value = entries_from_server(rel, text, repo_text, name)
                    if value is not None:
                        resolved[name] = value
                        listed += [f"{k} ({rel})" for k in json.loads(value)]
            except McDeployError as exc:
                problems.append(str(exc))
                continue
            if local.is_file():
                try:
                    unchanged = same_content(rel, data, render_text(rel, repo_text, resolved, expansions).encode("utf-8"),
                                             owned_keys(target, rel))
                except McDeployError:
                    unchanged = False  # compared the normal way below
                if unchanged:
                    continue  # only the plugin's layout (or a server-owned value) differs from the repo
            masker =Masker({n: k for n, k in names.items() if n in resolved}, resolved, scopes, expansions)
            masked = requote(masker.apply(rel, text, repo_text), fmt, frozenset(repo_text.splitlines()))
            # Masking must be lossless: rendering the masked text gives the server's document back.
            try:
                rendered = render_text(rel, masked, resolved, expansions)
                same = rendered == text if fmt == "text" else parse(fmt, rendered) == parse(fmt, text)
            except McDeployError as exc:
                problems.append(f"{rel}: {exc}")
                continue
            if not same:
                problems.append(f"{rel}: masking is not lossless (a secret value may also occur as normal text)")
                continue
            hits = filter_allowed(scan_text(f"{target.source}/{rel}", masked), allow)
            for f in hits:
                problems.append(
                    f"{rel}:{f.line}: unmasked possible secret ({f.reason}, key '{f.key}') - add a placeholder "
                    f"in the repo first or allowlist it"
                )
            pulled = masked.encode("utf-8")
            if local.is_file():
                # Server-owned values stay on the server; the repo keeps its default for new installs.
                pulled = with_values_from(pulled, local.read_bytes(), owned_keys(target, rel))
            if not local.is_file() or local.read_bytes() != pulled:
                updates[rel] = pulled
    finally:
        remote.close()
    fail_if(problems, f"{target.name}: sync-back refused, nothing was written:")
    for rel, data in updates.items():
        dest = target.source_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    OUTPUTS.add_summary(
        f"### {target.name}: pulled {len(updates)} changed file(s) from the server\n"
        + "\n".join(f"- `{r}`" for r in sorted(updates))
        + (("\n\nServer list found on the server (not stored in the repo; it is rebuilt from the "
            "mc-backend-*/mc-server-* environments on the next deploy): " + ", ".join(listed)) if listed else "")
    )
    OUTPUTS.set_output("changed", str(len(updates)))
    print(f"{target.name}: {len(updates)} file(s) updated from the server")
