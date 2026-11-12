#!/bin/sh
# Usage: mc-client-entrypoint <minecraft version> <player name> <server host> <server port> <WIDTHxHEIGHT>
# Starts the virtual screen, then installs (first run) and launches the game in offline mode,
# joining the given server directly. The container ends when the game ends.
set -eu
version="$1"; player="$2"; host="$3"; port="$4"; size="$5"

Xvfb :99 -screen 0 "${size}x24" -nolisten tcp +extension GLX +render -noreset &
xvfb_pid=$!
i=0
until xdpyinfo >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 50 ] || ! kill -0 "$xvfb_pid" 2>/dev/null; then
    echo "Xvfb did not start" >&2
    exit 1
  fi
  sleep 0.2
done
echo "virtual screen ${size} ready"

width="${size%x*}"; height="${size#*x}"
exec portablemc start "mojang:${version}" \
  --username "$player" \
  --join-server "$host" --join-server-port "$port" \
  --resolution "${width}x${height}" \
  --jvm-arg=-Xmx3G
