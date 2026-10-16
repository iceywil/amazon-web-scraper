"""Use an Amazon session **you authenticated yourself** — for login-gated data.

Some surfaces (notably full review text, `/product-reviews`) redirect anonymous
visitors to `/ap/signin`. To read them you need a logged-in session. This module
does **not** log in for you: it loads the cookies from a session *you* created by
signing in with your own browser, and hands them to :class:`AmazonWebScraper`.

That boundary is deliberate — this library never handles your password, one-time
codes, or Amazon's sign-in challenge. It only reuses a session you already hold.

Two ways to provide it:

1. Export cookies after logging in (a "cookies.txt"/Netscape file from a browser
   extension, or a JSON ``{name: value}`` dump) and::

       from scraper import AmazonWebScraper, cookies_from_file
       cookies = cookies_from_file("amazon_cookies.txt")
       amz = AmazonWebScraper.authenticated(cookies, marketplace="us")

2. Read them straight from your local browser (optional, needs ``browser_cookie3``)::

       from scraper import cookies_from_browser
       cookies = cookies_from_browser("chrome", "amazon.com")

3. Sign in on the emulator while the traffic is recorded (needs the ``android/``
   setup: a booted emulator with the mitmproxy CA installed)::

       from scraper import login_in_browser
       cookies = login_in_browser("us")          # you type the credentials on the device

   This also captures the sign-in / sign-up flow itself (which endpoints, which
   redirects, which cookies appear when) — with what you typed redacted.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable


def cookies_from_file(path: str) -> dict[str, str]:
    """Load cookies from a Netscape ``cookies.txt`` or a JSON export.

    JSON may be ``{name: value}``, a ``{"cookies": {...}}`` session dict, or a
    list of ``{"name":..., "value":...}`` objects (common browser-extension form).
    """
    text = open(path, encoding="utf-8").read().lstrip()
    if text[:1] in "{[":
        data = json.loads(text)
        if isinstance(data, dict):
            return dict(data.get("cookies", data))
        return {c["name"]: c["value"] for c in data if "name" in c and "value" in c}
    # Netscape cookies.txt
    jar = http.cookiejar.MozillaCookieJar()
    jar.load(path, ignore_discard=True, ignore_expires=True)
    return {c.name: c.value for c in jar}


def cookies_from_browser(browser: str = "chrome", domain: str = "amazon.com") -> dict[str, str]:
    """Read cookies for ``domain`` from your local browser (needs browser_cookie3).

    Reads your own browser's cookie store on your own machine; nothing is sent
    anywhere. Install with ``pip install browser_cookie3``.
    """
    try:
        import browser_cookie3
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "browser_cookie3 is not installed — `pip install browser_cookie3`, "
            "or export a cookies.txt and use cookies_from_file()."
        ) from exc
    loader = getattr(browser_cookie3, browser, None)
    if loader is None:
        raise ValueError(f"unknown browser {browser!r}")
    return {c.name: c.value for c in loader(domain_name=domain)}


# Signed-in pages greet by name and expose a sign-out link; signed-out ones say
# "Hello, sign in" / "Hello, Identify yourself".
_SIGNED_OUT = re.compile(r"Hello,\s*(sign in|identify yourself)", re.IGNORECASE)
_SIGN_OUT_LINK = re.compile(r"/gp/(flex/)?sign-?out|nav_youraccount_signout|ref=nav_signout",
                            re.IGNORECASE)


def is_signed_in(html: str) -> bool:
    """Best-effort HTML heuristic (positive-evidence only; defaults to False).

    :meth:`AmazonWebScraper.is_authenticated` applies this to a product page it
    fetches; call it directly when you already have page HTML.
    """
    if "/ap/signin" in html[:3000]:
        return False
    if _SIGN_OUT_LINK.search(html):
        return True
    m = re.search(r"Hello,\s*([^<\n]{1,40})", html)
    return bool(m and not _SIGNED_OUT.search("Hello, " + m.group(1)))


# --------------------------------------------------------------------------- #
# Sign in on the emulator, capturing the flow                                 #
# --------------------------------------------------------------------------- #

_ANDROID = Path(__file__).resolve().parent.parent / "android"
_APP_PKG = "com.amazon.mShop.android.shopping"
_CHROME_PKG = "com.android.chrome"
_EMULATOR_HOST = "10.0.2.2"   # the host machine, as seen from inside the emulator


def _tool(name: str) -> str:
    """Locate adb/mitmdump: PATH, then the venv next to this interpreter, then the SDK."""
    for cand in (shutil.which(name),
                 str(Path(sys.executable).parent / name),
                 str(Path(os.environ.get("ANDROID_HOME", Path.home() / "Library/Android/sdk"))
                     / "platform-tools" / name)):
        if cand and os.access(cand, os.X_OK):
            return cand
    raise RuntimeError(f"{name} not found (activate the venv / run android/setup_emulator.sh)")


def _adb(adb: str, *args: str, check: bool = False) -> str:
    out = subprocess.run([adb, *args], capture_output=True, text=True, timeout=30)
    if check and out.returncode:
        raise RuntimeError(f"adb {' '.join(args)}: {out.stderr.strip() or out.stdout.strip()}")
    return out.stdout.strip()


def _wait_port(port: int, timeout: float = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def _describe(event: dict) -> str:
    line = f"[{event['t']:>6.1f}s] {event['phase']:<28} {event['method']:<4} {event['status']} " \
           f"{event['url'].split('?')[0]}"
    if event.get("redirect"):
        line += f"\n{'':>39}-> {event['redirect'].split('?')[0]}"
    if event.get("fields"):
        line += f"\n{'':>39}fields: {', '.join(event['fields'])}"
    if event.get("set_cookies"):
        line += f"\n{'':>39}set-cookie: {', '.join(event['set_cookies'])}"
    if event.get("auth_cookies"):
        line += "   <- auth cookies"
    return line


def login_in_browser(marketplace: str = "us", *, app: bool = False, timeout: float = 600,
                     out_dir: str | os.PathLike | None = None, proxy_port: int = 8080,
                     verify: bool = True, on_event: Callable[[dict], None] | None = None,
                     ) -> dict[str, str]:
    """Open Amazon's sign-in on the emulator, record the flow, return the session.

    You sign in (or create an account) **yourself**, on the device; this only
    opens the page and watches. Traffic goes through mitmdump with the
    ``android/_login_capture.py`` addon, which writes to ``out_dir``
    (default ``android/flows/``):

    * ``login.ndjson``        — one line per request: phase, URL, status, redirect,
      submitted field *names*, cookies set (see the addon for the phase labels);
    * ``login_cookies.json``  — the resulting cookie jar (load it later with
      :func:`cookies_from_file`);
    * ``login.mitm``          — the full flows, with what you typed redacted.

    Each event is passed to ``on_event`` (default: printed to stderr) as it
    happens. Returns once the auth cookies appear, or when ``timeout`` expires /
    you press Ctrl-C — in those cases it raises :class:`AuthRequired` if no
    signed-in session was captured (the capture files are still written, which
    is what you want when you are only recording a sign-up).

    ``app=False`` opens the sign-in page in the emulator's browser (Chrome when
    installed). ``app=True`` launches the Amazon app with Frida unpinning (needs
    ``android/start_frida.sh``) — tap *Sign in* there; that also records the
    app's own device registration (``/auth/register``), whose response carries
    web cookies too.

    Prereqs: ``android/start_emulator.sh`` and ``android/install_ca.sh``.
    """
    from .amazon_web import MARKETPLACES, AmazonWebScraper, AuthRequired

    if marketplace not in MARKETPLACES:
        raise ValueError(f"unknown marketplace {marketplace!r}")
    host = MARKETPLACES[marketplace][0]
    out = Path(out_dir) if out_dir else _ANDROID / "flows"
    out.mkdir(parents=True, exist_ok=True)
    log_path, cookies_path, flows_path = (out / "login.ndjson", out / "login_cookies.json",
                                          out / "login.mitm")
    for p in (log_path, cookies_path):
        p.unlink(missing_ok=True)
    emit = on_event or (lambda e: print(_describe(e), file=sys.stderr, flush=True))

    adb = _tool("adb")
    if not re.search(r"^emulator-\S+\s+device$", _adb(adb, "devices"), re.MULTILINE):
        raise RuntimeError("no emulator attached — run android/start_emulator.sh "
                           "and android/install_ca.sh first")

    env = {**os.environ, "LOGIN_LOG": str(log_path), "LOGIN_COOKIES": str(cookies_path),
           "LOGIN_FLOWS": str(flows_path)}
    mitm = subprocess.Popen(
        [_tool("mitmdump"), "-q", "--listen-port", str(proxy_port),
         "--set", "confdir=~/.mitmproxy", "-s", str(_ANDROID / "_login_capture.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=open(out / "login_mitm.log", "w"))
    driver = None
    try:
        if not _wait_port(proxy_port):
            raise RuntimeError(f"mitmdump did not start; see {out / 'login_mitm.log'}")
        _adb(adb, "shell", "settings", "put", "global", "http_proxy",
             f"{_EMULATOR_HOST}:{proxy_port}", check=True)

        if app:
            # Reuses the capture driver: launch the app, attach Frida, hold it open.
            driver = subprocess.Popen([sys.executable, str(_ANDROID / "_attach.py"),
                                       str(_ANDROID / "unpin.js"), str(int(timeout) + 30)])
            print("==> Amazon app launched — tap 'Sign in' (or 'Create account') on the device",
                  file=sys.stderr)
        else:
            # Your Account redirects an anonymous visitor straight to /ap/signin
            # (with the right return_to/openid params); "Create account" is one tap away.
            url = f"https://{host}/gp/css/homepage.html"
            cmd = ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url]
            if _CHROME_PKG in _adb(adb, "shell", "pm", "list", "packages", _CHROME_PKG):
                cmd += ["-p", _CHROME_PKG]
            _adb(adb, *cmd, check=True)
            print(f"==> opened {url} on the emulator — sign in or create an account there "
                  f"(Ctrl-C to stop; timeout {int(timeout)}s)", file=sys.stderr)

        seen, signed_in_at = 0, None
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                if log_path.exists():
                    lines = log_path.read_text(encoding="utf-8").splitlines()
                    for line in lines[seen:]:
                        emit(json.loads(line))
                    seen = len(lines)
                if signed_in_at is None and _jar_state(cookies_path)[0]:
                    signed_in_at = time.time()
                    print("==> signed in — capturing the post-login redirects", file=sys.stderr)
                # A short grace period so the landing page after sign-in is recorded too.
                if signed_in_at and time.time() - signed_in_at > 5:
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n==> stopped by user", file=sys.stderr)
    finally:
        _adb(adb, "shell", "settings", "put", "global", "http_proxy", ":0")
        for proc in (driver, mitm):
            if proc and proc.poll() is None:
                proc.send_signal(signal.SIGINT)   # lets mitmdump close login.mitm cleanly
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()

    signed_in, cookies = _jar_state(cookies_path)
    print(f"==> capture: {log_path}, {flows_path}; cookies: {cookies_path} "
          f"({len(cookies)} cookies, signed_in={signed_in})", file=sys.stderr)
    if not signed_in:
        raise AuthRequired(f"no signed-in session was captured (flow recorded in {log_path})")
    if verify:
        with AmazonWebScraper.authenticated(cookies, marketplace):   # raises if not signed in
            pass
    return cookies


def _jar_state(path: Path) -> tuple[bool, dict[str, str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, {}
    return bool(data.get("signed_in")), dict(data.get("cookies", {}))
