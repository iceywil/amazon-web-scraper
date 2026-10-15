#!/usr/bin/env python3
"""Benchmark the *client's own* politeness and observability.

This deliberately does NOT probe how many requests Amazon tolerates — we don't
load-test infrastructure we don't own. Instead it measures our own behaviour:

  A. the rate limiter actually spaces requests at the configured RPS   (offline)
  B. the adaptive penalise/relax logic reacts to throttles            (offline)
  C. [opt-in] latency + metrics over a SMALL, polite burst of real requests

    python benchmark.py                # offline checks only (no network)
    python benchmark.py --live 8       # + 8 real requests at ~1 rps (capped at 20)
"""
from __future__ import annotations

import argparse
import time

from scraper import AmazonWebScraper, RateLimiter

LIVE_CAP = 20          # hard ceiling: this is a politeness demo, not a stress test
LIVE_RPS = 1.0         # gentle


def bar(title: str) -> None:
    print("\n" + "─" * 68 + f"\n{title}\n" + "─" * 68)


def check_rate_limiter() -> None:
    """A: measured throughput must not exceed the configured target."""
    bar("A. rate limiter spacing (offline, no network)")
    # The limiter guarantees a minimum gap *between* consecutive requests, so the
    # sustained rate is (n-1)/elapsed (the first request is free).
    print(f"{'target rps':>10} | {'requests':>8} | {'elapsed':>8} | {'sustained rps':>13}")
    for target in (5.0, 2.0, 1.0):
        n = 6
        rl = RateLimiter(1.0 / target)
        t0 = time.monotonic()
        for _ in range(n):
            rl.wait()
        elapsed = time.monotonic() - t0
        sustained = (n - 1) / elapsed
        ok = "ok" if sustained <= target * 1.05 else "OVER"
        print(f"{target:>10.1f} | {n:>8} | {elapsed:>7.2f}s | {sustained:>12.2f}  {ok}")


def check_adaptive() -> None:
    """B: a throttle widens the interval; clean responses decay it back."""
    bar("B. adaptive backoff (offline)")
    rl = RateLimiter(0.5)   # base 0.5s between requests
    print(f"base interval        : {rl.min_interval:.2f}s")
    rl.penalise(); print(f"after 1 throttle     : {rl.min_interval:.2f}s")
    rl.penalise(); print(f"after 2 throttles    : {rl.min_interval:.2f}s")
    for _ in range(5):
        rl.relax()
    print(f"after 5 clean replies: {rl.min_interval:.2f}s (decaying toward base)")


def check_live(n: int, marketplace: str) -> None:
    """C: metrics over a small, polite burst of real requests."""
    n = min(n, LIVE_CAP)
    bar(f"C. live politeness demo — {n} real request(s) at ~{LIVE_RPS}/s ({marketplace})")
    print("NOTE: a small, rate-limited sample to read our own latency/metrics —\n"
          "      not an attempt to find Amazon's limits.\n")
    with AmazonWebScraper.open(marketplace,
                               zip_code="10001" if marketplace == "us" else None) as amz:
        amz.client.metrics.__init__()        # reset counters after session mint
        amz.client._limiter.base_interval = amz.client._limiter.min_interval = 1.0 / LIVE_RPS
        amz.client._limiter.jitter = 0.3
        collected = 0
        # one search page, then a few product fetches — enough to fill metrics
        rows = list(amz.search("coffee grinder", pages=1))
        collected += len(rows)
        for row in rows[: max(0, n - 1)]:
            amz.product(row["asin"])
            collected += 1
        print(f"collected ~{collected} objects")
        print(amz.client.metrics.pretty())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", type=int, metavar="N", default=0,
                    help=f"also run N real requests (polite, capped at {LIVE_CAP})")
    ap.add_argument("--marketplace", default="us")
    args = ap.parse_args()

    check_rate_limiter()
    check_adaptive()
    if args.live > 0:
        check_live(args.live, args.marketplace)
    else:
        print("\n(offline checks only — pass --live N to measure real-request metrics)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
