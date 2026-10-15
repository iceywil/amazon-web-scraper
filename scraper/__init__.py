"""amazon_web_scraper — a small, honest library for Amazon's public web storefront.

The Amazon shopping app renders its screens as `www.amazon.<tld>` web pages behind
an anonymous session cookie (see the project README for how that was discovered).
This library mints that session over plain HTTP — no emulator, no browser — and
parses the pages into clean records, for any marketplace.

Quick start
-----------
    from scraper import AmazonWebScraper

    with AmazonWebScraper.open("us", zip_code="10001") as amz:
        results = list(amz.search("coffee grinder", pages=1))
        product = amz.product(results[0]["asin"])
        rating  = amz.review_summary(results[0]["asin"])

`open()` mints the session for you. To reuse an existing session instead, build
from cookies (``AmazonWebScraper(cookies=...)``) or a captured request
(``AmazonWebScraper(template=load_curl_file(...))``).

Collection methods (all iterators paginate lazily):
    search(keyword)            product cards for a query            -> dict rows
    browse(node)               product cards for a category node    -> dict rows
    product(asin)              one product's detail                 -> dict
    review_summary(asin)       average + total + star histogram     -> dict
    reviews(asin)              individual review text (login-gated)  -> dict rows
    seller(seller_id)          a seller ("company") page            -> dict
    seller_feedback(seller_id) a seller's feedback entries          -> dict rows

Lower-level pieces (`parse_*`, `ScraperClient`, `mint_session`, `bootstrap`) are
exported too, so you can parse your own saved HTML or drive the session yourself.
"""

from .amazon_web import (
    MARKETPLACES,
    AmazonWebScraper,
    AuthRequired,
    BotWall,
    parse_product,
    parse_review_summary,
    parse_reviews,
    parse_search,
    parse_seller,
    parse_seller_feedback,
)
from .capture import CapturedRequest, load_curl_file
from .client import RateLimiter, ScraperClient
from .metrics import Metrics
from .session import SessionError, bootstrap, mint_session

__version__ = "0.1.0"

__all__ = [
    # high level
    "AmazonWebScraper",
    "mint_session",
    "bootstrap",
    "MARKETPLACES",
    # parsers (operate on raw HTML you already have)
    "parse_search",
    "parse_product",
    "parse_review_summary",
    "parse_reviews",
    "parse_seller",
    "parse_seller_feedback",
    # transport & capture
    "ScraperClient",
    "RateLimiter",
    "Metrics",
    "CapturedRequest",
    "load_curl_file",
    # exceptions
    "BotWall",
    "AuthRequired",
    "SessionError",
    "__version__",
]
