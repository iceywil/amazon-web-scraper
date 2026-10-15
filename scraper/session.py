"""Mint an anonymous Amazon web session over plain HTTP — no emulator, no browser.

Key finding (verified): the storefront cookies are **not a credential** — they are
the anonymous session Amazon hands any first visitor. And while the *homepage*
sits behind a JS/WAF bot challenge (so a scripted GET of `/` only yields
`ak_bmsc`), the **`/dp` and `/s` pages answer a plain client directly** and set
the full session (`session-id`, `session-id-time`, `sst-main`, `i18n-prefs`,
`lc-main`, `sp-cdn`). So we can mint a usable session with one GET — no Playwright.

Optionally we then pin the delivery location (the session is geolocated from the
egress IP, which skews prices/availability — see README §1). Amazon's "Deliver to"
widget takes two CSRF hops: the `/dp` page embeds an `anti-csrftoken-a2z` to fetch
the location modal, and the modal embeds a second token for
`POST /…/glow/address-change`. Posting a postal code pins the store.

    from scraper.session import mint_session
    sess = mint_session("us", zip_code="10001")   # {marketplace, domain, cookies, ...}
"""
from __future__ import annotations

import logging
import re
from typing import Any

from .amazon_web import MARKETPLACES
from .client import ScraperClient

log = logging.getLogger("scraper.session")

_DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# A widely-available seed product; override per marketplace if it 404s.
DEFAULT_SEED_ASIN = "B083TCZDMT"

# Each store's own currency. The session's `i18n-prefs` is geolocated from the
# egress IP (e.g. INR from an Asian IP on amazon.com), so we force it back.
CURRENCIES = {"us": "USD", "uk": "GBP", "de": "EUR", "fr": "EUR", "it": "EUR",
              "es": "EUR", "ca": "CAD", "jp": "JPY", "in": "INR", "mx": "MXN",
              "br": "BRL", "au": "AUD"}

# Default ZIP for headless sessions, per marketplace (pins US availability).
DEFAULT_ZIPS = {"us": "10001"}

# The page token is HTML-escaped inside the glow modal's data attribute.
_PAGE_TOKEN = re.compile(
    r'anti-csrftoken-a2z(?:&quot;|"|\\")\s*:\s*(?:&quot;|"|\\")([^&"\\]+)')
_MODAL_TOKEN = re.compile(r'CSRF_TOKEN\s*[:=]\s*["\']([^"\']+)')
_DELIVER_TO = re.compile(r'id="glow-ingress-line2"[^>]*>\s*([^<]+)')


class SessionError(RuntimeError):
    """Raised when a session cannot be bootstrapped."""


def _nav_headers(language: str) -> dict[str, str]:
    return {
        "user-agent": _DESKTOP_UA,
        "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "accept-language": language,
        "upgrade-insecure-requests": "1",
        "sec-fetch-mode": "navigate",
        "sec-fetch-dest": "document",
        "sec-fetch-site": "none",
    }


def bootstrap(client: ScraperClient, marketplace: str = "us", *,
              zip_code: str | None = None, seed_asin: str = DEFAULT_SEED_ASIN) -> str:
    """Seed ``client``'s cookie jar with a fresh anonymous session.

    Returns the marketplace base URL. ``zip_code`` (postal code) pins the delivery
    location; ``None`` keeps whatever the egress IP geolocated.
    """
    if marketplace not in MARKETPLACES:
        raise ValueError(f"unknown marketplace {marketplace!r}")
    domain, _mid, language = MARKETPLACES[marketplace]
    base = f"https://{domain}"
    nav = _nav_headers(language)

    page = client.fetch("GET", f"{base}/dp/{seed_asin}", headers=nav)
    if "session-id" not in client.cookies:
        raise SessionError(f"seed page set no session cookie (HTTP {page.status_code})")
    log.info("session seeded (%s): %s", marketplace, sorted(client.cookies.keys()))

    if zip_code:
        try:
            _set_location(client, base, nav, page.text, zip_code)
            log.info("delivery location set to %s", zip_code)
            return base
        except SessionError as exc:
            log.warning("could not set location (%s); keeping geolocated session", exc)
    # No location pinned: at least show prices in the store's own currency.
    currency = CURRENCIES.get(marketplace)
    if currency and client.cookies.get("i18n-prefs") != currency:
        client.cookies.set("i18n-prefs", currency, domain=domain.removeprefix("www"))
        log.info("currency forced to %s", currency)
    return base


def _set_location(client: ScraperClient, base: str, nav: dict, seed_html: str,
                  zip_code: str) -> None:
    m = _PAGE_TOKEN.search(seed_html)
    if not m:
        raise SessionError("no anti-csrftoken-a2z on seed page")
    xhr = {**nav, "accept": "text/html,*/*", "x-requested-with": "XMLHttpRequest"}
    modal = client.fetch(
        "GET", f"{base}/portal-migration/hz/glow/get-rendered-address-selections",
        params={"deviceType": "desktop", "pageType": "Detail",
                "storeContext": "NoStoreName", "actionSource": "desktop-modal"},
        headers={**xhr, "anti-csrftoken-a2z": m.group(1)})
    m = _MODAL_TOKEN.search(modal.text)
    if not m:
        raise SessionError("no CSRF_TOKEN in location modal")
    resp = client.fetch(
        "POST", f"{base}/portal-migration/hz/glow/address-change?actionSource=glow",
        headers={**xhr, "anti-csrftoken-a2z": m.group(1), "content-type": "application/json"},
        content=('{"locationType":"LOCATION_INPUT","zipCode":"%s","deviceType":"web",'
                 '"storeContext":"generic","pageType":"Detail","actionSource":"glow"}'
                 % zip_code))
    try:
        ok = resp.json().get("isAddressUpdated") == 1
    except ValueError:
        ok = False
    if not ok:
        raise SessionError(f"address-change rejected ZIP {zip_code}: {resp.text[:120]}")


def mint_session(marketplace: str = "us", *, zip_code: str | None = None,
                 seed_asin: str = DEFAULT_SEED_ASIN) -> dict[str, Any]:
    """Mint a session and return it as a serialisable dict (cookies included)."""
    client = ScraperClient(requests_per_second=1.0)
    try:
        base = bootstrap(client, marketplace, zip_code=zip_code, seed_asin=seed_asin)
        cookies = dict(client.cookies)
    finally:
        client.close()
    domain = MARKETPLACES[marketplace][0]
    return {"marketplace": marketplace, "domain": domain, "base": base,
            "cookies": cookies, "user_agent": _DESKTOP_UA}


def deliver_to(html: str) -> str | None:
    """The "Deliver to" line of a page, to check which store a session hits."""
    m = _DELIVER_TO.search(html)
    return m.group(1).replace("&zwnj;", "").strip() if m else None
