"""Parse Amazon's **web** storefront (the pages the app renders in WebViews).

As the live capture in README §1 showed, the app's shopping screens are
`www.amazon.<tld>` pages authenticated by ordinary **session cookies**, not by the
native AAPI HMAC. They come back as HTML (often with JSON embedded in `<script>`
data-islands), so here we parse the page rather than read a private JSON API.

Build a scraper from either:
  * a captured request (`template=`) — inherits its cookies/headers, or
  * a cookie dict (`cookies=`) — e.g. from `scraper.session.mint_session()`.

and point it at any marketplace (`marketplace="uk"`, `"de"`, `"jp"`, …):

    from scraper.amazon_web import AmazonWebScraper
    with AmazonWebScraper(cookies=cookies, marketplace="uk") as s:
        for p in s.search("coffee", pages=2): ...
        s.product("B0D1VN9691")
        s.reviews("B0D1VN9691", pages=2)
        s.seller("A2L77EE7U53NWQ")

What the HTML gives us, per page (learned from live captures):
  * `/s`      product cards: `div[data-component-type="s-search-result"]`.
  * `/dp`     detail: desktop UA yields stable ids (`#productTitle`, `#corePrice…`).
  * `/product-reviews/<ASIN>`  review blocks: `div[data-hook="review"]`.
  * `/sp?seller=<ID>`          seller name, feedback summary, business info.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from bs4 import BeautifulSoup

from .capture import CapturedRequest
from .client import ScraperClient

_DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Per-country marketplace: code -> (domain, marketplace id, accept-language).
MARKETPLACES: dict[str, tuple[str, str, str]] = {
    "us": ("www.amazon.com",    "ATVPDKIKX0DER",  "en-US,en;q=0.9"),
    "uk": ("www.amazon.co.uk",  "A1F83G8C2ARO7P", "en-GB,en;q=0.9"),
    "de": ("www.amazon.de",     "A1PA6795UKMFR9", "de-DE,de;q=0.9"),
    "fr": ("www.amazon.fr",     "A13V1IB3VIYZZH", "fr-FR,fr;q=0.9"),
    "it": ("www.amazon.it",     "APJ6JRA9NG5V4",  "it-IT,it;q=0.9"),
    "es": ("www.amazon.es",     "A1RKKUPIHCS9HS", "es-ES,es;q=0.9"),
    "ca": ("www.amazon.ca",     "A2EUQ1WTGCTBG2", "en-CA,en;q=0.9"),
    "jp": ("www.amazon.co.jp",  "A1VC38T7YXB528", "ja-JP,ja;q=0.9"),
    "in": ("www.amazon.in",     "A21TJRUUN4KGV",  "en-IN,en;q=0.9"),
    "mx": ("www.amazon.com.mx", "A1AM78C64UM0Y8", "es-MX,es;q=0.9"),
    "br": ("www.amazon.com.br", "A2Q3Y263D00KWC", "pt-BR,pt;q=0.9"),
    "au": ("www.amazon.com.au", "A39IBJ37TRP1C6", "en-AU,en;q=0.9"),
}

# Markers that mean "challenge / robot wall", not data.
_WALL_MARKERS = (
    "Robot Check",
    "Enter the characters you see below",
    "/errors/validateCaptcha",
    "api-services-support@amazon.com",
    "To discuss automated access",
    "bm-verify",                      # Akamai Bot Manager interstitial
    "Type the characters you see in this image",
)


class BotWall(Exception):
    """Raised when a page is a captcha / robot check rather than content."""


class AuthRequired(BotWall):
    """Raised when a page redirects to sign-in (data gated behind a login)."""


# --- small DOM helpers -------------------------------------------------------
def _txt(node) -> str | None:
    return node.get_text(" ", strip=True) if node else None


def _first(soup, *selectors: str, attr: str | None = None) -> str | None:
    """First matching selector's text (or attribute), or None."""
    for sel in selectors:
        el = soup.select_one(sel)
        if el is not None:
            val = el.get(attr) if attr else _txt(el)
            if val:
                return val.strip() if isinstance(val, str) else val
    return None


