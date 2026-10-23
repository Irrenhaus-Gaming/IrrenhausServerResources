"""Command line entry point: python -m mcdeploy <command> (normally via mc.ps1 / mc.sh)."""
from __future__ import annotations

import argparse
import fnmatch
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path

from . import boottest, ci, convert, deploy, lint, pull, servers
from .config import OUT_DIR, OUTPUTS, REPO_ROOT, deploy_values, get_target, git_files, load_test_values, require_values
from .errors import McDeployError
from .remote import SFTP_KEYS, SftpRemote, fetch_host_key, fingerprint


def cmd_gitleaks(args) -> None:
    common = ["--config", str(REPO_ROOT / ".gitleaks.toml"), "--redact", "--no-banner", "--exit-code", "1"]
    if args.history or args.staged:
        mode = ["git", "--log-opts=--all"] if args.history else ["git", "--pre-commit", "--staged"]
        result = subprocess.run(["gitleaks", *mode, *common, "."], cwd=REPO_ROOT, check=False)
    else:
        # `gitleaks dir` ignores .gitignore; scan exactly the files git would commit instead.
        with tempfile.TemporaryDirectory(prefix="mcd-leaks-") as tmp:
            files = git_files(["."])
            for rel in files:
                dest = Path(tmp) / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(REPO_ROOT / rel, dest)
            print(f"gitleaks: scanning {len(files)} committable files")
            result = subprocess.run(["gitleaks", "dir", *common, tmp], check=False)
    if result.returncode != 0:
        raise McDeployError("gitleaks reported possible secrets (see above, values redacted)")


def cmd_render(args) -> None:
    target = get_target(args.target)
    out = Path(args.out).resolve()
    if out == REPO_ROOT or REPO_ROOT in out.parents:
        raise McDeployError("render output must be outside the repository")
    if args.test_values:
        values = load_test_values()
    else:
        values = deploy_values()
    names = deploy.target_placeholders(target)
    rendered = deploy.render_target(target, require_values(sorted(names), values, target.name))
    if out.exists():
        shutil.rmtree(out)
    for rel, data in rendered.items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_bytes(data)
    print(f"rendered {len(rendered)} files to {out}")


