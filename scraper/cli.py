"""Command line interface.

Examples
--------
    python run.py replay --curl android/example_request.curl --dry-run
    python run.py replay --curl req.curl --page-param page --pages 5 -o out/records.json
    python run.py web-search --keyword "coffee mug"     # mints a headless session itself
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .amazon import AmazonScraper, BotWall, describe
from .amazon_web import AmazonWebScraper, AuthRequired, BotWall as WebBotWall
from .capture import load_curl_file
from .client import ScraperClient


def build_parser() -> argparse.ArgumentParser:
    # Shared options, attached to both the top parser and every subparser so they
    # may appear before OR after the subcommand name.
    # default=SUPPRESS so a value given on one side of the subcommand is not
    # overwritten by the parser on the other side; real defaults applied in main().
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--format", choices=("json", "ndjson"), default=argparse.SUPPRESS,
                        help="output format (default json)")
    common.add_argument("-o", "--out", default=argparse.SUPPRESS, help="output file (default: stdout)")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="log pagination progress to stderr")

    p = argparse.ArgumentParser(prog="re-scraper", parents=[common],
                                description="Amazon mobile-backend capture-replay scraper")
    sub = p.add_subparsers(dest="command", required=True)

    # Generic capture-replay scraper (used for Amazon and any intercepted target).
    r = sub.add_parser("replay", parents=[common],
                       help="replay a captured request (Copy as cURL) and paginate it")
    r.add_argument("--curl", required=True, metavar="FILE",
                   help="file containing a 'curl ...' command copied from mitmproxy/Burp")
    r.add_argument("--page-param", help="query param to increment for pagination (e.g. page)")
    r.add_argument("--start-page", type=int, default=1)
    r.add_argument("--pages", type=int, default=1)
    r.add_argument("--records-path", help="dotted path to the record list (e.g. data.results)")
    r.add_argument("--rps-replay", type=float, default=1.0, dest="rps_replay",
                   help="rate for replay (default 1/s; keep it gentle)")
    r.add_argument("--dry-run", action="store_true",
                   help="print what would be sent (secrets redacted) and exit")

    # Web storefront (the pages the app renders in WebViews; cookie-auth HTML).
    # Shared options for every web-* command: a session source + marketplace.
    web = argparse.ArgumentParser(add_help=False)
    web.add_argument("--curl", metavar="FILE",
                     help="a captured www.amazon.<tld> request (for its session cookies)")
    web.add_argument("--cookies", metavar="FILE",
                     help="a session JSON from 'mint-session' (alternative to --curl)")
    web.add_argument("--zip", dest="zip_code", default="auto",
                     help="postal code for the headless session minted when neither "
                          "--cookies nor --curl is given (default: 10001 on us; 'none' "
                          "keeps the IP-geolocated store)")
    web.add_argument("--marketplace", default="us",
                     help="country marketplace: us, uk, de, fr, it, es, ca, jp, in, mx, br, au")
    web.add_argument("--pages", type=int, default=1)
    web.add_argument("--start-page", type=int, default=1)
    web.add_argument("--rps-replay", type=float, default=1.0, dest="rps_replay",
                     help="request rate (default 1/s; keep it gentle)")

    ws = sub.add_parser("web-search", parents=[common, web],
                        help="search the storefront (/s) and parse product cards")
    ws.add_argument("--keyword", required=True, help="search term")

    wb = sub.add_parser("web-browse", parents=[common, web],
                        help="list a category browse node (/s?node=) as product cards")
    wb.add_argument("--node", required=True, help="browse node id")

    wp = sub.add_parser("web-product", parents=[common, web],
                        help="fetch /dp/<ASIN> product pages and parse their detail fields")
    wp.add_argument("--asin", required=True, action="append", help="product ASIN (repeatable)")

    wrs = sub.add_parser("web-review-summary", parents=[common, web],
                         help="aggregate review data (average, count, star histogram) from /dp")
    wrs.add_argument("--asin", required=True, action="append", help="product ASIN (repeatable)")

    wr = sub.add_parser("web-reviews", parents=[common, web],
                        help="individual reviews from /product-reviews/<ASIN> (login-gated)")
    wr.add_argument("--asin", required=True, help="product ASIN")

    wse = sub.add_parser("web-seller", parents=[common, web],
                         help="fetch a seller ('company') page /sp?seller=<ID>")
    wse.add_argument("--seller", required=True, help="seller id")

    wf = sub.add_parser("web-seller-feedback", parents=[common, web],
                        help="fetch a seller's feedback entries")
    wf.add_argument("--seller", required=True, help="seller id")

    # Mint an anonymous session over plain HTTP (no emulator, no browser).
    ms = sub.add_parser("mint-session",
                        help="mint an anonymous session over HTTP and save its cookies")
    ms.add_argument("--marketplace", default="us")
    ms.add_argument("--zip", dest="zip_code", default=None,
                    help="postal code to pin delivery location (e.g. US 10001)")
    ms.add_argument("--seed-asin", default=None, help="override the seed product ASIN")
    ms.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    ms.add_argument("-o", "--out", default="session.json", dest="session_out",
                    help="where to write the session JSON (default session.json)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for key, default in {"format": "json", "out": None, "verbose": False}.items():
        if not hasattr(args, key):
            setattr(args, key, default)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    if args.command == "replay":
        return _run_replay(args)
    if args.command == "mint-session":
        return _run_mint(args)
    if args.command.startswith("web-"):
        return _run_web(args)
    return 2  # pragma: no cover


def _emit(records: list, args: argparse.Namespace) -> int:
    """Write records as json/ndjson to --out or stdout (shared by web commands)."""
    out = args.out
    stream = open(out, "w", encoding="utf-8") if out else sys.stdout
    try:
        if args.format == "ndjson":
            for rec in records:
                stream.write(json.dumps(rec, ensure_ascii=False) + "\n")
        else:
            json.dump(records, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    finally:
        if out:
            stream.close()
            print(f"wrote {len(records)} records -> {out}", file=sys.stderr)
    return 0 if records else 1


def _run_mint(args: argparse.Namespace) -> int:
    from .session import mint_session, SessionError, DEFAULT_SEED_ASIN
    try:
        sess = mint_session(args.marketplace, zip_code=args.zip_code,
                            seed_asin=args.seed_asin or DEFAULT_SEED_ASIN)
    except SessionError as exc:
        print(f"mint failed: {exc}", file=sys.stderr)
        return 3
    Path(args.session_out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.session_out, "w", encoding="utf-8") as fh:
        json.dump(sess, fh, indent=2)
    print(f"session for {sess['marketplace']} ({sess['domain']}): "
          f"{len(sess['cookies'])} cookies -> {args.session_out}", file=sys.stderr)
    return 0


def _web_scraper(args: argparse.Namespace, client: ScraperClient) -> AmazonWebScraper:
    """Build a web scraper from --cookies (minted), --curl (captured), or, by
    default, a session minted headlessly on the spot."""
    if args.cookies:
        with open(args.cookies, encoding="utf-8") as fh:
            sess = json.load(fh)
        cookies = sess.get("cookies", sess)   # accept a bare cookie dict too
        mkt = sess.get("marketplace", args.marketplace)
        print(f"session from {args.cookies}: {len(cookies)} cookies, marketplace={mkt}",
              file=sys.stderr)
        return AmazonWebScraper(cookies=cookies, marketplace=mkt, client=client)
    if args.curl:
        template = load_curl_file(args.curl)
        print(f"template cookies from: {template.summary()}", file=sys.stderr)
        return AmazonWebScraper(template, marketplace=args.marketplace, client=client)
    from .session import DEFAULT_ZIPS, mint_session
    zip_code = (DEFAULT_ZIPS.get(args.marketplace) if args.zip_code == "auto"
                else None if args.zip_code.lower() == "none" else args.zip_code)
    sess = mint_session(args.marketplace, zip_code=zip_code)
    print(f"headless session ({args.marketplace}, zip {zip_code or 'geo-IP'}): "
          f"{len(sess['cookies'])} cookies", file=sys.stderr)
    return AmazonWebScraper(cookies=sess["cookies"], marketplace=args.marketplace,
                            client=client)


def _run_web(args: argparse.Namespace) -> int:
    from .session import SessionError
    client = ScraperClient(requests_per_second=args.rps_replay)
    records: list = []
    try:
        scraper = _web_scraper(args, client)
    except SessionError as exc:
        print(f"SESSION: could not mint a headless session: {exc}", file=sys.stderr)
        client.close()
        return 5
    try:
        with scraper as s:
            if args.command == "web-search":
                it = s.search(args.keyword, pages=args.pages, start_page=args.start_page)
            elif args.command == "web-browse":
                it = s.browse(args.node, pages=args.pages, start_page=args.start_page)
            elif args.command == "web-product":
                it = (s.product(a) for a in args.asin)
            elif args.command == "web-review-summary":
                it = (s.review_summary(a) for a in args.asin)
            elif args.command == "web-reviews":
                it = s.reviews(args.asin, pages=args.pages, start_page=args.start_page)
            elif args.command == "web-seller":
                it = iter([s.seller(args.seller)])
            elif args.command == "web-seller-feedback":
                it = s.seller_feedback(args.seller, pages=args.pages, start_page=args.start_page)
            else:  # pragma: no cover
                return 2
            for rec in it:
                records.append(rec)
                if args.verbose:
                    print(f"  {_oneline(rec)}", file=sys.stderr)
    except AuthRequired as exc:
        print(f"LOGIN REQUIRED: {exc}", file=sys.stderr)
        return 4
    except WebBotWall as exc:
        print(f"BOT WALL: {exc}", file=sys.stderr)
        return 3
    return _emit(records, args)


def _oneline(rec: dict) -> str:
    for key in ("asin", "seller_id", "name", "id"):
        if rec.get(key):
            head = f"{key}={rec[key]}"
            break
    else:
        head = "record"
    extra = rec.get("title") or rec.get("name") or rec.get("comment") or ""
    return f"{head} {str(extra)[:60]}"


def _run_replay(args: argparse.Namespace) -> int:
    captured = load_curl_file(args.curl)
    print(f"captured: {captured.summary()}", file=sys.stderr)
    if args.dry_run:
        print(describe(captured))
        return 0

    client = ScraperClient(requests_per_second=args.rps_replay)
    records: list = []
    try:
        with AmazonScraper(captured, client=client) as scraper:
            for rec in scraper.crawl(
                page_param=args.page_param,
                start_page=args.start_page,
                pages=args.pages,
                record_path=args.records_path,
            ):
                records.append(rec)
    except BotWall as exc:
        print(f"BOT WALL: {exc}", file=sys.stderr)
        return 3

    out = args.out
    stream = open(out, "w", encoding="utf-8") if out else sys.stdout
    try:
        if args.format == "ndjson":
            for rec in records:
                stream.write(json.dumps(rec, ensure_ascii=False) + "\n")
        else:
            json.dump(records, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    finally:
        if out:
            stream.close()
            print(f"wrote {len(records)} records -> {out}", file=sys.stderr)
    return 0 if records else 1


if __name__ == "__main__":
    raise SystemExit(main())
