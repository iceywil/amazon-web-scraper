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
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scraper import AmazonWebScraper, RateLimiter, ScraperClient

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


# --- local mock server: a stand-in for a rate-limited backend ----------------
class _RateGate:
    """Allows up to `limit` requests per rolling 1s window; else the caller 429s."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._hits and now - self._hits[0] > 1.0:
                self._hits.popleft()
            if len(self._hits) >= self.limit:
                return False
            self._hits.append(now)
            return True


def _make_handler(gate: _RateGate):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):              # noqa: N802
            if gate.allow():
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            else:                      # over the limit -> throttle, like Amazon
                self.send_response(429)
                self.send_header("retry-after", "1")
                self.end_headers()
                self.wfile.write(b"throttled")

        def log_message(self, *a):     # silence the default stderr logging
            pass
    return Handler


class _MockBackend:
    """A localhost server that throttles above `limit` req/s — our safe 'edge'."""

    def __init__(self, limit: int) -> None:
        self.gate = _RateGate(limit)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.gate))
        self.port = self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()


def ramp_against_mock(limit: int, rates, per_rate: int, adaptive: bool) -> None:
    """Ramp request rate against a local mock whose edge is `limit` req/s.

    Shows throughput climb, flatten at the edge, and (with adaptive) how the
    client backs off to avoid the wall — all without touching any real service.
    """
    mode = "adaptive backoff ON" if adaptive else "adaptive OFF (raw edge)"
    bar(f"RAMP vs local mock — edge = {limit} req/s  [{mode}]")
    print("Escalating the request rate against OUR OWN server (localhost); the "
          "mock\nreturns 429 past its limit — this is where we 'see the edge'.\n")
    print(f"{'target rps':>10} | {'sent':>5} | {'200':>4} | {'429':>4} | "
          f"{'ok rps':>7} | {'retries':>7}")
    with _MockBackend(limit) as mock:
        for rps in rates:
            c = ScraperClient(requests_per_second=rps, adaptive=adaptive,
                              max_attempts=4, timeout=5.0)
            try:
                for _ in range(per_rate):
                    try:
                        c.fetch("GET", mock.url)
                    except Exception:
                        pass
                m = c.metrics
                ok = m.by_status.get(200, 0)
                th = m.by_status.get(429, 0)
                ok_rps = ok / m.elapsed if m.elapsed else 0.0
                print(f"{rps:>10.0f} | {m.requests:>5} | {ok:>4} | {th:>4} | "
                      f"{ok_rps:>7.1f} | {m.retries:>7}")
            finally:
                c.close()
    note = ("adaptive ON widens the interval after each 429, so repeat hits taper off"
            if adaptive else
            "adaptive OFF keeps firing at the target rate, so 429s recur")
    print(f"\nRead the edge: successful throughput plateaus around {limit}/s; past that "
          f"the\nmock returns 429 and the client retries with backoff. {note}.")


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
    ap.add_argument("--ramp", action="store_true",
                    help="ramp the rate against a LOCAL mock to find the edge (safe)")
    ap.add_argument("--edge", type=int, default=10, metavar="RPS",
                    help="the mock's throttle limit for --ramp (default 10/s)")
    ap.add_argument("--live", type=int, metavar="N", default=0,
                    help=f"also run N real requests (polite, capped at {LIVE_CAP})")
    ap.add_argument("--marketplace", default="us")
    args = ap.parse_args()

    check_rate_limiter()
    check_adaptive()
    if args.ramp:
        rates = [r for r in (2, 5, 10, 20, 40) if r <= args.edge * 5]
        ramp_against_mock(args.edge, rates, per_rate=40, adaptive=False)
        ramp_against_mock(args.edge, rates, per_rate=40, adaptive=True)
    if args.live > 0:
        check_live(args.live, args.marketplace)
    if not args.ramp and not args.live:
        print("\n(offline checks only — pass --ramp to find the edge against a local "
              "mock,\n or --live N to measure real-request metrics)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
