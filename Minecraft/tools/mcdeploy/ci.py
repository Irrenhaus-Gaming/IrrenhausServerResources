"""CI helper: which servers a workflow run boot-tests, deploys and collects for the proxy.

The GitHub Environments are the server list (see targets.yml). Approval is whatever protection
rules an environment has; protected environments make their jobs wait for a reviewer.
"""
from __future__ import annotations

import json
import subprocess

from .config import OUTPUTS, REPO_ROOT, Layout, load_layout
from .errors import McDeployError
from .servers import ServerEnvironments, classify, list_environments

# Changes here affect every server.
GLOBAL_PATHS = (
    "Minecraft/targets.yml", "Minecraft/secrets.schema.yml", "Minecraft/secret-scan-allowlist.yml",
    "Minecraft/test/", "Minecraft/tools/", "mc.sh", ".github/workflows/minecraft.yml",
    ".gitattributes", ".gitignore",
)
ZERO_SHA = "0" * 40


def changed_files(base: str, head: str) -> list[str] | None:
    """Files changed between two commits, or None when the range is unknown (treat as 'all')."""
    if not base or base == ZERO_SHA:
        return None
    result = subprocess.run(["git", "-C", str(REPO_ROOT), "diff", "--name-only", base, head],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise McDeployError(f"git diff {base}..{head} failed: {result.stderr.strip()}")
    return [l for l in result.stdout.splitlines() if l]


def source_affected(source: str, files: list[str] | None) -> bool:
    if files is None:
        return True
    return any(f.startswith(f"Minecraft/{source}/") or f.startswith(GLOBAL_PATHS) for f in files)


def plan(event: str, files: list[str] | None, requested: list[str], servers: ServerEnvironments,
         layout: Layout) -> tuple[list[dict], list[dict], bool, list[str], list[str]]:
    """Return (backend jobs, collector jobs, deploy proxy?, expected collected envs, summary lines)."""
    backend_names = [e.name for e in servers.backends]
    lines: list[str] = []
    if requested:
        known = set(backend_names) | ({servers.proxy.name} if servers.proxy else set())
        unknown = [r for r in requested if r not in known]
        if unknown:
            raise McDeployError(f"requested target(s) are not deployable environments: {unknown}")
        backends = [e for e in servers.backends if e.name in requested]
        proxy = servers.proxy is not None and servers.proxy.name in requested
    else:
        backends = servers.backends if source_affected(layout.profiles["backend"]["source"], files) else []
        proxy = source_affected(layout.profiles["proxy"]["source"], files)
        if proxy and servers.proxy is None:
            raise McDeployError(f"the proxy config changed but GitHub Environment '{layout.proxy_environment}' does not exist")

    if event == "pull_request":
        tested = [e for e in backends if not e.protected]
        for e in backends:
            lines.append(f"- `{e.name}`: " + ("boot test (pull request)" if not e.protected
                                              else "protected: tested before its own deploy, not on PRs"))
        if proxy:
            lines.append(f"- `{layout.proxy_environment}`: tested before its deploy, not on PRs "
                         "(it needs entries from every server environment)")
        return [{"environment": e.name, "mode": "test", "collect": False} for e in tested], [], False, [], lines
    if event not in ("push", "workflow_dispatch"):
        raise McDeployError(f"unsupported event {event}")

    backend_jobs = [{"environment": e.name, "mode": "deploy", "collect": proxy} for e in backends]
    for e in backends:
        lines.append(f"- `{e.name}`: boot test + deploy" + (" (waits for approval)" if e.protected else ""))
    collectors: list[dict] = []
    expected: list[str] = []
    if proxy:
        deployed = {e.name for e in backends}
        expected = [e.name for e in servers.listed_in_proxy]
        for e in servers.listed_in_proxy:
            if e.name not in deployed:
                collectors.append({"environment": e.name})
                lines.append(f"- `{e.name}`: collect server entry for the proxy"
                             + (" (waits for approval)" if e.protected else ""))
        lines.append(f"- `{layout.proxy_environment}`: boot test + deploy with {len(expected)} servers"
                     + (" (waits for approval)" if servers.proxy and servers.proxy.protected else ""))
    return backend_jobs, collectors, proxy, expected, lines


def run_plan_targets(event: str, base: str, head: str, requested: str) -> None:
    layout = load_layout()
    servers = classify(list_environments(), layout)
    names = [r.strip() for r in requested.split(",") if r.strip()] if event == "workflow_dispatch" else []
    files = None if event == "workflow_dispatch" else changed_files(base, head)
    backend_jobs, collectors, proxy, expected, lines = plan(event, files, names, servers, layout)
    if not lines:
        lines = ["- nothing to do for this change"]
    OUTPUTS.add_summary("### Minecraft servers for this run\n" + "\n".join(lines))
    OUTPUTS.set_output("backends", json.dumps(backend_jobs, separators=(",", ":")))
    OUTPUTS.set_output("collectors", json.dumps(collectors, separators=(",", ":")))
    OUTPUTS.set_output("proxy", "deploy" if proxy else "none")
    OUTPUTS.set_output("expected_servers", ",".join(expected))
    OUTPUTS.set_output("proxy_environment", layout.proxy_environment)
    print("\n".join(lines))
