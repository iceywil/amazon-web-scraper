"""Launch Amazon normally, then attach Frida and load the unpinning script.

Attach mode is more robust than spawn-gating on this emulator (spawn can time
out when a startup permission dialog interrupts init). TLS happens per request,
so hooks installed a moment after launch still cover the search/product/category
traffic we care about.
"""
import subprocess, sys, time, frida

PKG = "com.amazon.mShop.android.shopping"
script_path = sys.argv[1]
run_secs = int(sys.argv[2]) if len(sys.argv) > 2 else 240


def adb(*args):
    subprocess.run(["adb", *args], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def device_pid():
    """Resolve the app's pid via adb (frida truncates process names to 15 chars)."""
    out = subprocess.run(["adb", "shell", "pidof", PKG],
                         capture_output=True, text=True).stdout.strip()
    return int(out.split()[0]) if out else None


# Launch to the home screen, then let the process settle (Amazon re-forks its
# main process shortly after launch, so the first pid seen can be transient).
adb("shell", "am", "force-stop", PKG)
adb("shell", "monkey", "-p", PKG, "-c", "android.intent.category.LAUNCHER", "1")
time.sleep(6)

dev = frida.get_usb_device(timeout=10)
session = None
last = "no pid"
for attempt in range(20):
    pid = device_pid()
    if pid:
        try:
            session = dev.attach(pid)
            break
        except (frida.ProcessNotFoundError, frida.TransportError) as e:
            last = e
    time.sleep(0.5)
if session is None:
    print(f"  ERROR: could not attach: {last}", flush=True)
    sys.exit(1)
with open(script_path) as fh:
    script = session.create_script(fh.read())
script.on("message", lambda m, d: print("  [frida]", m.get("payload", m), flush=True))
script.load()
print(f"  attached to {PKG} (pid {pid}); holding {run_secs}s", flush=True)
time.sleep(run_secs)
try:
    session.detach()
except Exception:
    pass
print("  detached", flush=True)
