"""mitmdump addon: log one line per flow with the facts we mine for endpoints."""
from mitmproxy import http


def response(flow: http.HTTPFlow) -> None:
    r = flow.request
    ct = (flow.response.headers.get("content-type", "") if flow.response else "")[:40]
    status = flow.response.status_code if flow.response else 0
    # host | method | path (no query) | status | content-type | has-body
    print("FLOW\t%s\t%s\t%s\t%d\t%s\t%s" % (
        r.host, r.method, r.path.split("?")[0], status, ct,
        "B" if r.raw_content else "-"), flush=True)