# A single price token: optional grouped thousands + an optional 2-digit decimal.
_PRICE_TOKEN = re.compile(r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{2})?|\d+[.,]\d{2}|\d+")
_RATING_RE = re.compile(r"([0-9][.,]?[0-9]?)\s+(?:out of 5|von 5|sur 5|su 5|de 5|つ星)")
_COUNT_RE = re.compile(r"([\d.,]+)\s+(?:global\s+)?(?:ratings?|Bewertung|évaluation|valoracion|recensioni|評価)",
                       re.IGNORECASE)


def _to_price(text: str | None) -> float | None:
    """Parse the first price token, handling both 1,234.56 and 1.234,56 forms."""
    if not text:
        return None
    m = _PRICE_TOKEN.search(text)
    if not m:
        return None
    s = m.group()
    if "," in s and "." in s:          # both separators -> last one is the decimal
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("," if dec == "." else ".", "").replace(dec, ".")
    elif "," in s:                      # one separator: decimal only if ",dd" at the end
        s = s.replace(",", ".") if re.search(r",\d{2}$", s) else s.replace(",", "")
    elif s.count(".") > 1 or re.search(r"\.\d{3}$", s):   # "1.234" thousands
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


_CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR", "₹": "INR",
                     "¥": "JPY", "R$": "BRL", "C$": "CAD", "A$": "AUD"}


def _to_currency(text: str | None) -> str | None:
    """Best-effort currency of a price string (symbol or 3-letter code)."""
    if not text:
        return None
    m = re.search(r"\b([A-Z]{3})\b", text)          # explicit code, e.g. "INR 2,893"
    if m:
        return m.group(1)
    for sym, code in _CURRENCY_SYMBOLS.items():
        if sym in text:
            return code
    return None


def _to_rating(text: str | None) -> float | None:
    if not text:
        return None
    m = _RATING_RE.search(text)
    return float(m.group(1).replace(",", ".")) if m else None


def _to_int(text: str | None) -> int | None:
    """Parse a count, including abbreviated forms like '19.6K' or '1.2M'."""
    if not text:
        return None
    m = re.search(r"([\d.,]+)\s*([KkMm])?", text)
    if not m:
        return None
    num, suffix = m.group(1), m.group(2)
    if suffix:                       # abbreviated: keep the decimal, scale it
        return int(float(num.replace(",", "")) * (1_000 if suffix in "Kk" else 1_000_000))
    return int(re.sub(r"[.,]", "", num))


def check_wall(html: str) -> None:
    head = html[:4000]
    for mark in _WALL_MARKERS:
        if mark in head:
            raise BotWall(mark)


def data_islands(html: str | BeautifulSoup) -> Iterator[Any]:
    """Yield JSON objects embedded in the page's `<script>` data-islands.

    Amazon ships a lot of state as inline JSON (ATF payloads, price blocks,
    `data-a-state` nodes). Reading these is steadier than scraping rendered DOM,
    so parsers try them first and fall back to selectors.
    """
    soup = html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "html.parser")
    # 1) <script type="a-state" data-a-state='{"key":"..."}'>{...}</script>
    for sc in soup.find_all("script", attrs={"type": "a-state"}):
        try:
            yield json.loads(sc.string or "")
        except (json.JSONDecodeError, TypeError):
            pass
    # 2) <script type="application/json">{...}</script>
    for sc in soup.find_all("script", attrs={"type": "application/json"}):
        try:
            yield json.loads(sc.string or "")
        except (json.JSONDecodeError, TypeError):
            pass


