"""mitmdump addon: record what happens while *you* sign in / sign up on the emulator.

Loaded by ``scraper.auth.login_in_browser()``. For every Amazon flow it:

* appends one JSON line to ``$LOGIN_LOG`` — method, URL, status, redirect,
  a ``phase`` label (signin page, credential POST, OTP/MFA, captcha, sign-up,
  app device registration, …), the *names* of submitted form fields and of the
  cookies the response sets;
* keeps a running cookie jar for ``.amazon.*`` (Set-Cookie headers, plus the
  ``website_cookies`` the app's device-registration response hands out) and
  rewrites ``$LOGIN_COOKIES`` whenever it changes, with ``signed_in`` true once
  the auth cookies (``at-main``/``sess-at-main``/``x-main``, or ``*-acbuk`` …)
  are present;
* saves the flow to ``$LOGIN_FLOWS`` **after redacting what you typed**
  (password, email/phone, OTP codes, names) from request bodies and queries.

Session tokens and cookies are kept — they are the point of the capture — so the
output files are as sensitive as a logged-in browser profile (``android/flows/``
is git-ignored).
"""
from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from mitmproxy import http
from mitmproxy.io import FlowWriter

LOG_PATH = os.environ.get("LOGIN_LOG", "login.ndjson")
COOKIES_PATH = os.environ.get("LOGIN_COOKIES", "login_cookies.json")
FLOWS_PATH = os.environ.get("LOGIN_FLOWS", "login.mitm")

REDACTED = "<redacted>"
# Field names that carry something the user typed. Matched case-insensitively on
# the full key (form fields, query params, JSON keys at any depth).
_SECRET_KEY = re.compile(
    r"^(password|passwordcheck|passwd|pwd|encryptedpwd|email|user_?id|username|login|phone(number)?|"
    r"customername|name|otp(code)?|code|cvf_?captcha_?input|guess|"
    r"verification_?code|claimvalue|claim)$", re.IGNORECASE)
_AMAZON = re.compile(r"(^|\.)amazon\.[a-z.]+$|(^|\.)a2z\.com$")
# Auth cookies Amazon sets once you are signed in (".com" uses -main, others -acb<cc>).
AUTH_COOKIE = re.compile(r"^(sess-at|at|x)-(main|acb[a-z]{2})$")
# Noise that tells us nothing about the auth flow.
_SKIP_PATH = re.compile(r"\.(png|jpe?g|gif|webp|svg|css|woff2?|ico|js)(\?|$)|/(uedata|batch/1/OE|"
                        r"rd/uedata|1/events)|/gp/csm/", re.IGNORECASE)

# (path regex, phase) — first match wins.
_PHASES = [
    (r"/ap/signin", "signin"),
    (r"/ap/register", "signup"),
    (r"/ap/cvf/", "cvf verify (OTP/captcha)"),
    (r"/ap/mfa", "mfa (2-step)"),
    (r"/ap/challenge", "challenge"),
    (r"/ap/forgotpassword|/ap/fpp", "password reset"),
    (r"/ap/accountfixup|/ap/phone", "account fix-up"),
    (r"/ap/signout|/gp/flex/sign-out", "signout"),
    (r"/auth/register", "app registration (MAP)"),
    (r"/auth/token|/auth/o2/token", "token exchange"),
    (r"/ap/exchangetoken|/auth/cookies", "token -> web cookies"),
    (r"/ap/", "auth portal (other)"),
    (r"/errors/validatecaptcha|/captcha", "captcha"),
]

_jar: dict[str, str] = {}
_writer: FlowWriter | None = None
_fh = None
_t0 = time.time()


def _phase(path: str) -> str:
    for pat, label in _PHASES:
        if re.search(pat, path, re.IGNORECASE):
            return label
    return "page"


