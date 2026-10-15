"""Replay-based scraper for the Amazon mobile backend.

You cannot forge Amazon's request signing or its MAP session tokens by hand, and
you should not try to — the realistic, legitimate approach is to **replay a request
you captured from your own authenticated session** on your own device, then vary
only the pagination parameter.

This module takes a :class:`~scraper.capture.CapturedRequest` (produced from a
"Copy as cURL" of an intercepted request) and:

  * replays it through the resilient client (rate limit + retry/backoff),
  * walks pages by incrementing one query parameter,
  * pulls records out of the JSON at a configurable dotted path,
  * and tells you honestly when the response is a bot-check / login wall rather
    than data, instead of silently producing garbage.

Nothing here defeats a protection: it reuses the session you legitimately hold.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from .capture import CapturedRequest
from .client import ScraperClient

log = logging.getLogger("scraper.amazon")

# Fingerprints of Amazon's anti-bot / auth interstitials.
_WALL_MARKERS = (
    "api-services-support@amazon.com",
    "captcha",
    "Robot Check",
    "To discuss automated access",
    "Enter the characters you see below",
    "ap/signin",
    "validateCaptcha",
)


class BotWall(Exception):
    """Raised when a response is a challenge/login page rather than data."""


class AmazonScraper:
    """Replays a captured Amazon request and paginates it."""

    def __init__(self, captured: CapturedRequest, client: ScraperClient | None = None) -> None:
        self.captured = captured
        # No synthetic default headers: the captured request carries its own
        # User-Agent, auth token, signing and cookies. We only add rate/retry.
        self.client = client or ScraperClient(requests_per_second=1.0)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "AmazonScraper":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- single replay -----------------------------------------------------
    def fetch(self, **param_overrides: object) -> Any:
        """Replay the captured request, overriding chosen query params.

        Returns parsed JSON when possible. Raises :class:`BotWall` if the body
        looks like a challenge/login page.
        """
        req = self.captured
        resp = self.client.fetch(
            req.method,
            req.base_url,
            params=req.with_params(**param_overrides),
            headers=req.headers,
            content=req.body,
        )
        text = resp.text
        snippet = text[:4000]
        if any(marker.lower() in snippet.lower() for marker in _WALL_MARKERS):
            raise BotWall(
                f"response from {resp.url} looks like a bot-check / login wall "
                f"(status {resp.status_code}). Your captured session/token has "
                f"likely expired — re-capture a fresh request from the app."
            )
        try:
            return resp.json()
        except ValueError:
            # Not JSON and not an obvious wall: hand back the raw text to inspect.
            log.warning("non-JSON response (status %s, %d bytes)", resp.status_code, len(text))
            return {"_raw": text}

    # -- record extraction -------------------------------------------------
    @staticmethod
    def extract(payload: Any, record_path: str | None) -> list[Any]:
        """Pull a list of records out of ``payload`` at a dotted ``record_path``.

        ``record_path`` may index into dicts ("data.results") and lists
        ("data.0.items"). With no path, a top-level list is returned as-is and a
        dict is wrapped in a single-element list.
        """
        if record_path:
            node: Any = payload
            for part in record_path.split("."):
                if isinstance(node, list):
                    node = node[int(part)]
                elif isinstance(node, dict):
                    node = node.get(part)
                else:
                    node = None
                if node is None:
                    return []
            return node if isinstance(node, list) else [node]
        if isinstance(payload, list):
            return payload
        return [payload]

    # -- paginated crawl ---------------------------------------------------
    def crawl(
        self,
        *,
        page_param: str | None = None,
        start_page: int = 1,
        pages: int = 1,
        record_path: str | None = None,
    ) -> Iterator[Any]:
        """Yield records across ``pages`` requests.

        If ``page_param`` is given, that query parameter is set to
        ``start_page .. start_page + pages - 1`` on successive requests;
        otherwise the captured request is replayed once as-is.
        """
        if not page_param:
            yield from self.extract(self.fetch(), record_path)
            return
        for page in range(start_page, start_page + pages):
            payload = self.fetch(**{page_param: page})
            records = self.extract(payload, record_path)
            log.info("page %s -> %d records", page, len(records))
            if not records:
                break
            yield from records


def describe(captured: CapturedRequest) -> str:
    """A short human summary of what will be replayed (for --dry-run)."""
    redacted = {}
    for k, v in captured.headers.items():
        if k.lower() in {"authorization", "cookie", "x-amz-access-token", "x-amzn-sessionid"}:
            redacted[k] = v[:8] + "…(redacted)" if v else v
        else:
            redacted[k] = v
    return json.dumps(
        {"method": captured.method, "base_url": captured.base_url,
         "params": captured.params, "headers": redacted,
         "has_body": bool(captured.body)},
        indent=2, ensure_ascii=False,
    )
