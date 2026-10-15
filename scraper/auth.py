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
"""
from __future__ import annotations

import http.cookiejar
import json
import re


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

    The authoritative check is :meth:`AmazonWebScraper.is_authenticated`, which
    probes an auth-gated page; use this only when all you have is page HTML.
    """
    if "/ap/signin" in html[:3000]:
        return False
    if _SIGN_OUT_LINK.search(html):
        return True
    m = re.search(r"Hello,\s*([^<\n]{1,40})", html)
    return bool(m and not _SIGNED_OUT.search("Hello, " + m.group(1)))
