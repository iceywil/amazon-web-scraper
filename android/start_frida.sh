#!/usr/bin/env bash
# Download the matching frida-server, push it to the emulator, and run it.
# The server's version must equal the host frida tools' version.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/env.sh"
# shellcheck disable=SC1091
source "$here/../.venv/bin/activate"

FV="$(frida --version)"
abi="$(adb shell getprop ro.product.cpu.abi | tr -d '\r')"
case "$abi" in
    arm64-v8a) arch=arm64 ;;
    x86_64)    arch=x86_64 ;;
    *) echo "error: unhandled abi $abi" >&2; exit 1 ;;
esac

cache="$here/cache"; mkdir -p "$cache"
bin="$cache/frida-server-$FV-android-$arch"
if [ ! -f "$bin" ]; then
    url="https://github.com/frida/frida/releases/download/$FV/frida-server-$FV-android-$arch.xz"
    echo "==> downloading $(basename "$url")"
    curl -fSL "$url" -o "$bin.xz"
    xz -d -f "$bin.xz"
fi

echo "==> pushing frida-server"
adb root >/dev/null; adb wait-for-device
adb push "$bin" /data/local/tmp/frida-server >/dev/null
adb shell chmod 755 /data/local/tmp/frida-server

echo "==> (re)starting frida-server on the device"
adb shell su 0 pkill -f frida-server 2>/dev/null || true
adb shell su 0 sh -c '/data/local/tmp/frida-server >/dev/null 2>&1 &'
sleep 2
if frida-ps -U >/dev/null 2>&1; then
    echo "    OK: frida-server up ($(frida-ps -U | wc -l | tr -d ' ') processes visible)"
else
    echo "error: frida cannot reach the device" >&2; exit 1
fi
