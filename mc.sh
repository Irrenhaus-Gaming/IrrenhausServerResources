#!/usr/bin/env bash
# Runs mcdeploy (Minecraft/tools) inside its Docker image so local shells, act and GitHub Actions
# execute identical code. Usage: ./mc.sh <command> [args]   (./mc.sh --help for the list)
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
image="irrenhaus-mcdeploy:local"
repo_host="$repo"
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    # Git Bash on Windows: stop MSYS from rewriting container paths, give docker a Windows path.
    export MSYS_NO_PATHCONV=1
    repo_host="$(cd "$repo" && pwd -W)"
    ;;
esac

if [[ "${1:-}" == "install-hooks" ]]; then
  git -C "$repo" config core.hooksPath .githooks
  echo "pre-commit hook installed (.githooks/pre-commit)"
  exit 0
fi

docker build --quiet --tag "$image" "$repo_host/Minecraft/tools" >/dev/null

# Pass through only the variables mcdeploy uses (values never appear on the command line).
env_args=()
while IFS= read -r name; do
  case "$name" in
    MC_*|AMP_*|GITHUB_*|ACT|CI) env_args+=(--env "$name") ;;
  esac
done < <(compgen -e)
if [[ -z "${CI:-}" && -f "$repo/.mc-local.env" ]]; then
  env_args+=(--env-file "$repo/.mc-local.env")
fi

status=0
if [[ -n "${ACT:-}" ]]; then
  # act copies the workspace into its own container, so a host bind mount would point nowhere:
  # copy the repo into the tools container and the results back out instead.
  cid="$(docker create "${env_args[@]}" --volume /var/run/docker.sock:/var/run/docker.sock --workdir /repo "$image" "$@")"
  trap 'docker rm --force "$cid" >/dev/null' EXIT
  docker cp "$repo/." "$cid:/repo"
  docker start --attach "$cid" || status=$?
  rm -rf "$repo/.cache/out"
  mkdir -p "$repo/.cache"
  if docker cp "$cid:/repo/.cache/out" "$repo/.cache/out" 2>/dev/null; then
    echo "copied results out of the tools container"
  fi
else
  tty_args=()
  if [[ -t 0 && -t 1 ]]; then tty_args=(--interactive --tty); fi
  docker run --rm "${tty_args[@]}" "${env_args[@]}" \
    --volume /var/run/docker.sock:/var/run/docker.sock \
    --volume "$repo_host:/repo" --workdir /repo "$image" "$@" || status=$?
fi

# Forward step summary / outputs written by mcdeploy to the GitHub runner files.
if [[ -n "${GITHUB_STEP_SUMMARY:-}" && -f "$repo/.cache/out/summary.md" ]]; then
  cat "$repo/.cache/out/summary.md" >> "$GITHUB_STEP_SUMMARY"
fi
if [[ -n "${GITHUB_OUTPUT:-}" && -f "$repo/.cache/out/github_output" ]]; then
  cat "$repo/.cache/out/github_output" >> "$GITHUB_OUTPUT"
fi
exit "$status"
