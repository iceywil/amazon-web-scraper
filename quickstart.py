#!/usr/bin/env python3
"""Quick test: search a marketplace, then show the first result, its first review
with a photo, and that reviewer's profile, in the terminal and in output.json.

    python quickstart.py                      # asks what to search (amazon.fr)
    python quickstart.py "cat food"           # no prompt
    python quickstart.py "cat food" --marketplace uk --out output.json

No sign-in needed: on amazon.fr the product page itself serves its top reviews
anonymously, and reviewer profiles are public. Images (product photo, review
photos, reviewer avatar) are saved under ``output/assets/``; inline previews are
shown in terminals that support them (iTerm2, WezTerm, VS Code with
``terminal.integrated.enableImages``).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

from scraper import MARKETPLACES, AmazonWebScraper, BotWall, SessionError, parse_product, parse_reviews
from scraper.amazon_web import _full_size

ORGANIC_TRIES = 5   # results to try when the first one has no review with a photo


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("query", nargs="?", help="what to search (prompted if omitted)")
    ap.add_argument("--marketplace", default="fr", choices=sorted(MARKETPLACES))
    ap.add_argument("--out", default="output.json", help="JSON export (default output.json)")
    ap.add_argument("--assets", default="output/assets", help="where images are saved")
    args = ap.parse_args()

    domain = MARKETPLACES[args.marketplace][0]
    query = args.query or input(f"Search {domain.removeprefix('www.')} for: ").strip()
    if not query:
        print("nothing to search", file=sys.stderr)
        return 2

    try:
        amz = _open(args.marketplace)
    except BotWall as exc:
        print(f"Amazon is showing a robot check to this IP ({exc}). "
              "Wait a while and try again.", file=sys.stderr)
        return 3

    with amz:
        step(f"Searching {domain} for {query!r}")
        results = [r for r in amz.search(query, pages=1) if not r.get("sponsored")]
        if not results:
            print("no results", file=sys.stderr)
            return 1

        product = review = None
        for card in results[:ORGANIC_TRIES]:
            # One fetch of the product page gives both its details and its top reviews.
            html = amz._get(f"/dp/{card['asin']}").text
            product = {**card, **{k: v for k, v in parse_product(html).items() if v}}
            review = next((r for r in parse_reviews(html, amz.base) if r["images"]), None)
            if review:
                break
            print(f"  {card['asin']}: no review with a photo on its page, trying the next result")
        show_product(product)
        if not review:
            print("\nNo review with a photo among the first results.")
            return _export(args, query, product, None, None, {})

        show_review(review)
        reviewer = amz.profile(review["author_profile"]) if review.get("author_profile") else None
        if reviewer:
            show_reviewer(reviewer)

        step(f"Saving images to {args.assets}/")
        assets = save_assets(amz, Path(args.assets), product, review, reviewer)
        for label, paths in assets.items():
            for p in paths if isinstance(paths, list) else [paths]:
                print(f"  {label:<16} {p}")
                inline_image(p)
        return _export(args, query, product, review, reviewer, assets)


def _open(marketplace: str) -> AmazonWebScraper:
    """Mint a session, retrying the occasional seed page that sets no cookie."""
    for attempt in range(3):
        try:
            return AmazonWebScraper.open(marketplace)
        except SessionError:
            if attempt == 2:
                raise
            time.sleep(3)
    raise AssertionError("unreachable")


def save_assets(amz: AmazonWebScraper, folder: Path, product: dict, review: dict,
                reviewer: dict | None) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    assets: dict = {}
    if product.get("image"):
        # The page shows a resized copy (…_AC_SX300_….jpg); drop the directive for the original.
        assets["product_image"] = download(amz, _full_size(product["image"]),
                                           folder / f"product_{product['asin']}")
    assets["review_images"] = [download(amz, url, folder / f"review_{review['id']}_{i + 1}")
                               for i, url in enumerate(review["images"])]
    avatar = (reviewer or {}).get("avatar") or review.get("author_avatar")
    if avatar:
        name = (reviewer or {}).get("profile_id") or review["id"]
        assets["reviewer_avatar"] = download(amz, avatar, folder / f"avatar_{name}")
    return assets


def download(amz: AmazonWebScraper, url: str, stem: Path) -> str:
    resp = amz.client.fetch("GET", url, headers={"user-agent": amz._headers()["user-agent"]})
    ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(
        resp.headers.get("content-type", "").split(";")[0], ".jpg")
    path = stem.with_suffix(ext)
    path.write_bytes(resp.content)
    return str(path)


def _export(args, query, product, review, reviewer, assets) -> int:
    out = {"query": query, "marketplace": args.marketplace, "product": product,
           "review": review, "reviewer": reviewer, "assets": assets}
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    step(f"JSON written to {args.out}")
    return 0


# -- terminal output ----------------------------------------------------------

def step(text: str) -> None:
    print(f"\n\033[1m==> {text}\033[0m")


def field(label: str, value) -> None:
    if value not in (None, "", []):
        print(f"  {label:<14} {value}")


def show_product(p: dict) -> None:
    step("First result")
    field("title", p.get("title"))
    field("asin", p.get("asin"))
    field("price", f"{p['price']} {p.get('currency') or ''}".strip() if p.get("price") else None)
    field("rating", f"{p['rating']} ({p.get('reviews') or '?'} ratings)" if p.get("rating") else None)
    field("seller", p.get("seller"))
    field("url", p.get("url"))


def show_review(r: dict) -> None:
    step("First review with a photo")
    field("title", r.get("title"))
    field("rating", r.get("rating"))
    field("author", r.get("author"))
    field("date", r.get("date"))
    field("verified", "yes" if r.get("verified") else None)
    body = (r.get("body") or "").strip()
    field("text", body if len(body) <= 300 else body[:300] + "…")
    field("photos", len(r["images"]))


def show_reviewer(p: dict) -> None:
    step("Reviewer profile")
    field("name", p.get("name"))
    field("profile id", p.get("profile_id"))
    field("bio", p.get("bio"))
    field("influencer", "yes" if p.get("is_influencer") else None)
    field("reviews", len(p.get("reviews") or []))
    field("avatar", p.get("avatar") or "(default silhouette)")


def inline_image(path: str) -> None:
    """Show an image inline via the iTerm2 protocol when the terminal supports it."""
    if os.environ.get("TERM_PROGRAM") not in ("iTerm.app", "WezTerm", "vscode") or not sys.stdout.isatty():
        return
    data = Path(path).read_bytes()
    b64 = base64.b64encode(data).decode()
    sys.stdout.write(f"\033]1337;File=inline=1;size={len(data)};height=8;preserveAspectRatio=1:{b64}\a\n")
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
