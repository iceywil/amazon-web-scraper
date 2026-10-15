"""Lightweight, thread-safe metrics for the transport layer.

Attached to every :class:`~scraper.client.ScraperClient` as ``.metrics`` so a run
can be observed: how many requests went out, their status mix, how much time was
spent waiting on the rate limiter, retries, throttles (429/503), and latency.

It measures *our own* behaviour — it is not a tool for probing a remote service's
limits. Keep collection polite; read the numbers to tune delays, not to find a
server's breaking point.
"""
from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class Metrics:
    requests: int = 0                     # responses actually received
    retries: int = 0                      # retried attempts
    throttled: int = 0                    # 429 / 503 responses seen
    transport_errors: int = 0             # connection/timeout failures
    by_status: Counter = field(default_factory=Counter)
    rate_limit_wait: float = 0.0          # total seconds slept by the limiter
    _lat_sum: float = 0.0
    _lat_n: int = 0
    _lat_min: float = float("inf")
    _lat_max: float = 0.0
    _start: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_response(self, status: int, latency: float) -> None:
        with self._lock:
            self.requests += 1
            self.by_status[status] += 1
            if status in (429, 503):
                self.throttled += 1
            self._lat_sum += latency
            self._lat_n += 1
            self._lat_min = min(self._lat_min, latency)
            self._lat_max = max(self._lat_max, latency)

    def record_retry(self) -> None:
        with self._lock:
            self.retries += 1

    def record_transport_error(self) -> None:
        with self._lock:
            self.transport_errors += 1

    def add_wait(self, seconds: float) -> None:
        with self._lock:
            self.rate_limit_wait += seconds

    # -- reporting ------------------------------------------------------------
    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._start

    @property
    def avg_latency(self) -> float:
        return self._lat_sum / self._lat_n if self._lat_n else 0.0

    @property
    def effective_rps(self) -> float:
        el = self.elapsed
        return self.requests / el if el > 0 else 0.0

    def summary(self) -> dict:
        return {
            "requests": self.requests,
            "retries": self.retries,
            "throttled": self.throttled,
            "transport_errors": self.transport_errors,
            "by_status": dict(self.by_status),
            "elapsed_s": round(self.elapsed, 2),
            "effective_rps": round(self.effective_rps, 2),
            "rate_limit_wait_s": round(self.rate_limit_wait, 2),
            "latency_ms": {
                "avg": round(self.avg_latency * 1000),
                "min": round(self._lat_min * 1000) if self._lat_n else 0,
                "max": round(self._lat_max * 1000),
            },
        }

    def pretty(self) -> str:
        s = self.summary()
        lat = s["latency_ms"]
        return (
            f"requests={s['requests']}  "
            f"status={s['by_status']}  "
            f"retries={s['retries']}  throttled={s['throttled']}  "
            f"errors={s['transport_errors']}\n"
            f"elapsed={s['elapsed_s']}s  effective={s['effective_rps']}/s  "
            f"rate-limit-wait={s['rate_limit_wait_s']}s  "
            f"latency avg/min/max={lat['avg']}/{lat['min']}/{lat['max']}ms"
        )
