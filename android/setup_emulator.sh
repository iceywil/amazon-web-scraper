#!/usr/bin/env bash
# One-time setup: SDK packages + a rootable arm64 emulator (AVD).
# Safe to re-run; already-installed packages are skipped.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/env.sh"
require_java

# Bootstrap with Homebrew's sdkmanager, then switch to the copy inside the SDK.
bootstrap="$(command -v sdkmanager || echo /opt/homebrew/bin/sdkmanager)"
if [ ! -x "$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager" ]; then
    echo "==> bootstrapping cmdline-tools into $ANDROID_HOME"
    mkdir -p "$ANDROID_HOME"
    { yes || true; } | "$bootstrap" --sdk_root="$ANDROID_HOME" "cmdline-tools;latest"
fi

# Licenses are accepted by answering the install's own prompts; a standalone
# `sdkmanager --licenses` on a fresh SDK can hang indefinitely.
echo "==> installing SDK packages (the system image is ~1.5 GB)"
{ yes || true; } | sdkmanager --install \
    "platform-tools" \
    "emulator" \
    "platforms;android-${API_LEVEL}" \
    "build-tools;${BUILD_TOOLS}" \
    "$SYSTEM_IMAGE"

if avdmanager list avd -c | grep -qx "$AVD_NAME"; then
    echo "==> AVD $AVD_NAME already exists"
else
    echo "==> creating AVD $AVD_NAME"
    echo no | avdmanager create avd -n "$AVD_NAME" -k "$SYSTEM_IMAGE" -d pixel_8
fi

# Room for a ~170 MB app plus its data, a usable keyboard, enough RAM.
config="$HOME/.android/avd/${AVD_NAME}.avd/config.ini"
set_cfg() {
    if grep -q "^$1=" "$config"; then
        sed -i '' "s|^$1=.*|$1=$2|" "$config"
    else
        echo "$1=$2" >> "$config"
    fi
}
set_cfg hw.keyboard yes
set_cfg hw.ramSize 4096
set_cfg disk.dataPartition.size 8G

echo "==> done. boot it with: android/start_emulator.sh"
