#!/usr/bin/env bash
# Hold a capture session open: mitmdump recording + Amazon spawned unpinned.
# Runs in the background; a separate driver issues UI/deep-link actions.
#   android/_capture_session.sh <seconds>
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/env.sh"
# shellcheck disable=SC1091
source "$here/../.venv/bin/activate"
dur="${1:-240}"
mkdir -p "$here/flows"

# Always clear the proxy and stop mitmdump, even on error/interrupt.
cleanup() {
    adb shell settings put global http_proxy :0 >/dev/null 2>&1 || true
    [ -f "$here/flows/mitm.pid" ] && kill "$(cat "$here/flows/mitm.pid")" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# frida-server must be up.
adb shell su 0 sh -c 'pidof frida-server >/dev/null || (/data/local/tmp/frida-server >/dev/null 2>&1 &)'
sleep 1

mitmdump -q --listen-port 8080 --set confdir=~/.mitmproxy \
    --save-stream-file "$here/flows/session.mitm" \
    -s "$here/_flow_log.py" > "$here/flows/session.log" 2>&1 &
echo $! > "$here/flows/mitm.pid"
sleep 4

adb shell settings put global http_proxy 10.0.2.2:8080
echo "proxy up; launching Amazon unpinned (attach) for ${dur}s"
python "$here/_attach.py" "$here/unpin.js" "$dur" 2>&1

echo "session ended; flows in android/flows/session.mitm"
