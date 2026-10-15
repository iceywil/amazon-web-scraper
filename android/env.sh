# Shared environment for the android/ scripts. Source it, don't run it.
#
# Java comes from the JetBrains Runtime bundled inside Android Studio, so no
# separate JDK is installed. The SDK lives where Android Studio expects it, so
# the emulator created here also shows up in Studio's Device Manager.

export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
export ANDROID_HOME="$HOME/Library/Android/sdk"
export ANDROID_SDK_ROOT="$ANDROID_HOME"
export PATH="$JAVA_HOME/bin:$ANDROID_HOME/cmdline-tools/latest/bin:$ANDROID_HOME/emulator:$ANDROID_HOME/platform-tools:$PATH"

# Rootable "Google APIs" image (no Play Store): `adb root` works, which is what
# lets the mitmproxy CA be trusted system-wide and Frida attach later.
API_LEVEL="${API_LEVEL:-35}"
SYSTEM_IMAGE="system-images;android-${API_LEVEL};google_apis;arm64-v8a"
BUILD_TOOLS="${BUILD_TOOLS:-35.0.0}"
AVD_NAME="${AVD_NAME:-amazon_api${API_LEVEL}}"

require_java() {
    if [ ! -x "$JAVA_HOME/bin/java" ]; then
        echo "error: bundled Java not found at $JAVA_HOME" >&2
        echo "       is Android Studio installed in /Applications yet?" >&2
        exit 1
    fi
}
