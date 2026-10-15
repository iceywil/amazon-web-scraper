#!/usr/bin/env bash
# Verify and install an XAPK bundle (base APK + split APKs) on the running emulator.
#
#   android/install_xapk.sh apk/amazon-shopping-32.18.0.100.xapk
#
# Refuses to install when the file isn't a real zip (e.g. a saved Cloudflare
# challenge page), when the splits aren't all signed by the same certificate,
# or when the device is too old for the app's minSdk.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/env.sh"
require_java

xapk="${1:?usage: $0 <file.xapk>}"
expected_signer="${EXPECTED_SIGNER:-Amazon}"

if ! file -b "$xapk" | grep -qi "zip archive"; then
    echo "error: $xapk is not a zip archive ($(file -b "$xapk" | cut -c1-60))" >&2
    echo "       re-download it in a browser" >&2
    exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
unzip -q "$xapk" -d "$work"

# manifest.json describes the bundle; read it with an isolated interpreter.
read_manifest() {
    python3 -I -c 'import json,sys; m=json.load(open(sys.argv[1])); print(m.get(sys.argv[2], ""))' \
        "$work/manifest.json" "$1"
}
package="$(read_manifest package_name)"
version="$(read_manifest version_name)"
min_sdk="$(read_manifest min_sdk_version)"
echo "==> $package $version (minSdk ${min_sdk:-?})"

device_sdk="$(adb shell getprop ro.build.version.sdk | tr -d '\r')"
if [ -n "$min_sdk" ] && [ "$device_sdk" -lt "$min_sdk" ]; then
    echo "error: device is API $device_sdk, app needs API $min_sdk" >&2
    echo "       re-run setup with API_LEVEL=$min_sdk (or higher)" >&2
    exit 1
fi

# Keep the base + arm64 + language/density splits; drop other ABIs.
apks=()
for f in "$work"/*.apk; do
    case "$(basename "$f")" in
        *armeabi*|*x86*) ;;
        *) apks+=("$f") ;;
    esac
done
[ "${#apks[@]}" -gt 0 ] || { echo "error: no APKs inside $xapk" >&2; exit 1; }

apksigner="$(ls -d "$ANDROID_HOME"/build-tools/*/apksigner | tail -1)"
echo "==> verifying signatures of ${#apks[@]} APK(s)"
first_digest=""
for f in "${apks[@]}"; do
    certs="$("$apksigner" verify --print-certs "$f")" || {
        echo "error: signature check failed for $(basename "$f")" >&2; exit 1; }
    digest="$(echo "$certs" | awk -F': ' '/certificate SHA-256 digest/ {print $2; exit}')"
    if [ -z "$first_digest" ]; then
        first_digest="$digest"
        echo "$certs" | grep -E "certificate DN|SHA-256 digest" | head -2 | sed 's/^/    /'
        if ! echo "$certs" | grep "certificate DN" | grep -qi "$expected_signer"; then
            echo "error: signer DN doesn't mention '$expected_signer'" >&2; exit 1
        fi
    elif [ "$digest" != "$first_digest" ]; then
        echo "error: $(basename "$f") is signed by a different certificate" >&2; exit 1
    fi
done
echo "    all splits share one signer"

echo "==> installing"
adb install-multiple -r "${apks[@]}"

# Some XAPKs ship expansion files alongside the APKs.
if [ -d "$work/Android/obb" ]; then
    echo "==> pushing OBB files"
    adb push "$work/Android/obb/." /sdcard/Android/obb/
fi

echo "==> launching"
adb shell monkey -p "$package" -c android.intent.category.LAUNCHER 1 >/dev/null