def _redact_obj(obj):
    if isinstance(obj, dict):
        return {k: (REDACTED if _SECRET_KEY.match(str(k)) and isinstance(v, (str, int))
                    else _redact_obj(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    return obj


def _redact_pairs(pairs):
    return [(k, REDACTED if _SECRET_KEY.match(k) else v) for k, v in pairs]


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    q = urlencode(_redact_pairs(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit(parts._replace(query=q))


def _form_fields(req: http.Request) -> list[str]:
    ctype = req.headers.get("content-type", "")
    try:
        if "x-www-form-urlencoded" in ctype:
            return [k for k, _ in req.urlencoded_form.items(multi=True)]
        if "json" in ctype and req.content:
            body = json.loads(req.content)
            return sorted(body) if isinstance(body, dict) else []
    except Exception:
        pass
    return []


def _redact_request(req: http.Request) -> None:
    """Strip the values the user typed, in place, before the flow is persisted."""
    req.url = _redact_url(req.url)
    ctype = req.headers.get("content-type", "")
    try:
        if "x-www-form-urlencoded" in ctype:
            req.urlencoded_form = _redact_pairs(req.urlencoded_form.items(multi=True))
        elif "json" in ctype and req.content:
            req.content = json.dumps(_redact_obj(json.loads(req.content))).encode()
    except Exception:
        # Unknown encoding: drop the body rather than risk persisting a password.
        if re.search(r"/ap/|/auth/", req.path):
            req.content = b""


def _map_website_cookies(flow: http.HTTPFlow) -> dict[str, str]:
    """Cookies the app's MAP device registration returns for the web plane."""
    if "/auth/register" not in flow.request.path or not flow.response:
        return {}
    try:
        body = json.loads(flow.response.content or b"{}")
        tokens = body["response"]["success"]["tokens"]
    except Exception:
        return {}
    raw = tokens.get("website_cookies") or []
    # A flat list of {Name, Value, Domain, …}, or {domain: [that list]}.
    lists = raw.values() if isinstance(raw, dict) else [raw]
    out = {}
    for cookies in lists:
        for c in cookies:
            if isinstance(c, dict) and c.get("Name"):
                out[c["Name"]] = str(c.get("Value", "")).strip('"')
    return out


def _write_cookies() -> None:
    signed_in = sum(1 for k in _jar if AUTH_COOKIE.match(k)) >= 2
    tmp = COOKIES_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"signed_in": signed_in, "updated": time.time(), "cookies": _jar}, fh, indent=2)
    os.replace(tmp, COOKIES_PATH)


def load(loader) -> None:  # mitmproxy hook
    global _writer, _fh
    _fh = open(FLOWS_PATH, "wb")
    _writer = FlowWriter(_fh)
    _write_cookies()


def done() -> None:  # mitmproxy hook
    if _fh:
        _fh.close()


def response(flow: http.HTTPFlow) -> None:  # mitmproxy hook
    req, resp = flow.request, flow.response
    if not _AMAZON.search(req.pretty_host):
        return

    set_cookies = []
    for name, (value, attrs) in resp.cookies.items(multi=True):
        set_cookies.append(name)
        domain = (attrs.get("domain") or req.pretty_host).lstrip(".")
        expired = attrs.get("max-age") == "0" or value in ("", '""', "deleted")
        if "amazon." in domain:
            if expired:
                _jar.pop(name, None)
            else:
                _jar[name] = value.strip('"')
    map_cookies = _map_website_cookies(flow)
    _jar.update(map_cookies)
    if set_cookies or map_cookies:
        _write_cookies()

    if not _SKIP_PATH.search(req.path):
        entry = {
            "t": round(time.time() - _t0, 2),
            "phase": _phase(req.path),
            "method": req.method,
            "url": _redact_url(req.pretty_url)[:300],
            "status": resp.status_code,
        }
        if resp.headers.get("location"):
            entry["redirect"] = _redact_url(resp.headers["location"])[:300]
        if req.method != "GET":
            entry["fields"] = _form_fields(req)
        if set_cookies:
            entry["set_cookies"] = set_cookies
        if map_cookies:
            entry["website_cookies"] = sorted(map_cookies)
        if any(AUTH_COOKIE.match(n) for n in set_cookies + list(map_cookies)):
            entry["auth_cookies"] = True
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    _redact_request(req)
    if resp.headers.get("location"):
        resp.headers["location"] = _redact_url(resp.headers["location"])
    if _writer:
        _writer.add(flow)
        _fh.flush()
