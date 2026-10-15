#!/usr/bin/env bash
# Trust the mitmproxy CA system-wide on the running emulator.
#
# Android 14+ keeps its CA store in the Conscrypt APEX
# (/apex/com.android.conscrypt/cacerts), which is read-only and per-namespace.
# We overlay a tmpfs copy that includes our CA, then re-create that mount inside
# PID 1's mount namespace so every zygote-spawned app inherits it. User-added
# CAs alone are not enough: since Android 7, apps only trust user CAs if their
# network-security-config opts in, and Amazon's does not.
#
# Idempotent-ish: safe to re-run; re-boot the emulator to get a clean slate.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/env.sh"

cert="$(ls "$here"/certs/*.0 2>/dev/null | head -1)"
[ -n "$cert" ] || { echo "error: no hash-named cert in $here/certs" >&2; exit 1; }
name="$(basename "$cert")"
echo "==> installing $name into the system trust store"

adb root >/dev/null
adb wait-for-device
adb push "$cert" "/data/local/tmp/$name" >/dev/null

# Everything below runs as root on the device.
adb shell su 0 sh -s <<SH
set -e
STORE=/apex/com.android.conscrypt/cacerts
TMP=/data/local/tmp/cacerts-overlay

# Build a tmpfs that mirrors the current store plus our CA.
rm -rf "\$TMP"; mkdir -p "\$TMP"
cp "\$STORE"/* "\$TMP"/ 2>/dev/null || true
cp "/data/local/tmp/$name" "\$TMP/$name"
chmod 644 "\$TMP"/*
chown root:root "\$TMP"/*
chcon u:object_r:system_certificates_file:s0 "\$TMP"/* 2>/dev/null || true

# Mount it over the store in THIS namespace...
mount -t tmpfs tmpfs "\$STORE"
cp "\$TMP"/* "\$STORE"/
chmod 644 "\$STORE"/*
chcon u:object_r:system_certificates_file:s0 "\$STORE"/* 2>/dev/null || true

# ...then replicate the same mount inside init's (PID 1) namespace so already
# running zygote/app processes pick it up without a reboot.
nsenter --mount=/proc/1/ns/mnt -- mount -t tmpfs tmpfs "\$STORE"
nsenter --mount=/proc/1/ns/mnt -- sh -c "cp \$TMP/* \$STORE/ && \
    chmod 644 \$STORE/* && \
    (chcon u:object_r:system_certificates_file:s0 \$STORE/* 2>/dev/null || true)"

echo "    store now holds \$(ls "\$STORE" | wc -l | tr -d ' ') certs"
SH

echo "==> verifying the CA is visible to apps"
subj="$(adb shell su 0 nsenter --mount=/proc/1/ns/mnt -- cat "/apex/com.android.conscrypt/cacerts/$name" 2>/dev/null | openssl x509 -noout -subject 2>/dev/null || true)"
if echo "$subj" | grep -qi mitmproxy; then
    echo "    OK: $subj (visible in init namespace)"
else
    echo "    WARNING: could not confirm the CA; check manually" >&2
fi
