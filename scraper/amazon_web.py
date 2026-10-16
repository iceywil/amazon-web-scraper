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
  * `/product-reviews/<ASIN>`  review blocks: `[data-hook="review"]` (an `<li>`).
  * `/sp?seller=<ID>`          seller name, feedback summary, business info.
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
from collections.abc import Callable, Iterator
from typing import Any

from bs4 import BeautifulSoup

from .capture import CapturedRequest
from .client import ScraperClient

log = logging.getLogger(__name__)

_DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# A widely-available product used as a probe page for auth checks.
_SEED_ASIN = "B083TCZDMT"

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


# Amazon image URLs carry a resize directive before the extension
# (".../I/714zIXoRKlL._SY88.jpg"); dropping it yields the original upload.
_IMG_SIZE = re.compile(r"\._[^/]*?(\.\w+)$")


def _full_size(url: str) -> str:
    return _IMG_SIZE.sub(r"\1", url)


def _is_default_avatar(url: str) -> bool:
    # The silhouette is ".../amazon-avatars-global/default.png", or a resized
    # "default._CR0,0,1024,1024_SX48_.png" on some stores.
    return url.rsplit("/", 1)[-1].startswith("default")


def _abs_profile(a, base: str) -> str | None:
    href = a.get("href") if a else None
    if not href:
        return None
    href = re.split(r"[/?]ref=", href)[0]
    return href if href.startswith("http") else base + href


def _avatar(rev) -> str | None:
    """The reviewer's avatar as shown on the card; None for the default silhouette."""
    img = rev.select_one(".a-profile-avatar img")
    src = (img.get("data-src") or img.get("src")) if img else None
    if not src or _is_default_avatar(src) or "grey-pixel" in src:
        return None
    return src


def _review_images(rev) -> list[str]:
    """Full-size URLs of the photos attached to the review (deduplicated, in order)."""
    out: list[str] = []
    for img in rev.select('img[data-hook="review-image-tile"]'):
        src = img.get("data-src") or img.get("src")
        if src and "/images/I/" in src and (url := _full_size(src)) not in out:
            out.append(url)
    return out


def parse_reviews(html: str, base: str = "https://www.amazon.com") -> list[dict[str, Any]]:
    """Extract individual review blocks from a `/product-reviews/<ASIN>` page.

    Full review listing is login-gated: Amazon redirects anonymous requests to
    `/ap/signin`, which this raises as :class:`AuthRequired`. When a logged-in
    session's HTML *does* contain reviews, each one's shown content is captured
    (including the display name, avatar and profile link shown on the review).
    The profile itself is fetched with :meth:`AmazonWebScraper.profile`.
    """
    if "ap/signin" in html[:3000] or "<title>Amazon Sign" in html[:3000] \
            or "<title>Amazon Sign-In" in html[:3000]:
        raise AuthRequired("/product-reviews redirects to sign-in (login required)")
    check_wall(html)
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, Any]] = []
    for rev in soup.select('[data-hook="review"]'):   # <li> now, <div> before
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
            # As shown on the card; the profile page itself is never fetched.
            "author_profile": _abs_profile(rev.select_one("a.a-profile"), base),
            "author_avatar": _avatar(rev),
            "images": _review_images(rev),
        })
    return out


_PROFILE_ID = re.compile(r"amzn1\.account\.[A-Z0-9]+")


