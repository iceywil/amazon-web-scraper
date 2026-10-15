#!/usr/bin/env python3
"""Demo of the amazon_web_scraper library — readable summaries, not raw dumps.

Run it:  python example.py           (defaults to the US marketplace)
         python example.py uk        (any key of scraper.MARKETPLACES)

It mints an anonymous session (no emulator / browser), calls each collection
method once, and prints a compact résumé of what came back: how many objects,
and a sample of the first object's fields.
"""
from __future__ import annotations

import sys
import textwrap

from scraper import AmazonWebScraper, AuthRequired, MARKETPLACES


# --- pretty-printing helpers -------------------------------------------------
def _fmt(value) -> str:
    """One-line, length-capped repr of a field value."""
    if isinstance(value, str):
        v = " ".join(value.split())
        return f'"{textwrap.shorten(v, 72)}"'
    if isinstance(value, list):
        return f"[{len(value)} items]" + (f" e.g. {_fmt(value[0])}" if value else "")
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}={value[k]}" for k in list(value)[:6]) + "}"
    return str(value)


def _fields(obj: dict) -> None:
    width = max(len(k) for k in obj)
    for k, v in obj.items():
        print(f"    {k.ljust(width)} : {_fmt(v)}")


def show_list(label: str, rows: list[dict]) -> None:
    print(f"\n━━ {label}")
    print(f"  returned : {len(rows)} objects")
    if rows:
        print("  first object:")
        _fields(rows[0])


def show_obj(label: str, obj: dict) -> None:
    print(f"\n━━ {label}")
    print(f"  returned : 1 object, {len(obj)} fields")
    _fields(obj)


def section(title: str) -> None:
    print("\n" + "═" * 70 + f"\n{title}\n" + "═" * 70)


# --- the demo ----------------------------------------------------------------
def main(marketplace: str = "us") -> int:
    if marketplace not in MARKETPLACES:
        print(f"unknown marketplace {marketplace!r}; choose from: {', '.join(MARKETPLACES)}")
        return 2

    domain = MARKETPLACES[marketplace][0]
    section(f"amazon_web_scraper demo — marketplace={marketplace} ({domain})")
    print("Minting an anonymous session over HTTP (no emulator, no browser)…")

    # `open()` mints the session and hands back a ready scraper.
    with AmazonWebScraper.open(marketplace, zip_code="10001" if marketplace == "us" else None) as amz:

        # 1) SEARCH ----------------------------------------------------------
        results = list(amz.search("coffee grinder", pages=1))
        show_list("search('coffee grinder', pages=1)", results)
        if not results:
            print("\n(no search results — can't continue the product/review demos)")
            return 1
        asin = next((r["asin"] for r in results if r.get("price")), results[0]["asin"])

        # 2) PRODUCT DETAIL --------------------------------------------------
        product = amz.product(asin)
        show_obj(f"product({asin!r})", product)

        # 3) REVIEW AGGREGATE (anonymous) ------------------------------------
        show_obj(f"review_summary({asin!r})", amz.review_summary(asin))

        # 4) INDIVIDUAL REVIEWS (login-gated — shown as a handled limit) ------
        print("\n━━ reviews(asin)  [individual review text]")
        try:
            revs = list(amz.reviews(asin, pages=1))
            show_list(f"reviews({asin!r}, pages=1)", revs)
        except AuthRequired as exc:
            print(f"  skipped  : login required — {exc}")

        # 5) CATEGORY BROWSE -------------------------------------------------
        node = "172282" if marketplace == "us" else None   # US Electronics
        if node:
            show_list(f"browse({node!r}) [Electronics], pages=1",
                      list(amz.browse(node, pages=1)))

        # 6) SELLER + FEEDBACK ----------------------------------------------
        seller_id = product.get("seller_id")
        if not seller_id:
            # find a third-party seller from a category where they're common
            for row in list(amz.search("phone case", pages=1))[:6]:
                sid = amz.product(row["asin"]).get("seller_id")
                if sid:
                    seller_id = sid
                    break
        if seller_id:
            show_obj(f"seller({seller_id!r})", amz.seller(seller_id))
            show_list(f"seller_feedback({seller_id!r}, pages=1)",
                      list(amz.seller_feedback(seller_id, pages=1)))
        else:
            print("\n━━ seller / seller_feedback\n  skipped  : no third-party seller found in this sample")

    section("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "us"))