def cmd_probe(args) -> None:
    values = deploy_values()
    if args.host_key_only:
        v = require_values(["MC_SERVER_HOST", "MC_SERVER_SFTP_PORT"], values, "reading the host key")
        key = fetch_host_key(v["MC_SERVER_HOST"], int(v["MC_SERVER_SFTP_PORT"]))
        print("Server host key (verify it, then store it as AMP_SFTP_HOST_KEY):")
        print(f"  {key.get_name()} {key.get_base64()}")
        print(f"  fingerprint {fingerprint(key)}")
        return
    require_values(SFTP_KEYS, values, "the SFTP probe")
    remote = SftpRemote(values)
    try:
        print(f"Connected. Login folder (server root): {remote.sftp.normalize('.')}")
        print("Directories:", ", ".join(remote.list_dirs("")) or "(none)")
        print("Files:      ", ", ".join(remote.list_files("")) or "(none)")
        plugins = remote.list_files("plugins")
        print(f"plugins/: {len(plugins)} files, {sum(f.endswith('.jar') for f in plugins)} jars")
        if args.target:
            target = get_target(args.target)
            matches = [f for f in remote.list_files("") if fnmatch.fnmatch(f, target.server_jar)]
            print(f"server_jar '{target.server_jar}' matches: {matches}")
        if args.write_test:
            probe = ".mcdeploy-probe"
            remote.write_atomic({probe: b"first\n"})
            remote.write_atomic({probe: b"second\n"})
            if remote.read(probe) != b"second\n":
                raise McDeployError("write test: replacing an existing file did not work")
            remote.sftp.remove(remote._abs(probe))
            print("Write test passed: upload + atomic replace work on this server.")
    finally:
        remote.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mc", description="Irrenhaus Minecraft config tooling")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("lint", help="validate configs, placeholders, schema and test values (no secrets needed)")
    p = sub.add_parser("gitleaks", help="run gitleaks over the working tree")
    p.add_argument("--history", action="store_true", help="scan the full git history instead")
    p.add_argument("--staged", action="store_true", help="scan staged changes (pre-commit)")
    p = sub.add_parser("test", help="boot test a target in Docker")
    p.add_argument("target")
    p.add_argument("--jars-from", choices=["remote", "cache"], default="cache",
                   help="remote: download jars from the server first (needs AMP_SFTP_*); cache: reuse .cache/jars")
    p = sub.add_parser("fetch-jars", help="download server + plugin jars of a target into .cache/jars")
    p.add_argument("target")
    p = sub.add_parser("render", help="render a target into a directory outside the repo (debugging)")
    p.add_argument("target")
    p.add_argument("--out", required=True)
    p.add_argument("--test-values", action="store_true", help="use test-values.env instead of real values")
    p = sub.add_parser("deploy", help="drift-check and upload a target")
    p.add_argument("target")
    p.add_argument("--dry-run", action="store_true", help="only check drift and list the files that would change")
    p = sub.add_parser("drift", help="alias for deploy --dry-run")
    p.add_argument("target")
    p = sub.add_parser("pull", help="copy changed server files into the repo, secrets masked")
    p.add_argument("target")
    p.add_argument("--add", nargs="*", default=[], help="extra server paths (relative to the instance root) to import")
    p = sub.add_parser("sftp-probe", help="check SFTP access to a target")
    p.add_argument("--target")
    p.add_argument("--host-key-only", action="store_true", help="print the server host key without logging in")
    p.add_argument("--write-test", action="store_true", help="also test upload + atomic replace with a probe file")
    p = sub.add_parser("plan-targets", help="(CI) choose targets for this workflow run")
    p.add_argument("--event", required=True)
    p.add_argument("--base", default="")
    p.add_argument("--head", default="HEAD")
    p.add_argument("--targets", default="", help="workflow_dispatch: comma-separated environment names")
    p = sub.add_parser("target-info", help="(CI) check that an environment name is a deployable server")
    p.add_argument("target")
    p = sub.add_parser("collect-server", help="(CI) write this server environment's encrypted Velocity entry")
    p.add_argument("environment")
    sub.add_parser("gen-collect-key", help="print a new MC_COLLECT_KEY for the repository secret")
    p = sub.add_parser("convert-scan", help="(one-time) find secrets and write a review plan outside the repo")
    p.add_argument("--out", required=True)
    p = sub.add_parser("convert-apply", help="(one-time) mask the reviewed plan")
    p.add_argument("--plan", required=True)
    p.add_argument("--values", required=True)
    p = sub.add_parser("unit-tests", help="run the mcdeploy unit tests")

    args = parser.parse_args(argv)
    # Results of this run only: mc.sh forwards this folder to the GitHub step summary / outputs.
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    try:
        if args.command == "lint":
            lint.run_lint()
        elif args.command == "gitleaks":
            cmd_gitleaks(args)
        elif args.command == "test":
            boottest.run_boot_test(get_target(args.target), args.jars_from)
        elif args.command == "fetch-jars":
            target = get_target(args.target)
            remote = SftpRemote(deploy_values())
            try:
                boottest.fetch_jars(target, remote)
            finally:
                remote.close()
        elif args.command == "render":
            cmd_render(args)
        elif args.command == "deploy":
            deploy.run_deploy(get_target(args.target), args.dry_run)
        elif args.command == "drift":
            deploy.run_deploy(get_target(args.target), True)
        elif args.command == "pull":
            pull.run_pull(get_target(args.target), args.add)
        elif args.command == "sftp-probe":
            cmd_probe(args)
        elif args.command == "plan-targets":
            ci.run_plan_targets(args.event, args.base, args.head, args.targets)
        elif args.command == "target-info":
            target = get_target(args.target)
            OUTPUTS.set_output("environment", target.environment)
            print(f"{target.name}: deployable ({target.profile}, Minecraft/{target.source})")
        elif args.command == "collect-server":
            servers.collect_server(args.environment, deploy_values())
        elif args.command == "gen-collect-key":
            print(servers.generate_key())
        elif args.command == "convert-scan":
            convert.run_convert_scan(Path(args.out))
        elif args.command == "convert-apply":
            convert.run_convert_apply(Path(args.plan), Path(args.values))
        elif args.command == "unit-tests":
            import pytest
            return pytest.main(["-q", str(Path(__file__).resolve().parent.parent / "tests")])
    except McDeployError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        with (OUT_DIR / "summary.md").open("a", encoding="utf-8") as fh:
            fh.write(f"### ❌ `mc {args.command}` failed\n```\n{exc}\n```\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