def _deep_find(obj: Any, *keys: str) -> Any:
    """Return the first value for any of `keys` anywhere in a nested structure."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k in keys and v not in (None, "", []):
                    return v
                stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


# --- parsers -----------------------------------------------------------------
def parse_search(html: str, base: str = "https://www.amazon.com") -> list[dict[str, Any]]:
    """Extract product cards from a `/s` search or `/s?node=` category page."""
    check_wall(html)
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in soup.select('[data-component-type="s-search-result"]'):
        asin = card.get("data-asin")
        if not asin or asin in seen:
            continue
        seen.add(asin)
        # The product link: first anchor pointing at /dp/ or /gp/ (some title
        # anchors are JS stubs with href="#").
        href = None
        for a in card.select("a[href]"):
            h = a.get("href", "")
            if "/dp/" in h or "/gp/product/" in h:
                href = h
                break
        if not href:
            href = f"/dp/{asin}"
        img = card.select_one("img.s-image")
        classes = " ".join(card.get("class", []))
        price_text = _first(card, ".a-price .a-offscreen")
        out.append({
            "asin": asin,
            "title": _txt(card.select_one("h2")) or _first(card, "h2 span"),
            "url": (base + href) if href and href.startswith("/") else href,
            "price": _to_price(price_text),
            "currency": _to_currency(price_text),
            "rating": _to_rating(_first(card, ".a-icon-alt")),
            "reviews": _to_int(_first(card,
                                      'a[href*="customerReviews"] span',
                                      '[data-csa-c-content-id="alf-customer-ratings-count-component"]',
                                      "span.a-size-base.s-underline-text")),
            "image": img.get("src") if img else None,
            "sponsored": "AdHolder" in classes or bool(card.select_one(".s-sponsored-label-text")),
        })
    return out


def _detail_field(soup, *labels: str) -> str | None:
    """Look up a labelled value in the product-details bullets or tables."""
    wanted = {lbl.lower() for lbl in labels}
    # detail-bullets list: "<span>Brand</span> <span>BLACK+DECKER</span>"
    for li in soup.select("#detailBullets_feature_div li, #productDetails_detailBullets_sections1 tr"):
        cells = li.select("span, th, td")
        if len(cells) >= 2 and _txt(cells[0]):
            key = _txt(cells[0]).strip(" :‎‏").lower()
            if key in wanted:
                return _txt(cells[-1])
    # product info table: "<th>Brand</th><td>…</td>"
    for row in soup.select("table tr"):
        th, td = row.select_one("th"), row.select_one("td")
        if th and td and _txt(th).strip().lower() in wanted:
            return _txt(td)
    return None


def parse_product(html: str) -> dict[str, Any]:
    """Extract detail fields from a `/dp/<ASIN>` product page (desktop layout)."""
    check_wall(html)
    soup = BeautifulSoup(html, "html.parser")
    asin = _first(soup, "input#ASIN", attr="value") \
        or _first(soup, "[data-asin]", attr="data-asin")
    rating = _to_rating(_first(soup, "#acrPopover", attr="title")) or _to_rating(html[:200000])
    reviews = _to_int(_first(soup, "#acrCustomerReviewText")) \
        or (_COUNT_RE.search(html) and _to_int(_COUNT_RE.search(html).group()))
    price_text = _first(
        soup,
        "#corePrice_feature_div .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-offscreen",
        "#price_inside_buybox",
        ".a-price .a-offscreen",
    )
    price = _to_price(price_text)
    currency = _to_currency(price_text)
    if price is None:   # data-island fallback
        amt = _deep_find(list(data_islands(soup)), "priceAmount", "displayPrice", "buyingPrice")
        price = _to_price(str(amt)) if amt is not None else None
    img = soup.select_one("#landingImage") or soup.select_one("#imgTagWrapperId img")
    bullets = [_txt(li) for li in soup.select("#feature-bullets li")
               if _txt(li) and "hide" not in " ".join(li.get("class", []))]
    # seller / merchant, when exposed on the detail page
    brand = _first(soup, "#bylineInfo", "#brand") or _detail_field(soup, "Brand", "Manufacturer")
    seller = _first(soup, "#sellerProfileTriggerId", "#merchant-info a")
    seller_link = soup.select_one("#sellerProfileTriggerId")
    seller_id = None
    if seller_link and seller_link.get("href") and "seller=" in seller_link["href"]:
        seller_id = re.search(r"seller=([A-Z0-9]+)", seller_link["href"]).group(1)
    return {
        "asin": asin,
        "title": _first(soup, "#productTitle", "#title", "h1#title", "h1"),
        "brand": brand,
        "price": price,
        "currency": currency,
        "rating": rating,
        "reviews": reviews,
        "availability": _first(soup, "#availability .a-color-success",
                               "#availability .a-color-state", "#availability"),
        "seller": seller,
        "seller_id": seller_id,
        "image": img.get("src") if img else None,
        "bullets": bullets,
    }


_STAR_PCT_RE = re.compile(r"(\d+)\s*percent of reviews have (\d) star", re.IGNORECASE)


def parse_review_summary(html: str) -> dict[str, Any]:
    """Aggregate review data from a `/dp` page (available without login).

    Returns the average rating, total rating count, and the star histogram
    (percent per star). Individual review *text* is login-gated and not here.
    """
    check_wall(html)
    soup = BeautifulSoup(html, "html.parser")
    medley = soup.select_one("#reviewsMedley") or soup
    hist = {int(star): int(pct) for pct, star in _STAR_PCT_RE.findall(str(medley))}
    return {
        "average": _to_rating(_first(medley, '[data-hook="rating-out-of-text"]', ".a-icon-alt")),
        "total_ratings": _to_int(_first(medley, '[data-hook="total-review-count"]')),
        "histogram": {s: hist.get(s) for s in (5, 4, 3, 2, 1)} if hist else None,
    }


def parse_reviews(html: str) -> list[dict[str, Any]]:
    """Extract individual review blocks from a `/product-reviews/<ASIN>` page.

    Full review listing is login-gated: Amazon redirects anonymous requests to
    `/ap/signin`, which this raises as :class:`AuthRequired`. When a logged-in
    session's HTML *does* contain reviews, each one's shown content is captured
    (including the display name on the review); reviewer profiles are not followed.
    """
    if "ap/signin" in html[:3000] or "<title>Amazon Sign" in html[:3000] \
            or "<title>Amazon Sign-In" in html[:3000]:
        raise AuthRequired("/product-reviews redirects to sign-in (login required)")
    check_wall(html)
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, Any]] = []
    for rev in soup.select('div[data-hook="review"]'):
        title_el = rev.select_one('a[data-hook="review-title"], span[data-hook="review-title"]')
        # the title anchor wraps the star icon + a span; last span is the text
        title = None
        if title_el:
            spans = title_el.select("span")
            title = _txt(spans[-1]) if spans else _txt(title_el)
        out.append({
            "id": rev.get("id"),
            "rating": _to_rating(_first(rev, 'i[data-hook="review-star-rating"] .a-icon-alt',
                                        'i[data-hook="cmps-review-star-rating"] .a-icon-alt',
                                        '.a-icon-star .a-icon-alt')),
            "title": title,
            "body": _first(rev, 'span[data-hook="review-body"]'),
            "date": _first(rev, 'span[data-hook="review-date"]'),
            "verified": bool(rev.select_one('span[data-hook="avp-badge"]')),
            "helpful": _first(rev, 'span[data-hook="helpful-vote-statement"]'),
            "variant": _first(rev, 'a[data-hook="format-strip"]'),
            "author": _first(rev, "span.a-profile-name"),   # display name on the review
        })
    return out


def parse_seller(html: str) -> dict[str, Any]:
    """Extract a seller ("company") page `/sp?seller=<ID>`: name, rating, info."""
    check_wall(html)
    soup = BeautifulSoup(html, "html.parser")
    summary = _first(soup, "#seller-info-feedback-summary", "#feedback-summary-card")
    # business info rows ("Business Name", "Business Address", …)
    info: dict[str, str] = {}
    detail = soup.select_one("#page-section-detail-seller-info") or soup
    for row in detail.select(".a-row"):
        spans = row.select("span")
        if len(spans) >= 2:
            k = _txt(spans[0]) or ""
            if k.endswith(":"):
                info[k.rstrip(":").strip()] = _txt(spans[1])
    return {
        "name": _first(soup, "#sellerName", "h1#sellerName", "h1"),
        "rating": _to_rating(_first(soup, "#seller-info-feedback-summary .a-icon-alt",
                                    "i.a-icon-star .a-icon-alt")),
        "rating_text": summary,
        "lifetime_ratings": _to_int(_first(soup, 'a[href*="feedback"] .a-size-base',
                                           "#feedback-summary-rating-count")),
        "about": _first(soup, "#spp-expander-about-seller", "#about-seller"),
        "business_info": info or None,
    }


_RATER_RE = re.compile(r"By\s+(.+?)\s+on\s+(.+?)\.?\s*$")


def parse_seller_feedback(html: str) -> list[dict[str, Any]]:
    """Extract individual feedback entries from a seller `/sp` page.

    Each entry (`div.a-fixed-left-grid-inner` holding a `span.feedback-rater`) has
    a star rating, the comment (`.a-text-quote`), and a "By <name> on <date>" line.
    The name shown on the entry is captured; the rater's profile is not followed.
    """
    check_wall(html)
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, Any]] = []
    for rater in soup.select("span.feedback-rater"):
        entry = rater.find_parent("div", class_="a-fixed-left-grid-inner") or rater.parent
        rater_text = _txt(rater) or ""
        m = _RATER_RE.search(rater_text)
        out.append({
            "rating": _to_rating(_first(entry, ".a-icon-alt")),
            "comment": _first(entry, ".a-text-quote", ".feedback-comments"),
            "rated_by": m.group(1) if m else None,     # display name on the entry
            "date": m.group(2) if m else None,
        })
    return out


# --- scraper -----------------------------------------------------------------
class AmazonWebScraper:
    """Reuse a web session's cookies against the storefront, for any marketplace."""

    def __init__(self, template: CapturedRequest | None = None, *,
                 cookies: dict[str, str] | str | None = None,
                 marketplace: str = "us",
                 client: ScraperClient | None = None) -> None:
        if marketplace not in MARKETPLACES:
            raise ValueError(f"unknown marketplace {marketplace!r}; "
                             f"known: {', '.join(MARKETPLACES)}")
        self.domain, self.marketplace_id, self.language = MARKETPLACES[marketplace]
        self.marketplace = marketplace
        self.base = f"https://{self.domain}"
        self.template = template
        self._cookie_header = self._as_cookie_header(cookies)
        if template is None and self._cookie_header is None:
            raise ValueError("provide either template= or cookies=")
        self.client = client or ScraperClient(requests_per_second=1.0)

    @staticmethod
    def _as_cookie_header(cookies) -> str | None:
        if cookies is None:
            return None
        if isinstance(cookies, str):
            return cookies
        return "; ".join(f"{k}={v}" for k, v in cookies.items())

    @classmethod
    def from_cookies(cls, cookies, marketplace: str = "us", **kw) -> "AmazonWebScraper":
        return cls(cookies=cookies, marketplace=marketplace, **kw)

    @classmethod
    def open(cls, marketplace: str = "us", *, zip_code: str | None = None,
             rps: float = 1.0) -> "AmazonWebScraper":
        """Mint a fresh anonymous session and return a ready-to-use scraper.

        The one-liner entry point — no emulator, no captured cURL, no browser::

            with AmazonWebScraper.open("us", zip_code="10001") as amz:
                for p in amz.search("coffee", pages=1):
                    ...

        ``marketplace`` is a key of :data:`MARKETPLACES`; ``zip_code`` pins the
        delivery location (and currency).
        """
        from .session import mint_session   # lazy: session.py imports this module
        sess = mint_session(marketplace, zip_code=zip_code)
        return cls(cookies=sess["cookies"], marketplace=marketplace,
                   client=ScraperClient(requests_per_second=rps))

    def __enter__(self) -> "AmazonWebScraper":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    def _headers(self, desktop: bool = True) -> dict[str, str]:
        """Browser-like navigation headers carrying the session cookies.

        Starts from the captured template (if any), else from scratch; always
        shapes a top-level document request (not an XHR) so full HTML comes back.
        """
        h = dict(self.template.headers) if self.template else {}
        if self._cookie_header:
            h["Cookie"] = self._cookie_header
        # strip XHR / client-hint / mobile-layout markers
        for k in list(h):
            kl = k.lower()
            if kl.startswith("sec-ch") or kl in (
                    "x-requested-with", "accept", "sec-fetch-mode", "sec-fetch-dest",
                    "sec-fetch-site", "referer", "priority", "viewport-width", "dpr",
                    "device-memory", "downlink", "rtt", "ect"):
                h.pop(k)
        if desktop or "user-agent" not in {k.lower() for k in h}:
            h["user-agent"] = _DESKTOP_UA
        h["accept"] = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,*/*;q=0.8")
        h["accept-language"] = self.language
        h["sec-fetch-mode"] = "navigate"
        h["sec-fetch-dest"] = "document"
        h["sec-fetch-site"] = "none"
        h["upgrade-insecure-requests"] = "1"
        return h

    def _get(self, path: str, params: dict[str, str] | None = None):
        return self.client.fetch("GET", f"{self.base}{path}",
                                 params=params, headers=self._headers())

    # -- collection methods ----------------------------------------------------
    def search(self, keyword: str, *, pages: int = 1, start_page: int = 1
               ) -> Iterator[dict[str, Any]]:
        """Yield product cards for `keyword`, paginating `/s` via `&page=`."""
        for page in range(start_page, start_page + pages):
            resp = self._get("/s", {"k": keyword, "page": str(page), "ref": "nb_sb_noss"})
            rows = parse_search(resp.text, base=self.base)
            if not rows:
                break
            yield from rows

    def browse(self, node: str, *, pages: int = 1, start_page: int = 1
               ) -> Iterator[dict[str, Any]]:
        """Yield product cards for a category browse node, paginating `/s`.

        `fs=true` forces the search-results grid; a bare `?node=` renders a
        merchandised landing page with no parseable cards.
        """
        for page in range(start_page, start_page + pages):
            resp = self._get("/s", {"rh": f"n:{node}", "fs": "true", "page": str(page)})
            rows = parse_search(resp.text, base=self.base)
            if not rows:
                break
            yield from rows

    def product(self, asin: str) -> dict[str, Any]:
        """Fetch and parse one `/dp/<ASIN>` product page."""
        data = parse_product(self._get(f"/dp/{asin}").text)
        if not data.get("asin"):
            data["asin"] = asin
        return data

    def review_summary(self, asin: str) -> dict[str, Any]:
        """Aggregate review data (average, count, star histogram) from `/dp`.

        Works anonymously — unlike full review text, which Amazon gates behind login.
        """
        data = parse_review_summary(self._get(f"/dp/{asin}").text)
        data["asin"] = asin
        return data

    def reviews(self, asin: str, *, pages: int = 1, start_page: int = 1,
                sort: str = "recent") -> Iterator[dict[str, Any]]:
        """Yield individual reviews, paginating `/product-reviews/<ASIN>`.

        Anonymous sessions are redirected to sign-in; this raises
        :class:`AuthRequired`. Use :meth:`review_summary` for anonymous aggregate
        data, or supply a logged-in session (via `--curl`/`--cookies`) for text.
        """
        for page in range(start_page, start_page + pages):
            resp = self._get(f"/product-reviews/{asin}", {
                "pageNumber": str(page), "sortBy": sort, "reviewerType": "all_reviews"})
            if "/ap/signin" in str(resp.url):
                raise AuthRequired("/product-reviews redirects to sign-in (login required); "
                                   "use review_summary() for anonymous aggregate data")
            rows = parse_reviews(resp.text)
            if not rows:
                if page == start_page:
                    # Anonymous sessions get a page with no review text (lazy/gated).
                    raise AuthRequired(
                        "no anonymous review text (login-gated); use review_summary()")
                break
            for r in rows:
                r["asin"] = asin
            yield from rows

    def seller(self, seller_id: str) -> dict[str, Any]:
        """Fetch and parse a seller ("company") page `/sp?seller=<ID>`."""
        data = parse_seller(self._get("/sp", {"seller": seller_id}).text)
        data["seller_id"] = seller_id
        return data

    def seller_feedback(self, seller_id: str, *, pages: int = 1, start_page: int = 1
                        ) -> Iterator[dict[str, Any]]:
        """Yield a seller's feedback entries from the `/sp` page, paginating it."""
        for page in range(start_page, start_page + pages):
            resp = self._get("/sp", {"seller": seller_id, "pageNumber": str(page)})
            rows = parse_seller_feedback(resp.text)
            if not rows:
                break
            for r in rows:
                r["seller_id"] = seller_id
            yield from rows
