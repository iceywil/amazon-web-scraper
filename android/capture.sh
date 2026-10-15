#!/usr/bin/env bash
# Start a capture session: proxy -> mitmweb, Amazon launched with unpinning.
#
# Prereqs (run once per boot): start_emulator.sh, install_ca.sh, start_frida.sh.
# Then:  android/capture.sh
#
#   * mitmweb UI:            http://127.0.0.1:8081
#   * flows are also saved:  android/flows/amazon.mitm
#
# In the UI, drive the app (search, open a product), find the JSON request,
# right-click -> Export -> "Copy as cURL", paste into a file, and feed it to the
# scraper:  python run.py capture <file> --format ndjson
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/env.sh"
# shellcheck disable=SC1091
source "$here/../.venv/bin/activate"

PKG=com.amazon.mShop.android.shopping
PROXY_HOST=10.0.2.2   # the host machine, as seen from inside the emulator
PROXY_PORT=8080
mkdir -p "$here/flows"

cleanup() {
    echo; echo "==> clearing device proxy"
    adb shell settings put global http_proxy :0 >/dev/null 2>&1 || true
    [ -n "${MITM_PID:-}" ] && kill "$MITM_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# frida-server must be up (start_frida.sh); confirm quickly.
frida-ps -U >/dev/null 2>&1 || { echo "error: frida not reachable; run android/start_frida.sh" >&2; exit 1; }

echo "==> starting mitmweb (UI on http://127.0.0.1:8081)"
mitmweb --no-web-open-browser \
    --listen-port "$PROXY_PORT" --web-port 8081 \
    --set confdir=~/.mitmproxy \
    --save-stream-file "$here/flows/amazon.mitm" \
    >"$here/mitmweb.log" 2>&1 &
MITM_PID=$!
sleep 3
kill -0 "$MITM_PID" 2>/dev/null || { echo "error: mitmweb failed to start; see android/mitmweb.log" >&2; exit 1; }

echo "==> pointing the emulator at $PROXY_HOST:$PROXY_PORT"
adb shell settings put global http_proxy "$PROXY_HOST:$PROXY_PORT"

echo "==> launching $PKG under the unpinning script"
echo "    (Ctrl-C here stops capture and clears the proxy)"
# -f spawns fresh so hooks are in place before any TLS handshake.
frida -U -f "$PKG" -l "$here/unpin.js"
