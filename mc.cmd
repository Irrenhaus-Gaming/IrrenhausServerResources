@echo off
rem Windows (cmd) entry point for mcdeploy (Minecraft/tools), run inside its Docker image.
rem Works where PowerShell scripts are disabled. Usage: mc.cmd <command> [args]
rem   e.g. mc.cmd sftp-probe --host-key-only     mc.cmd lint     mc.cmd test mc-backend-Test
rem Secrets for local runs go into .mc-local.env (git-ignored, KEY=value lines).
setlocal EnableExtensions
set "REPO=%~dp0"
set "REPO=%REPO:~0,-1%"
set "IMAGE=irrenhaus-mcdeploy:local"

if /i "%~1"=="install-hooks" (
  git -C "%REPO%" config core.hooksPath .githooks || exit /b 1
  echo pre-commit hook installed ^(.githooks/pre-commit^)
  exit /b 0
)

docker build --quiet --tag %IMAGE% "%REPO%\Minecraft\tools" >nul
if errorlevel 1 (
  echo docker build of the tools image failed ^(is Docker Desktop running?^) 1>&2
  exit /b 1
)

rem Pass through only the variables mcdeploy uses; values never appear on the command line.
set "ENVARGS="
for /f "delims==" %%V in ('set MC_ 2^>nul') do call set "ENVARGS=%%ENVARGS%% --env %%V"
for /f "delims==" %%V in ('set AMP_ 2^>nul') do call set "ENVARGS=%%ENVARGS%% --env %%V"
rem .mc-local.env is read by the tool itself from the mounted repo (never by docker --env-file).

docker run --rm %ENVARGS% --volume /var/run/docker.sock:/var/run/docker.sock --volume "%REPO%:/repo" --workdir /repo %IMAGE% %*
exit /b %ERRORLEVEL%
