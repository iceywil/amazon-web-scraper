"""Transport layer for the scraper.

Everything that is *not* specific to a given endpoint lives here: the HTTP/2
session, the headers we present, a polite rate limiter, and retry/backoff on the
status codes a backend throws when it wants us to slow down (429, 503, 5xx).

In a real reverse-engineering engagement the headers below are exactly what you
reconstruct from an intercepted request (mitmproxy / Frida). They are kept in one
place so that swapping in a captured ``User-Agent`` / auth token is a one-liner.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .metrics import Metrics

log = logging.getLogger("scraper.client")

# Status codes that mean "retry later" rather than "give up".
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class RetryableStatus(Exception):
    """Raised for a response whose status code warrants a retry."""


class RateLimiter:
    """Blocks so that calls are spaced at least ``min_interval`` seconds apart.

    Thread-safe so the same client can back several workers without exceeding
    the target rate. ``jitter`` adds a small random extra delay (0..jitter s) so
    requests don't fall on an exact, machine-like cadence. The limiter can also
    be *slowed* at runtime (e.g. after a throttle) via :meth:`penalise`.
    """

    def __init__(self, min_interval: float, *, jitter: float = 0.0) -> None:
        self.base_interval = max(0.0, min_interval)
        self.min_interval = self.base_interval
        self.jitter = max(0.0, jitter)
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self.total_wait = 0.0

    def wait(self) -> float:
        """Block until the next request is allowed; return seconds slept."""
        if self.min_interval <= 0 and self.jitter <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            sleep_for = self._next_allowed - now
            if self.jitter:
                sleep_for += random.uniform(0, self.jitter)
            slept = 0.0
            if sleep_for > 0:
                time.sleep(sleep_for)
                slept = sleep_for
                now = time.monotonic()
            self._next_allowed = now + self.min_interval
            self.total_wait += slept
            return slept

    def penalise(self, factor: float = 2.0, cap: float = 30.0) -> None:
        """After a throttle, widen the interval (decays back on success)."""
        with self._lock:
            self.min_interval = min(cap, max(self.min_interval, self.base_interval) * factor)

    def relax(self) -> None:
        """A clean response nudges the interval back toward the configured base."""
        with self._lock:
            if self.min_interval > self.base_interval:
                self.min_interval = max(self.base_interval, self.min_interval * 0.9)


class ScraperClient:
    """A thin, polite wrapper around :class:`httpx.Client`.

    Parameters
    ----------
    base_url:
        Host the adapter talks to by default. Adapters may also pass absolute URLs.
    user_agent:
        Identifies the caller. A real RE scraper replays the mobile app's
        captured UA verbatim.
    requests_per_second:
        Upper bound on request rate. Translated into a minimum inter-request delay.
    timeout:
        Per-request timeout in seconds.
    max_attempts:
        How many times to try a request before giving up (includes the first try).
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        user_agent: str = "amazon-web-scraper/0.1 (+https://github.com/your-org/amazon-web-scraper)",
        requests_per_second: float = 2.0,
        timeout: float = 25.0,
        max_attempts: int = 5,
        jitter: float = 0.0,
        adaptive: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.max_attempts = max_attempts
        self.adaptive = adaptive
        self.metrics = Metrics()
        self._limiter = RateLimiter(
            1.0 / requests_per_second if requests_per_second > 0 else 0.0, jitter=jitter)
        headers = {
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Accept-Language": "en",
        }
        if extra_headers:
            headers.update(extra_headers)
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            http2=True,
            timeout=timeout,
            follow_redirects=True,
        )

    @property
    def cookies(self) -> httpx.Cookies:
        """The underlying session cookie jar (persists across requests)."""
        return self._client.cookies

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ScraperClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- general request (used by the capture/replay harness) --------------
    def fetch(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | str | None = None,
    ) -> httpx.Response:
        """Perform an arbitrary request with the same rate-limit + retry policy.

        Per-request ``headers`` override the client defaults key-by-key, so a
        captured request's own ``User-Agent`` / auth headers take precedence.
        Returns the raw :class:`httpx.Response` (caller decides how to parse).
        """

        attempts = {"n": 0}

        @retry(
            reraise=True,
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            retry=retry_if_exception_type((RetryableStatus, httpx.TransportError)),
        )
        def _do() -> httpx.Response:
            if attempts["n"]:
                self.metrics.record_retry()
            attempts["n"] += 1
            self.metrics.add_wait(self._limiter.wait())
            t0 = time.monotonic()
            try:
                resp = self._client.request(method, url, params=params,
                                            headers=headers, content=content)
            except httpx.TransportError:
                self.metrics.record_transport_error()
                raise
            self.metrics.record_response(resp.status_code, time.monotonic() - t0)
            if resp.status_code in _RETRY_STATUSES:
                log.warning("transient %s on %s -> backing off", resp.status_code, resp.url)
                if self.adaptive and resp.status_code in (429, 503):
                    self._limiter.penalise()
                raise RetryableStatus(f"{resp.status_code} for {resp.url}")
            if self.adaptive:
                self._limiter.relax()
            return resp

        return _do()

    # -- core request ------------------------------------------------------
    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET ``url`` and return parsed JSON, retrying transient failures.

        Retries (with exponential backoff) on connection errors and on any of
        :data:`_RETRY_STATUSES`. A non-retryable 4xx raises immediately; a body
        that is not JSON is treated as a transient backend hiccup and retried.
        """

        @retry(
            reraise=True,
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            retry=retry_if_exception_type((RetryableStatus, httpx.TransportError)),
        )
        def _do() -> dict[str, Any]:
            self._limiter.wait()
            resp = self._client.get(url, params=params)
            if resp.status_code in _RETRY_STATUSES:
                log.warning("transient %s on %s -> backing off", resp.status_code, resp.url)
                raise RetryableStatus(f"{resp.status_code} for {resp.url}")
            resp.raise_for_status()  # non-retryable 4xx -> stop
            try:
                return resp.json()
            except ValueError as exc:
                # Backend sometimes returns an HTML error page with a 200.
                log.warning("non-JSON body on %s -> retrying", resp.url)
                raise RetryableStatus(f"non-JSON body for {resp.url}") from exc

        return _do()
