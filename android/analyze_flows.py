#!/usr/bin/env python3
"""Mine a mitmproxy capture for endpoints.

The Amazon app negotiates `accept-encoding: snappy`, which mitmproxy can't
decode on its own, so response bodies come back raw; this tool tries the Snappy
variants (via cramjam) to recover JSON where it can.

    python android/analyze_flows.py android/flows/session.mitm            # endpoint table
    python android/analyze_flows.py android/flows/session.mitm --host data.amazon.com
    python android/analyze_flows.py android/flows/session.mitm --grep /suggestions --show 1
"""
import argparse
import collections
import json

from mitmproxy.io import FlowReader

try:
    import cramjam
except ImportError:
    cramjam = None


def decode_body(resp):
    """Best-effort decode of a (possibly Snappy) response body -> bytes."""
    if resp is None:
        return b""
    raw = resp.raw_content or b""
    enc = resp.headers.get("content-encoding", "").lower()
    if "snappy" in enc and cramjam is not None:
        for fn in ("decompress", "decompress_raw"):
            try:
                return bytes(getattr(cramjam.snappy, fn)(raw))
            except Exception:
                pass
    try:
        return resp.content or raw   # mitmproxy handles gzip/br/zstd/deflate
    except Exception:
        return raw


def flows(path):
    with open(path, "rb") as fh:
        for fl in FlowReader(fh).stream():
            if hasattr(fl, "request"):
                yield fl


def table(path):
    agg = collections.OrderedDict()
    for fl in flows(path):
        r = fl.request
        key = (r.host, r.method, r.path.split("?")[0])
        st = agg.setdefault(key, {"n": 0, "status": set(), "ct": set()})
        st["n"] += 1
        if fl.response:
            st["status"].add(fl.response.status_code)
            st["ct"].add(fl.response.headers.get("content-type", "").split(";")[0])
    rows = sorted(agg.items(), key=lambda kv: (-kv[1]["n"], kv[0]))
    print(f"{'n':>4}  {'status':<10} {'host':<26} path")
    for (host, method, p), st in rows:
        sts = ",".join(str(s) for s in sorted(st["status"])) or "-"
        print(f"{st['n']:>4}  {sts:<10} {host:<26} {method} {p[:64]}")


def detail(path, host=None, grep=None, show=0):
    shown = 0
    for fl in flows(path):
        r = fl.request
        if host and r.host != host:
            continue
        if grep and grep not in r.pretty_url:
            continue
        print("=" * 78)
        print(f"{r.method} {r.pretty_url}")
        if fl.response:
            print(f"-> {fl.response.status_code} "
                  f"{fl.response.headers.get('content-type','')} "
                  f"(enc={fl.response.headers.get('content-encoding','-')})")
        if show and shown < show:
            print("- request headers:")
            for k, v in r.headers.items():
                print(f"    {k}: {v if len(v) < 120 else v[:117] + '...'}")
            if r.raw_content:
                print("- request body:", r.get_text()[:600])
            body = decode_body(fl.response)
            print(f"- response body ({len(body)} bytes decoded):")
            try:
                print("  " + json.dumps(json.loads(body), indent=2)[:1500])
            except Exception:
                print("  " + repr(body[:600]))
            shown += 1
    if grep or host:
        print("=" * 78)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mitm")
    ap.add_argument("--host")
    ap.add_argument("--grep", help="substring that must appear in the URL")
    ap.add_argument("--show", type=int, default=0, help="dump N full req/resp bodies")
    a = ap.parse_args()
    if a.host or a.grep:
        detail(a.mitm, a.host, a.grep, a.show)
    else:
        table(a.mitm)
