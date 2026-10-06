#!/bin/sh
# Stop this checkout's local multi_app or console: `stop-local.sh run|webui`.
#
# Only processes whose working directory is this checkout are stopped, so a
# second clone's agents (or any other multi_app.py / webui.py) keep running.
# The script takes "run" / "webui" rather than the file name, because its own
# command line would otherwise match the pattern it searches for.
set -u

case "${1:-}" in
  run) script=multi_app.py; label=multi_app ;;
  webui) script=webui.py; label=webui ;;
  *) echo "usage: $0 run|webui" >&2; exit 2 ;;
esac

here=$(cd "$(dirname "$0")/.." && pwd -P)

cwd_of() {
  if [ -r "/proc/$1/cwd" ]; then
    readlink "/proc/$1/cwd"
  else
    lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p'
  fi
}

mine() {
  for pid in $(pgrep -f "${script%.py}\\.py\$" 2>/dev/null); do
    [ "$pid" = "$$" ] && continue
    [ "$(cwd_of "$pid")" = "$here" ] && echo "$pid"
  done
}

pids=$(mine)
if [ -z "$pids" ]; then
  echo "$label was not running"
  exit 0
fi
# SIGTERM: multi_app cancels running turns and reaps their codex / claude
# children before it exits; the console shuts down gracefully.
kill $pids 2>/dev/null
i=0
while [ $i -lt 20 ] && [ -n "$(mine)" ]; do
  sleep 0.5
  i=$((i + 1))
done
if [ -n "$(mine)" ]; then
  echo "$label is still shutting down (pid $(mine | tr '\n' ' '))"
else
  echo "stopped $label"
fi
