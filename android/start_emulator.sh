#!/usr/bin/env bash
# Boot the AVD with a writable system partition, wait for boot, enable root.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/env.sh"

if adb devices | grep -q "^emulator-.*device$"; then
    echo "==> an emulator is already running"
else
    log="$here/emulator.log"
    echo "==> starting $AVD_NAME (log: $log)"
    # -writable-system: needed later to drop the mitmproxy CA into the system store.
    nohup emulator -avd "$AVD_NAME" -writable-system -no-snapshot-load \
        -netdelay none -netspeed full >"$log" 2>&1 &
fi

echo "==> waiting for boot"
adb wait-for-device
until [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; do
    sleep 2
done

adb root >/dev/null
adb wait-for-device
echo "==> booted: Android $(adb shell getprop ro.build.version.release | tr -d '\r')" \
     "(API $(adb shell getprop ro.build.version.sdk | tr -d '\r')), uid $(adb shell id -u | tr -d '\r')"
