"""Disposable inboxes via the free mail.tm API, persisted to a JSON file.

Each created inbox (address, password, bearer token, account id) is saved to
``mailtm.json`` so it can be reused later to read mail or pull a one-time code::

    python -m scraper.mailtm create                 # new inbox, prints the address
    python -m scraper.mailtm list                   # inboxes saved in the store
    python -m scraper.mailtm otp  abc@domain.tld    # wait for a code and print it
    python -m scraper.mailtm inbox abc@domain.tld   # dump received messages

From Python::

    from scraper.mailtm import MailTm
    mt = MailTm()
    acct = mt.create()
    code = mt.wait_for_otp(acct["address"], timeout=120)
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import string
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

API = "https://api.mail.tm"
DEFAULT_STORE = "mailtm.json"
OTP_RE = re.compile(r"(?<![\d#])(\d{4,8})(?!\d)")


class MailTmError(RuntimeError):
    pass


class MailTm:
    def __init__(self, store: str | Path = DEFAULT_STORE, api: str = API, timeout: float = 20.0):
        self.store = Path(store)
        self.http = httpx.Client(base_url=api, timeout=timeout,
                                 headers={"Accept": "application/json"})

    # -- store ---------------------------------------------------------------
    def _load(self) -> dict:
        if self.store.exists():
            return json.loads(self.store.read_text(encoding="utf-8"))
        return {"accounts": {}}

    def _save(self, data: dict) -> None:
        self.store.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def accounts(self) -> dict[str, dict]:
        return self._load()["accounts"]

    def get(self, address: str) -> dict:
        acct = self.accounts().get(address)
        if not acct:
            raise MailTmError(f"{address} is not in {self.store}")
        return acct

    def _put(self, acct: dict) -> None:
        data = self._load()
        data["accounts"][acct["address"]] = acct
        self._save(data)

    # -- API -----------------------------------------------------------------
    def _req(self, method: str, path: str, token: str | None = None, **kw) -> dict:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        for attempt in range(4):
            r = self.http.request(method, path, headers=headers, **kw)
            if r.status_code == 429:  # mail.tm rate limit is ~8 req/s per IP
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise MailTmError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else {}
        raise MailTmError(f"{method} {path} -> rate limited")

    def domains(self) -> list[str]:
        data = self._req("GET", "/domains")
        items = data.get("hydra:member", data) if isinstance(data, dict) else data
        return [d["domain"] for d in items if d.get("isActive", True)]

    def create(self, username: str | None = None, password: str | None = None,
               domain: str | None = None) -> dict:
        """Register a new inbox, fetch its token, and persist it to the store."""
        domain = domain or self.domains()[0]
        username = username or "".join(secrets.choice(string.ascii_lowercase + string.digits)
                                       for _ in range(12))
        password = password or secrets.token_urlsafe(16)
        address = f"{username}@{domain}".lower()
        created = self._req("POST", "/accounts", json={"address": address, "password": password})
        acct = {
            "address": address,
            "password": password,
            "id": created.get("id"),
            "token": self._token(address, password),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "messages": [],
        }
        self._put(acct)
        return acct

    def _token(self, address: str, password: str) -> str:
        return self._req("POST", "/token", json={"address": address, "password": password})["token"]

    def _authed(self, address: str, method: str, path: str) -> dict:
        """Call with the stored token, refreshing it once if it has expired."""
        acct = self.get(address)
        try:
            return self._req(method, path, token=acct["token"])
        except MailTmError as e:
            if "-> 401" not in str(e):
                raise
            acct["token"] = self._token(acct["address"], acct["password"])
            self._put(acct)
            return self._req(method, path, token=acct["token"])

    def messages(self, address: str, full: bool = True) -> list[dict]:
        """List messages (newest first); with ``full`` also fetch each body and save it."""
        data = self._authed(address, "GET", "/messages")
        items = data.get("hydra:member", data) if isinstance(data, dict) else data
        if not full:
            return items
        acct = self.get(address)
        known = {m["id"]: m for m in acct.get("messages", [])}
        out = []
        for m in items:
            if m["id"] not in known:
                body = self._authed(address, "GET", f"/messages/{m['id']}")
                known[m["id"]] = {
                    "id": m["id"],
                    "from": (body.get("from") or {}).get("address"),
                    "subject": body.get("subject"),
                    "received_at": body.get("createdAt"),
                    "text": body.get("text") or "",
                    "otp": extract_otp(body),
                }
            out.append(known[m["id"]])
        acct = self.get(address)  # re-read: a token refresh may have rewritten it
        acct["messages"] = sorted(known.values(), key=lambda x: x["received_at"] or "", reverse=True)
        self._put(acct)
        return out

    def wait_for_otp(self, address: str, timeout: float = 120, interval: float = 3,
                     since_ids: set[str] | None = None) -> str:
        """Poll until a new message containing a code arrives; return the code."""
        seen = since_ids if since_ids is not None else set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for m in self.messages(address):
                if m["id"] not in seen and m["otp"]:
                    return m["otp"]
            time.sleep(interval)
        raise MailTmError(f"no code received at {address} within {timeout:.0f}s")

    def delete(self, address: str) -> None:
        acct = self.get(address)
        self._authed(address, "DELETE", f"/accounts/{acct['id']}")
        data = self._load()
        data["accounts"].pop(address, None)
        self._save(data)


def extract_otp(message: dict) -> str | None:
    """Pull the most likely one-time code out of a mail.tm message (subject first)."""
    html = message.get("html") or []
    html_text = re.sub(r"<[^>]+>", " ", " ".join(html) if isinstance(html, list) else html)
    for text in (message.get("subject") or "", message.get("text") or "", html_text):
        # Prefer a code right after a keyword like "code", "OTP", "verification".
        m = re.search(r"(?i)(?:code|otp|pin|verification|confirm)\D{0,40}?(\d{4,8})(?!\d)", text)
        if m:
            return m.group(1)
        m = OTP_RE.search(text)
        if m:
            return m.group(1)
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mailtm", description="Disposable mail.tm inboxes")
    p.add_argument("--store", default=DEFAULT_STORE, help=f"JSON store (default {DEFAULT_STORE})")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="create a new inbox")
    c.add_argument("--username")
    c.add_argument("--domain")
    sub.add_parser("list", help="list saved inboxes")
    i = sub.add_parser("inbox", help="fetch and print received messages")
    i.add_argument("address")
    o = sub.add_parser("otp", help="wait for a one-time code and print it")
    o.add_argument("address")
    o.add_argument("--timeout", type=float, default=120)
    o.add_argument("--new-only", action="store_true",
                   help="ignore codes from messages already in the inbox")
    d = sub.add_parser("delete", help="delete an inbox on mail.tm and from the store")
    d.add_argument("address")
    args = p.parse_args(argv)

    mt = MailTm(args.store)
    try:
        if args.cmd == "create":
            print(mt.create(username=args.username, domain=args.domain)["address"])
        elif args.cmd == "list":
            for addr, a in mt.accounts().items():
                print(f"{addr}\t{a['created_at']}\t{len(a.get('messages', []))} msgs")
        elif args.cmd == "inbox":
            print(json.dumps(mt.messages(args.address), indent=2, ensure_ascii=False))
        elif args.cmd == "otp":
            seen = {m["id"] for m in mt.messages(args.address, full=False)} if args.new_only else None
            print(mt.wait_for_otp(args.address, timeout=args.timeout, since_ids=seen))
        elif args.cmd == "delete":
            mt.delete(args.address)
    except MailTmError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
