"""Render, drift-check and upload a target's managed files."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone

from .config import (
    OUTPUTS, REPO_ROOT, Target, assert_clean_worktree, deploy_values, load_schema,
    managed_files, require_values,
)
from .blocks import expand_blocks, verify_blocks
from .errors import McDeployError, fail_if
from .formats import detect_format, parse
from .knobs import KNOB_KINDS, knob_values
from .owned import owned_keys, strip_owned, with_values_from
from .placeholders import TOKEN_RE, find_tokens, mask, render_file, substitute, token_text
from .remote import RemoteBase, SftpRemote, verify_instance
from .scan import scan_text
from .serverinfo import SERVER_KIND, check_server_tokens, server_values

MANIFEST = ".irrenhaus-deploy.json"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_hash(data: bytes, keys: tuple[str, ...]) -> str:
    """Hash recorded in the manifest: server-owned values do not count."""
    return sha256(strip_owned(data, keys))


def _canonical(value):
    """JSON-ready form of a parsed document: mapping order and key types no longer matter."""
    if isinstance(value, dict):
        return {"map": sorted([repr(k), _canonical(v)] for k, v in value.items())}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)  # dates and other YAML scalars


def content_hash(rel: str, data: bytes, keys: tuple[str, ...]) -> str | None:
    """Hash of the parsed document without its server-owned top-level keys, or None for text and
    unparseable files. A plugin that re-saves a file in its own layout (line folding, key order,
    quoting, dropped comments) keeps this hash; any changed value changes it."""
    fmt = detect_format(rel)
    if fmt == "text":
        return None
    try:
        doc = parse(fmt, data.decode("utf-8"))
    except (UnicodeDecodeError, McDeployError):
        return None
    docs = doc if fmt == "yaml" else [doc]
    docs = [{k: v for k, v in d.items() if k not in keys} if isinstance(d, dict) else d for d in docs]
    return sha256(json.dumps(_canonical(docs), ensure_ascii=False).encode("utf-8"))


def same_content(rel: str, a: bytes, b: bytes, keys: tuple[str, ...]) -> bool:
    if a == b:
        return True
    ha = content_hash(rel, a, keys)
    return ha is not None and ha == content_hash(rel, b, keys)


def deployed_unchanged(rel: str, remote_data: bytes, data: bytes, expected: str | None, expected_content: str | None,
                       old_keys: tuple[str, ...], keys: tuple[str, ...]) -> bool:
    """Is the server file still what the last deploy wrote, apart from server-owned values and layout?"""
    if expected is None:
        return False
    if manifest_hash(remote_data, old_keys) == expected:
        return True
    if expected_content is not None and content_hash(rel, remote_data, old_keys) == expected_content:
        return True  # re-saved by its plugin in another layout, same values
    # A manifest written before these keys were server-owned holds the hash of the whole file, and
    # the server may since have changed just those values (a hologram moved in game).
    return bool(keys) and not old_keys and sha256(with_values_from(remote_data, data, keys)) == expected


def target_placeholders(target: Target) -> dict[str, str]:
    """Placeholder name -> kind for everything this target's files use (validated against the schema,
    knobs against knobs.yml; server tokens need no declaration)."""
    schema = load_schema()
    names: dict[str, str] = {}
    for rel in managed_files(target):
        data = (target.source_dir / rel).read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        uses, problems = find_tokens(rel, text)
        fail_if(problems + check_server_tokens(uses, target.profile), "Malformed placeholders:")
        for u in uses:
            if names.setdefault(u.name, u.kind) != u.kind:
                fail_if([f"{u.name} is used as {names[u.name]} and as {u.kind} ({rel})"], "Conflicting placeholders:")
    knobs = {n: k for n, k in names.items() if k in KNOB_KINDS}
    knob_values(knobs)  # fails on unknown knobs
    names_in_schema = {n: k for n, k in names.items() if n not in knobs and k != SERVER_KIND}
    problems = [f"{n} is not declared in secrets.schema.yml" for n in names_in_schema if n not in schema]
    problems += [
        f"{n} is declared as {schema[n].kind} but used as {k}" for n, k in names.items()
        if n in schema and schema[n].kind != k
    ]
    problems += [
        f"{n} is used in Minecraft/{target.source} but its schema sources are {list(schema[n].sources)}"
        for n in names_in_schema if n in schema and target.source not in schema[n].sources
    ]
    fail_if(problems, f"{target.name}: placeholder declarations do not match:")
    return names


def resolve_values(target: Target, names: dict[str, str], values: dict[str, str], context: str) -> dict[str, str]:
    """Value of every placeholder in `names` (name -> kind): secrets and variables from `values`
    (all must be set), knobs computed from knobs.yml, server tokens from the target."""
    knobs = {n: k for n, k in names.items() if k in KNOB_KINDS}
    servers = [n for n, k in names.items() if k == SERVER_KIND]
    resolved = require_values(sorted(set(names) - set(knobs) - set(servers)), values, context)
    resolved.update(knob_values(knobs))
    resolved.update(server_values(target, servers))
    return resolved


def load_mask_scopes() -> dict[str, str]:
    return {n: e.mask for n, e in load_schema().items()}


def load_expansions() -> dict[str, str]:
    """Placeholder name -> expansion type for list placeholders (schema 'expand')."""
    return {n: e.expand for n, e in load_schema().items() if e.expand}


def render_text(path: str, text: str, values: dict[str, str], expansions: dict[str, str]) -> str:
    """Expand list placeholders, substitute the rest, verify both."""
    expanded, done = expand_blocks(path, text, values, expansions)
    rendered = render_file(path, expanded, values)
    verify_blocks(path, rendered, done)
    return rendered


class Masker:
    """Turns real values back into tokens for diffs and sync-back.

    1. Every server line that equals a rendered repo line containing placeholders gets the repo
       line back (exact, so a variable like "localhost" is only restored where the repo had it).
    2. Secrets with mask scope "everywhere" are then replaced wherever they still occur.
    Variables and "keyed" secrets (ordinary words) are never replaced globally; if one appears in
    a new credential-like line the secret scanner catches it.
    """

    def __init__(self, names: dict[str, str], resolved: dict[str, str], scopes: dict[str, str],
                 expansions: dict[str, str] | None = None):
        self.resolved = resolved
        self.expansions = expansions or {}
        self.everywhere = {
            token_text(names[n], n): v for n, v in resolved.items()
            if names[n] == "secret" and scopes.get(n, "everywhere") == "everywhere"
        }

    def apply(self, path: str, server_text: str, repo_text: str) -> str:
        restore: dict[str, str] = {}
        blocks: list[tuple[list[str], str]] = []  # (generated lines, placeholder line)
        for line in repo_text.splitlines():
            if not TOKEN_RE.search(line):
                continue
            _, done = expand_blocks(path, line + "\n", self.resolved, self.expansions)
            if done:
                blocks.append((done[0].lines, line))
            else:
                restore[substitute(path, line, self.resolved)] = line
        lines = server_text.splitlines(keepends=True)
        # A list placeholder is restored only where its exact generated lines appear in a row.
        for generated, placeholder in blocks:
            bodies = [l.rstrip("\r\n") for l in lines]
            for i in range(len(bodies) - len(generated) + 1):
                if bodies[i:i + len(generated)] == generated:
                    ending = lines[i][len(bodies[i]):]
                    lines[i:i + len(generated)] = [placeholder + ending]
                    break
        for i, line in enumerate(lines):
            body = line.rstrip("\r\n")
            if body in restore:
                lines[i] = restore[body] + line[len(body):]
        return mask("".join(lines), self.everywhere)


def render_target(target: Target, values: dict[str, str]) -> dict[str, bytes]:
    """Render every managed file. `values` must contain every placeholder the target uses."""
    rendered: dict[str, bytes] = {}
    problems: list[str] = []
    expansions = load_expansions()
    for rel in managed_files(target):
        data = (target.source_dir / rel).read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            rendered[rel] = data  # binary files are copied untouched
            continue
        try:
            rendered[rel] = render_text(rel, text, values, expansions).encode("utf-8")
        except McDeployError as exc:
            problems.append(str(exc))
    fail_if(problems, f"{target.name}: rendering failed (nothing was uploaded):")
    return rendered


def is_act() -> bool:
    return bool(os.environ.get("ACT"))


def masked_diff(rel: str, remote_text: str, repo_text: str, masker: Masker) -> list[str]:
    """Unified diff with every known value masked. Lines that still look secret are redacted:
    Actions logs of this public repo are public."""
    remote_masked = masker.apply(rel, remote_text, repo_text).splitlines()
    diff = list(difflib.unified_diff(repo_text.splitlines(), remote_masked, f"repo/{rel}", f"server/{rel}", lineterm="", n=2))
    out = []
    for line in diff:
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")) and scan_text(rel, line[1:]):
            out.append(line[0] + " <line redacted: looks like an unmasked secret>")
        else:
            out.append(line)
    return out


def check_drift(target: Target, remote: RemoteBase, rendered: dict[str, bytes], masker: Masker,
                adopt: bool = False, full: bool = False) -> tuple[dict, dict[str, bytes]]:
    """Compare server files with the last deploy manifest. Returns (manifest, files to upload).

    Incremental by default: the manifest holds the hash of every file as last deployed, so only
    files whose rendered content differs from it (changed config or changed secret) are read
    from the server, drift-checked and uploaded. Untouched files are never overwritten, so a
    server-side edit to them is preserved (bring it in with the sync workflow). The first
    deploy (no manifest), adopt and full=True compare every managed file.

    Server-owned keys (targets.yml `server_owned`) are left out of every comparison, and an
    upload keeps the server's value for them.

    adopt: one-time takeover of a server that was never deployed to (no manifest yet), e.g. a
    fresh AMP instance full of generated defaults. Differing files are replaced instead of
    aborting; the masked diff is still printed. Refused once a manifest exists."""
    raw_manifest = remote.read(MANIFEST)
    if adopt and raw_manifest is not None:
        raise McDeployError(
            f"{target.name}: --adopt is only for a server that was never deployed to, but it has a "
            f"deploy manifest ({MANIFEST}). Resolve the drift with the sync workflow instead."
        )
    manifest = {"files": {}}
    if raw_manifest is not None:
        try:
            manifest = json.loads(raw_manifest)
            if not isinstance(manifest.get("files"), dict):
                raise ValueError("missing 'files'")
        except ValueError as exc:
            raise McDeployError(f"{target.name}: server manifest {MANIFEST} is corrupt ({exc}); fix or remove it manually") from None
    drifted: list[str] = []
    diff_lines: list[str] = []
    uploads: dict[str, bytes] = {}
    replaced: dict[str, bytes] = {}  # drifted files, uploaded only with adopt
    keys_for = {rel: owned_keys(target, rel) for rel in rendered}
    owned_before = manifest.get("owned", {})
    incremental = raw_manifest is not None and not adopt and not full
    if incremental:
        candidates = {rel: d for rel, d in rendered.items() if manifest["files"].get(rel) != manifest_hash(d, keys_for[rel])}
        print(f"{target.name}: incremental deploy, {len(candidates)} of {len(rendered)} managed files differ "
              f"from the last deploy ({str(manifest.get('commit', '?'))[:12]}); only those are checked and uploaded")
    else:
        candidates = rendered
        print(f"{target.name}: full check of all {len(rendered)} managed files"
              + (" (first deploy)" if raw_manifest is None else ""))
    remote_files = remote.read_many(list(candidates))
    for rel, data in candidates.items():
        remote_data = remote_files[rel]
        keys = keys_for[rel]
        if remote_data is None:
            if rel in manifest["files"]:
                drifted.append(rel)
                diff_lines.append(f"--- {rel}: deployed earlier but now missing on the server")
            else:
                uploads[rel] = data
            continue
        upload = with_values_from(data, remote_data, keys)
        if same_content(rel, upload, remote_data, keys):
            # Already up to date, possibly in the plugin's own layout (e.g. an earlier deploy stopped
            # before writing the manifest, or the repo was reformatted). The server's file stays.
            continue
        if deployed_unchanged(rel, remote_data, data, manifest["files"].get(rel), manifest.get("content", {}).get(rel),
                              tuple(owned_before.get(rel, ())), keys):
            uploads[rel] = upload
        else:
            drifted.append(rel)
            replaced[rel] = upload
            repo_text = (target.source_dir / rel).read_bytes().decode("utf-8", errors="replace")
            diff_lines += masked_diff(rel, remote_data.decode("utf-8", errors="replace"), repo_text, masker)
    if drifted and adopt:
        OUTPUTS.add_summary(
            f"### {target.name}: adopting the server, {len(drifted)} existing file(s) are replaced\n"
            + "\n".join(f"- `{r}`" for r in drifted)
            + "\n```diff\n" + "\n".join(diff_lines) + "\n```"
        )
        print("\n".join(diff_lines))
        print(f"{target.name}: adopt: replacing {len(drifted)} existing file(s) that differ from the repo")
        return manifest, {**uploads, **replaced}
    if drifted:
        OUTPUTS.add_summary(
            f"### {target.name}: deploy aborted, server files changed since the last deploy\n"
            + "\n".join(f"- `{r}`" for r in drifted)
            + "\n\nPull the server changes into the repo (Minecraft sync workflow or `mc pull`), then redeploy."
            + ("\nNever deployed to before (no manifest): if these are just a fresh server's defaults, run the "
               "workflow manually with `adopt` checked." if raw_manifest is None else "") + "\n"
            + "```diff\n" + "\n".join(diff_lines) + "\n```"
        )
        print("\n".join(diff_lines))
        raise McDeployError(
            f"{target.name}: {len(drifted)} managed file(s) changed on the server since the last deploy "
            f"(diff above, values masked). Nothing was uploaded:\n" + "\n".join(f"  - {r}" for r in drifted)
        )
    return manifest, uploads


def git_commit() -> str:
    result = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise McDeployError(f"git rev-parse failed: {result.stderr.strip()}")
    return result.stdout.strip()


def run_deploy(target: Target, dry_run: bool, remote: RemoteBase | None = None, values: dict[str, str] | None = None,
               adopt: bool = False, full: bool = False) -> None:
    if is_act() and not dry_run and os.environ.get("MC_ACT_ALLOW_DEPLOY") != "true":
        print("Running under act: forcing --dry-run (set var MC_ACT_ALLOW_DEPLOY=true to really deploy).")
        dry_run = True
    if remote is None:
        assert_clean_worktree(target)
    values = deploy_values() if values is None else values
    names = target_placeholders(target)
    resolved = resolve_values(target, names, values, f"target {target.name} (GitHub Environment secrets/vars)")
    for name, value in resolved.items():
        # Knob values ("&e", "750") and the server name are public; masking them would garble the logs.
        if os.environ.get("GITHUB_ACTIONS") and names[name] not in (*KNOB_KINDS, SERVER_KIND):
            print(f"::add-mask::{value}")
    rendered = render_target(target, resolved)  # render errors stop us before connecting
    own_remote = remote is None
    remote = remote or SftpRemote(values)
    if own_remote:
        try:
            verify_instance(target, remote, values)
        except McDeployError:
            remote.close()
            raise
    try:
        masker = Masker(names, resolved, load_mask_scopes(), load_expansions())
        manifest, uploads = check_drift(target, remote, rendered, masker, adopt, full)
        changed = list(uploads)
        print(f"{target.name}: {len(rendered)} managed files, {len(changed)} to upload")
        for rel in changed:
            print(f"  upload {rel}")
        if dry_run:
            OUTPUTS.add_summary(f"### {target.name}: dry run, no drift, {len(changed)} file(s) would be uploaded")
            return
        keys_for = {rel: owned_keys(target, rel) for rel in rendered}
        files = {rel: manifest_hash(data, keys_for[rel]) for rel, data in rendered.items()}
        old_content = manifest.get("content", {})
        content: dict[str, str] = {}
        for rel, data in rendered.items():
            # Parsing is the slow part: reuse the value of files that did not change since the last deploy.
            unchanged = manifest["files"].get(rel) == files[rel] and manifest.get("owned", {}).get(rel, []) == list(keys_for[rel])
            value = old_content.get(rel) if unchanged and rel in old_content else content_hash(rel, data, keys_for[rel])
            if value is not None:
                content[rel] = value
        new_manifest = {
            "deployed_at": datetime.now(timezone.utc).isoformat(),
            "commit": git_commit() if own_remote else "test",
            "files": files,
            # Files hashed without their server-owned keys, and which keys those were.
            "owned": {rel: list(keys) for rel, keys in keys_for.items() if keys},
            # Hash of each parsed file (content_hash), so a plugin re-saving it in its own layout is not drift.
            "content": content,
        }
        if uploads:
            remote.write_atomic(uploads)
        if (uploads or new_manifest["files"] != manifest.get("files")
                or new_manifest["owned"] != manifest.get("owned", {})
                or new_manifest["content"] != old_content):
            remote.write_atomic({MANIFEST: (json.dumps(new_manifest, indent=2, sort_keys=True) + "\n").encode()})
        OUTPUTS.add_summary(
            f"### {target.name}: deployed {len(changed)} file(s)\n" + "\n".join(f"- `{r}`" for r in changed)
            + "\n\nNo restart was triggered; restart the instance in AMP when convenient."
        )
    finally:
        if own_remote:
            remote.close()
