"""Non-interactive end-to-end check of the capture chain.

Spawns Amazon under Frida with the unpinning script, lets its startup traffic
flow for a bit, then exits. mitmdump (started separately) records the flows; the
caller greps its log for decrypted Amazon hosts.
"""
import sys, time, frida

PKG = "com.amazon.mShop.android.shopping"
script_path = sys.argv[1]
run_secs = int(sys.argv[2]) if len(sys.argv) > 2 else 25

dev = frida.get_usb_device(timeout=10)
pid = dev.spawn([PKG])
session = dev.attach(pid)
with open(script_path) as fh:
    script = session.create_script(fh.read())
script.on("message", lambda m, d: print("  [frida]", m.get("payload", m)))
script.load()
dev.resume(pid)
print(f"  spawned {PKG} (pid {pid}); letting it run {run_secs}s")
time.sleep(run_secs)
try:
    session.detach()
except Exception:
    pass
print("  detached")
