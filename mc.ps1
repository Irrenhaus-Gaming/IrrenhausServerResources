# Windows entry point for mcdeploy (Minecraft/tools), run inside its Docker image.
# Usage: .\mc.ps1 <command> [args]     e.g. .\mc.ps1 lint ; .\mc.ps1 test backend-test --jars-from remote
# Local SFTP credentials for fetching jars go into .mc-local.env (git-ignored, KEY=value lines).
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$image = 'irrenhaus-mcdeploy:local'

if ($args.Count -gt 0 -and $args[0] -eq 'install-hooks') {
    git -C $repo config core.hooksPath .githooks
    if ($LASTEXITCODE -ne 0) { throw 'git config failed' }
    Write-Output 'pre-commit hook installed (.githooks/pre-commit)'
    exit 0
}

docker build --quiet --tag $image (Join-Path $repo 'Minecraft/tools') | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'docker build of the tools image failed (is Docker Desktop running?)' }

$envArgs = @()
Get-ChildItem Env: | Where-Object { $_.Name -match '^(MC_|AMP_)' } | ForEach-Object { $envArgs += @('--env', $_.Name) }
# .mc-local.env is read by the tool itself from the mounted repo (never by docker --env-file).

$ttyArgs = @()
if (-not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected) { $ttyArgs = @('--interactive', '--tty') }

docker run --rm @ttyArgs @envArgs `
    --volume /var/run/docker.sock:/var/run/docker.sock `
    --volume "${repo}:/repo" --workdir /repo $image @args
exit $LASTEXITCODE