def parse_profile(html: str) -> dict[str, Any]:
    """Parse a reviewer's public profile page `/gp/profile/<amzn1.account.…>`.

    Everything comes from the page's ``page-state-profile`` JSON: name, avatar,
    bio, and the reviewer's review timeline (title, text, rating, product,
    attached photos, helpful votes). ``next_page_token`` is non-empty when the
    timeline continues beyond this page.
    """
    check_wall(html)
    soup = BeautifulSoup(html, "html.parser")
    state = None
    for s in soup.select("script[data-a-state]"):
        if "page-state-profile" in (s.get("data-a-state") or "") and s.string:
            state = json.loads(s.string)
            break
    if state is None:
        raise ValueError("no page-state-profile on this page (not a profile, or layout changed)")
    model = state.get("profileModel") or {}
    timeline = state.get("reviewsTimeline") or {}
    reviews = []
    for item in timeline.get("shopItemModels") or []:
        rm = item.get("reviewModel")
        if item.get("itemContentType") != "Review" or not rm:
            continue
        url = rm.get("viewOnAmazon") or ""
        m = re.search(r"/customer-reviews/(R[A-Z0-9]+)", url)
        helpful = re.match(r"\s*([\d,]+)", rm.get("helpfulVoteText") or "")
        reviews.append({
            "review_id": m.group(1) if m else None,
            "rating": rm.get("rating"),
            "title": rm.get("title"),
            "body": rm.get("description"),
            "product_title": rm.get("asinTitle"),
            "product_image": rm.get("productImageUrl"),
            "images": [v["visualElementUrl"] for v in rm.get("visualElements") or []
                       if v.get("visualElementUrl") and not v.get("isVideo")],
            "videos": [v["visualElementUrl"] for v in rm.get("visualElements") or []
                       if v.get("visualElementUrl") and v.get("isVideo")],
            "helpful": int(helpful.group(1).replace(",", "")) if helpful else None,
            "url": url.split("?")[0] or None,
        })
    avatar = model.get("avatarImageURL") or None
    return {
        "profile_id": state.get("entityId"),
        "name": model.get("name"),
        "avatar": None if avatar and _is_default_avatar(avatar) else avatar,
        "bio": model.get("bio") or None,
        "is_influencer": bool(state.get("isInfluencer")),
        "reviews": reviews,
        "next_page_token": timeline.get("nextPageToken") or None,
    }


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
    Only the first page lives in the HTML; :meth:`AmazonWebScraper.seller_feedback`
    follows the rest through `/sp/ajax/feedback`.
    """
    check_wall(html)
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict[str, Any]] = []
    for rater in soup.select("span.feedback-rater"):
        entry = rater.find_parent("div", class_="a-fixed-left-grid-inner") or rater.parent
        rater_text = _txt(rater) or ""
        if "template-" in rater_text:
            continue   # hidden client-side templates (template-rater / template-date)
        m = _RATER_RE.search(rater_text)
        out.append({
            "rating": _to_rating(_first(entry, ".a-icon-alt")),
            "comment": _first(entry, ".a-text-quote", ".feedback-comments"),
            "rated_by": m.group(1) if m else None,     # display name on the entry
            "date": m.group(2) if m else None,
        })
    return out


# --- scraper -----------------------------------------------------------------
# Hard stop for pages=None ("all"), so a page that never empties can't loop forever.
MAX_PAGES = 500


def _paginate(fetch: Callable[[int], "list[dict[str, Any]] | tuple[list[dict[str, Any]], bool]"],
              key: Callable[[dict], Any], *, pages: int | None, start_page: int
              ) -> Iterator[dict[str, Any]]:
    """Yield rows page by page, from ``start_page``.

    ``pages`` caps how many pages are requested; ``None`` means keep going until
    the listing ends (up to :data:`MAX_PAGES`). ``fetch(page)`` returns the rows,
    or ``(rows, more)`` when the page itself says whether another follows.
    Either way it also ends at the first page that is empty or adds no unseen
    row: past the last page Amazon tends to serve the last one again rather than
    an empty page, and some listings (reviews) ignore the page number entirely.
    """
    last = start_page + (pages if pages is not None else MAX_PAGES)
    seen: set = set()
    for page in range(start_page, last):
        got = fetch(page)
        rows, more = got if isinstance(got, tuple) else (got, True)
        fresh = [r for r in rows if key(r) not in seen]
        log.info("page %d: %d rows (%d new)%s", page, len(rows), len(fresh),
                 "" if more else ", last page")
        if not fresh:
            return
        seen.update(key(r) for r in fresh)
        yield from fresh
        if not more:
            return


def _has_next_page(html: str) -> bool:
    """Whether a `/s` results page links to a following page (its Next button)."""
    return "s-pagination-next" in html and "s-pagination-next s-pagination-disabled" not in html


def _feedback_key(r: dict[str, Any]) -> tuple:
    # Feedback entries have no id; the shown fields together identify one.
    return (r.get("rated_by"), r.get("date"), r.get("comment"), r.get("rating"))


def _feedback_from_json(d: dict[str, Any]) -> dict[str, Any]:
    """One entry from `/sp/ajax/feedback` JSON, in :func:`parse_seller_feedback`'s shape."""
    rd = d.get("ratingData") or {}
    text = rd.get("text") or {}
    avatar = d.get("raterAvatarUrl")
    profile = d.get("raterProfileUrl")
    return {
        "rating": float(d["rating"]) if d.get("rating") is not None else None,
        "comment": text.get("expandedText") or text.get("truncatedText"),
        "rated_by": d.get("rater"),
        "date": rd.get("date"),
        "rater_profile": ("https://www.amazon.com" + profile) if profile and profile.startswith("/") else profile,
        "rater_avatar": None if not avatar or _is_default_avatar(avatar) else avatar,
        "suppressed": bool(rd.get("wasSuppressed")),
    }


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
        self.authenticated = False   # set True by authenticated() once verified

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
    def authenticated(cls, cookies, marketplace: str = "us", *, rps: float = 1.0,
                      verify: bool = True, client: "ScraperClient | None" = None
                      ) -> "AmazonWebScraper":
        """Build a scraper from a session **you logged into yourself**.

        Pass cookies you exported from your own browser session (see
        :mod:`scraper.auth`). Unlocks login-gated data such as full review text.
        This library never logs in, handles your password, or solves sign-in
        challenges — it only reuses the authenticated session you provide.

        With ``verify`` (default), it fetches a page and confirms the session
        really is signed in, raising :class:`AuthRequired` if not.
        """
        self = cls(cookies=cookies, marketplace=marketplace,
                   client=client or ScraperClient(requests_per_second=rps))
        if verify and not self.is_authenticated():
            self.close()
            raise AuthRequired("the provided cookies are not a signed-in session "
                               "(log in in your browser, then re-export them)")
        self.authenticated = True
        return self

    def is_authenticated(self) -> bool:
        """Report whether this session is signed in.

        Probe: a product page, whose nav greets a signed-in user by name and
        carries a sign-out link (:func:`scraper.auth.is_signed_in`). Your Orders
        can't be used: it demands a fresh sign-in (``max_auth_age=0``), so even a
        valid session is bounced to ``/ap/signin`` → ``/ax/claim`` once its
        sign-in is a few minutes old.
        """
        from .auth import is_signed_in
        try:
            resp = self._get(f"/dp/{_SEED_ASIN}")
        except BotWall:
            return False
        return is_signed_in(resp.text)

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
    def search(self, keyword: str, *, pages: int | None = 1, start_page: int = 1
               ) -> Iterator[dict[str, Any]]:
        """Yield product cards for `keyword`, paginating `/s` via `&page=`.

        ``pages=None`` walks every page Amazon serves for the query.
        """
        return _paginate(
            lambda page: self._search_page({"k": keyword, "page": str(page), "ref": "nb_sb_noss"}),
            key=lambda r: r["asin"], pages=pages, start_page=start_page)

    def browse(self, node: str, *, pages: int | None = 1, start_page: int = 1
               ) -> Iterator[dict[str, Any]]:
        """Yield product cards for a category browse node, paginating `/s`.

        `fs=true` forces the search-results grid; a bare `?node=` renders a
        merchandised landing page with no parseable cards. ``pages=None`` walks
        every page.
        """
        return _paginate(
            lambda page: self._search_page({"rh": f"n:{node}", "fs": "true", "page": str(page)}),
            key=lambda r: r["asin"], pages=pages, start_page=start_page)

    def _search_page(self, params: dict[str, str]) -> tuple[list[dict[str, Any]], bool]:
        html = self._get("/s", params).text
        return parse_search(html, base=self.base), _has_next_page(html)

    def product(self, asin: str) -> dict[str, Any]:
        """Fetch and parse one `/dp/<ASIN>` product page."""
        data = parse_product(self._get(f"/dp/{asin}").text)
        if not data.get("asin"):
            data["asin"] = asin
        return data

    def top_reviews(self, asin: str) -> list[dict[str, Any]]:
        """The reviews shown on the product page itself (`/dp/<ASIN>`).

        On some stores (amazon.fr: 13 cards) these are served anonymously, while
        the full `/product-reviews` list needs sign-in; on amazon.com the
        anonymous product page shows none.
        """
        rows = parse_reviews(self._get(f"/dp/{asin}").text, self.base)
        for r in rows:
            r["asin"] = asin
        return rows

    def review_summary(self, asin: str) -> dict[str, Any]:
        """Aggregate review data (average, count, star histogram) from `/dp`.

        Works anonymously — unlike full review text, which Amazon gates behind login.
        """
        data = parse_review_summary(self._get(f"/dp/{asin}").text)
        data["asin"] = asin
        return data

    def reviews(self, asin: str, *, pages: int | None = 1, start_page: int = 1,
                sort: str = "recent") -> Iterator[dict[str, Any]]:
        """Yield individual reviews, paginating `/product-reviews/<ASIN>`.

        Anonymous sessions are redirected to sign-in; this raises
        :class:`AuthRequired`. Use :meth:`review_summary` for anonymous aggregate
        data, or supply a logged-in session (via `--curl`/`--cookies`) for text.
        ``pages=None`` walks every page. Amazon currently ignores the page number
        and serves the same featured reviews each time, so this stops after the
        first page (README, Finding 9).
        """
        def fetch(page: int) -> list[dict[str, Any]]:
            resp = self._get(f"/product-reviews/{asin}", {
                "pageNumber": str(page), "sortBy": sort, "reviewerType": "all_reviews"})
            if "/ap/signin" in str(resp.url):
                raise AuthRequired("/product-reviews redirects to sign-in (login required); "
                                   "use review_summary() for anonymous aggregate data")
            rows = parse_reviews(resp.text, self.base)
            if not rows and page == start_page and not self.authenticated:
                # Anonymous sessions get a page with no review text (lazy/gated).
                raise AuthRequired(
                    "no anonymous review text (login-gated); use review_summary() "
                    "or AmazonWebScraper.authenticated(cookies=…)")
            for r in rows:
                r["asin"] = asin
            return rows

        return _paginate(fetch, key=lambda r: r["id"], pages=pages, start_page=start_page)

    def profile(self, profile: str) -> dict[str, Any]:
        """Fetch and parse a reviewer's public profile.

        ``profile`` is an ``amzn1.account.…`` id or a profile URL, e.g. the
        ``author_profile`` of a row from :meth:`reviews`.
        """
        m = _PROFILE_ID.search(profile)
        if not m:
            raise ValueError(f"not a profile id or URL: {profile!r}")
        resp = self._get(f"/gp/profile/{m.group(0)}")
        if "/ap/signin" in str(resp.url):
            raise AuthRequired("/gp/profile redirects to sign-in (login required)")
        return parse_profile(resp.text)

    def seller(self, seller_id: str) -> dict[str, Any]:
        """Fetch and parse a seller ("company") page `/sp?seller=<ID>`."""
        data = parse_seller(self._get("/sp", {"seller": seller_id}).text)
        data["seller_id"] = seller_id
        return data

    def seller_feedback(self, seller_id: str, *, pages: int | None = 1, start_page: int = 1
                        ) -> Iterator[dict[str, Any]]:
        """Yield a seller's feedback entries, following the feedback cursor.

        Page 1 is the `/sp` page itself. Its *Next* link carries a keyset cursor
        (``lastEvaluatedKey``); each following page is one
        ``GET /sp/ajax/feedback?…&lastEvaluatedKey=…`` returning JSON with the
        entries, ``hasNextPage`` and the next cursor (`pageNumber` is ignored).
        A cursor can't jump ahead, so ``start_page`` > 1 walks and skips.
        ``pages=None`` follows the cursor to the end (up to :data:`MAX_PAGES`).
        """
        html = self._get("/sp", {"seller": seller_id}).text
        rows = parse_seller_feedback(html)
        m = re.search(r'data-spp-feedback-link-next-page="([^"]+)"', html)
        params = json.loads(_html.unescape(m.group(1))) if m else None
        last = start_page + (pages if pages is not None else MAX_PAGES)
        seen: set = set()
        page = 1
        while True:
            fresh = [r for r in rows if _feedback_key(r) not in seen]
            seen.update(_feedback_key(r) for r in fresh)
            log.info("feedback page %d: %d entries (%d new)", page, len(rows), len(fresh))
            if page >= start_page:
                for r in fresh:
                    r["seller_id"] = seller_id
                yield from fresh
            page += 1
            if not fresh or not params or page >= last:
                return
            data = self.client.fetch("GET", f"{self.base}{params.get('url', '/sp/ajax/feedback')}",
                                     params={"seller": seller_id,
                                             "marketplaceID": params.get("marketplaceID", self.marketplace_id),
                                             "pageElement": "feedback-next", "pageTouchTracking": "true",
                                             "lastEvaluatedKey": params["lastEvaluatedKey"]},
                                     headers=self._xhr_headers(f"/sp?seller={seller_id}")).json()
            rows = [_feedback_from_json(d) for d in data.get("details") or []]
            cursor = data.get("lastEvaluatedKey") or (data.get("actionParams") or {}).get("lastEvaluatedKey")
            params = dict(params, lastEvaluatedKey=cursor) if data.get("hasNextPage") and cursor else None
            if not params:
                # Last batch: yield it, then stop.
                fresh = [r for r in rows if _feedback_key(r) not in seen]
                log.info("feedback page %d: %d entries (%d new), last page", page, len(rows), len(fresh))
                if page >= start_page:
                    for r in fresh:
                        r["seller_id"] = seller_id
                    yield from fresh
                return

    def _xhr_headers(self, referer_path: str) -> dict[str, str]:
        """Headers for the storefront's own XHR calls (JSON/fragments, not documents)."""
        h = self._headers()
        h.pop("upgrade-insecure-requests", None)
        h.update({"accept": "application/json, text/html, */*", "x-requested-with": "XMLHttpRequest",
                  "referer": f"{self.base}{referer_path}", "sec-fetch-mode": "cors",
                  "sec-fetch-dest": "empty", "sec-fetch-site": "same-origin"})
        return h
