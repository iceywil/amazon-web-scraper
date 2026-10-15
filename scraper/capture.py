"""Turn a captured request into a replayable object.

Workflow on your own device:

1. Run the Amazon app in an emulator routed through mitmproxy / Burp.
2. Trigger the screen you care about (search, product, orders...).
3. Right-click the request -> "Copy as cURL".
4. Paste it into a file and point this module at it.

``parse_curl`` understands the flags Burp/mitmproxy/Chrome emit: -X, -H, -b,
-d/--data*, -A, -e, --compressed, and the URL. Everything the request needs to
be replayed — method, headers, cookies, query params, body — is preserved, so
the only thing you ever hand-edit is a page number.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


@dataclass
class CapturedRequest:
    method: str = "GET"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    body: str | None = None

    @property
    def base_url(self) -> str:
        """The URL without its query string (params are carried separately)."""
        parts = urlsplit(self.url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    def with_params(self, **overrides: object) -> dict[str, str]:
        """Return a copy of the params with ``overrides`` applied (e.g. page=2)."""
        merged = dict(self.params)
        for key, value in overrides.items():
            merged[key] = str(value)
        return merged

    def summary(self) -> str:
        host = urlsplit(self.url).netloc
        return (f"{self.method} {host}{urlsplit(self.url).path} "
                f"({len(self.headers)} headers, {len(self.params)} params"
                f"{', body' if self.body else ''})")


# Flags that take a value we care about.
_VALUE_FLAGS = {
    "-X": "method", "--request": "method",
    "-H": "header", "--header": "header",
    "-b": "cookie", "--cookie": "cookie",
    "-A": "user-agent", "--user-agent": "user-agent",
    "-e": "referer", "--referer": "referer",
    "-d": "data", "--data": "data", "--data-raw": "data",
    "--data-binary": "data", "--data-ascii": "data", "--data-urlencode": "data",
}
# Value-less flags to skip.
_SKIP_FLAGS = {"--compressed", "-s", "--silent", "-L", "--location", "-k",
               "--insecure", "-i", "--include", "-v", "--verbose", "-g", "--globoff"}


def parse_curl(text: str) -> CapturedRequest:
    """Parse a ``curl ...`` command string into a :class:`CapturedRequest`."""
    # Join line-continuations, drop a leading "curl".
    text = text.strip()
    text = text.replace("\\\n", " ").replace("^\n", " ").replace("`\n", " ")
    tokens = shlex.split(text)
    if tokens and tokens[0] == "curl":
        tokens = tokens[1:]

    req = CapturedRequest()
    has_body = False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _VALUE_FLAGS:
            kind = _VALUE_FLAGS[tok]
            value = tokens[i + 1] if i + 1 < len(tokens) else ""
            i += 2
            _apply(req, kind, value)
            if kind == "data":
                has_body = True
            continue
        if tok in _SKIP_FLAGS or (tok.startswith("-") and tok != "-"):
            # Unknown flag; skip it (and a following value if it clearly has one).
            i += 1
            continue
        # Positional = the URL.
        if not req.url:
            req.url = tok.strip("'\"")
        i += 1

    # Split the URL's own query string into params.
    parts = urlsplit(req.url)
    if parts.query:
        req.params.update(dict(parse_qsl(parts.query, keep_blank_values=True)))

    # curl implies POST when a body is present and no method was given.
    if has_body and req.method == "GET":
        req.method = "POST"
    req.method = req.method.upper()
    return req


def _apply(req: CapturedRequest, kind: str, value: str) -> None:
    if kind == "method":
        req.method = value
    elif kind == "header":
        if ":" in value:
            name, _, val = value.partition(":")
            req.headers[name.strip()] = val.strip()
    elif kind == "cookie":
        req.headers["Cookie"] = value
    elif kind == "user-agent":
        req.headers["User-Agent"] = value
    elif kind == "referer":
        req.headers["Referer"] = value
    elif kind == "data":
        req.body = (req.body + "&" + value) if req.body else value


def load_curl_file(path: str) -> CapturedRequest:
    with open(path, encoding="utf-8") as fh:
        return parse_curl(fh.read())
