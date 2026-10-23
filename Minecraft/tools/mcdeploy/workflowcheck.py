"""Least-privilege check for the workflows: every job receives exactly the secrets it needs.

Secrets are passed one by one as job-level `env` entries (`NAME: ${{ secrets.NAME }}`), never
as a dump like toJSON(secrets). The needed names come from the schema plus the tooling's own
keys, so the workflow list cannot drift from the schema: lint fails on a missing, extra or
renamed entry, and on any secret referenced outside those job-level env blocks.
"""
from __future__ import annotations

import re

from .config import REPO_ROOT, SchemaEntry, load_layout, load_schema, load_yaml
from .remote import SFTP_KEYS

COLLECT_KEYS = ["MC_SERVER_HOST", "MC_SERVER_PROXY_HOST", "MC_SERVER_MC_PORT", "MC_COLLECT_KEY"]
REF_RE = re.compile(r"\$\{\{\s*(secrets|vars)\.([A-Za-z0-9_]+)\s*\}\}")
DUMP_RE = re.compile(r"toJSON\(\s*(secrets|vars)\s*\)|\b(secrets|vars)\s*\[")


def _placeholders(schema: dict[str, SchemaEntry], source: str) -> dict[str, str]:
    """name -> 'secrets' | 'vars' for the placeholders a source folder needs from GitHub."""
    return {n: ("secrets" if e.kind == "secret" else "vars")
            for n, e in schema.items() if source in e.sources and e.scope != "collected"}


def expected_jobs() -> dict[tuple[str, str], dict[str, str]]:
    """(workflow file, job id) -> {name: 'secrets'|'vars'} that job must receive."""
    layout = load_layout()
    schema = load_schema(layout)
    backend = _placeholders(schema, layout.profiles["backend"]["source"])
    proxy = _placeholders(schema, layout.profiles["proxy"]["source"])
    sftp = {k: "secrets" for k in SFTP_KEYS}
    collect = {k: "secrets" for k in COLLECT_KEYS}
    main, sync = ".github/workflows/minecraft.yml", ".github/workflows/minecraft-sync.yml"
    return {
        (main, "backend"): {**backend, **sftp, **collect},
        (main, "collect"): collect,
        (main, "proxy"): {**proxy, **sftp, "MC_COLLECT_KEY": "secrets"},
        (sync, "sync"): {**backend, **proxy, **sftp},
    }


def _strings(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def check_workflows() -> list[str]:
    problems: list[str] = []
    expected = expected_jobs()
    for wf in sorted({f for f, _ in expected}):
        text = (REPO_ROOT / wf).read_text(encoding="utf-8")
        for m in DUMP_RE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            problems.append(f"{wf}:{line}: '{m.group(0)}' hands out every secret; pass each needed one by name")
        jobs = (load_yaml(REPO_ROOT / wf) or {}).get("jobs") or {}
        for job_id, job in jobs.items():
            want = expected.get((wf, job_id))
            env = job.get("env") or {}
            got: dict[str, str] = {}
            for key, value in env.items():
                m = REF_RE.fullmatch(str(value).strip())
                if m:
                    if m.group(2) != key:
                        problems.append(f"{wf} job {job_id}: env {key} must be ${{{{ {m.group(1)}.{key} }}}} (same name)")
                    got[key] = m.group(1)
            # Secrets anywhere else (steps, with:, if:) would bypass this check.
            for path, value in _strings({k: v for k, v in job.items() if k != "env"}):
                for m in REF_RE.finditer(value):
                    problems.append(f"{wf} job {job_id}{path}: {m.group(0)} must be passed via the job's env instead")
            if want is None:
                for key in got:
                    problems.append(f"{wf} job {job_id}: must not receive secrets/vars ({key})")
                continue
            for name in sorted(set(want) - set(got)):
                problems.append(f"{wf} job {job_id}: missing env {name}: ${{{{ {want[name]}.{name} }}}}")
            for name in sorted(set(got) - set(want)):
                problems.append(f"{wf} job {job_id}: receives {name} but does not need it")
            for name in sorted(set(want) & set(got)):
                if want[name] != got[name]:
                    problems.append(f"{wf} job {job_id}: {name} must come from {want[name]}, not {got[name]}")
        for _, job_id in [k for k in expected if k[0] == wf]:
            if job_id not in jobs:
                problems.append(f"{wf}: job '{job_id}' is missing")
    return problems
