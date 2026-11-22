"""Tests for the User-Agent fallback chain in pipeline.fetch_page.

Run: .venv/bin/python -m pytest tests/test_fetch_fallback.py -q
No network: httpx.Client is wrapped so every request goes through an httpx.MockTransport.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import _env  # noqa: E402,F401  (fake tool paths + no-network guard)
import pipeline as P  # noqa: E402

CHROME = P.HEADERS["User-Agent"]
CURL = "curl/8.5.0"
HTTPX = "python-httpx/0.28"
HTML = b"<html><body>info@example.ca</body></html>"


@pytest.fixture
def server(monkeypatch):
    """Route every httpx request through `handler(request) -> httpx.Response` and record UAs."""
    seen: list[str] = []
    state: dict = {"handler": None}
    real_client = httpx.Client

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda req: _dispatch(req))
        return real_client(**kwargs)

    def _dispatch(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("user-agent", ""))
        return state["handler"](req)

    monkeypatch.setattr(httpx, "Client", factory)
    return {"seen": seen, "state": state}


def ok_html(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=HTML, headers={"content-type": "text/html; charset=utf-8"},
                          request=req)


def reset(req: httpx.Request) -> httpx.Response:
    raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=req)


def test_chrome_success_no_fallback(server):
    server["state"]["handler"] = ok_html
    text, final = P.fetch_page("https://bravehomes.example/contact-us/")
    assert "info@example.ca" in text
    assert final == "https://bravehomes.example/contact-us/"
    assert server["seen"] == [CHROME]          # exactly one request, Chrome UA, no retry


def test_fallback_fires_only_on_transport_failure(server):
    def handler(req):
        if req.headers["user-agent"] == CHROME:
            return reset(req)
        return ok_html(req)
    server["state"]["handler"] = handler
    text, final = P.fetch_page("https://rsarchitecture.example/")
    assert "info@example.ca" in text
    assert final == "https://rsarchitecture.example/"
    assert server["seen"] == [CHROME, "curl/8.5.0"]   # second UA succeeded, third never tried


def test_second_fallback_python_httpx_used_when_curl_also_fails(server):
    def handler(req):
        if req.headers["user-agent"] == "python-httpx/0.28":
            return ok_html(req)
        return reset(req)
    server["state"]["handler"] = handler
    text, _ = P.fetch_page("https://x.example/")
    assert "info@example.ca" in text
    assert server["seen"] == [CHROME, "curl/8.5.0", "python-httpx/0.28"]


def test_all_user_agents_fail_raises_last_error(server):
    server["state"]["handler"] = reset
    with pytest.raises(httpx.RemoteProtocolError):
        P.fetch_page("https://x.example/")
    assert server["seen"] == [CHROME, "curl/8.5.0", "python-httpx/0.28"]


def test_genuine_404_raises_without_fallback(server):
    server["state"]["handler"] = lambda req: httpx.Response(404, request=req)
    with pytest.raises(httpx.HTTPStatusError) as exc:
        P.fetch_page("https://x.example/missing")
    assert exc.value.response.status_code == 404
    assert server["seen"] == [CHROME]          # a 404 is an answer, not a transport failure


def test_403_retries_every_ua_then_raises(server):
    """403 means 'we do not like this client', not 'gone': another UA gets the page
    (norvale.com is the real case: 403 on Chrome, 200 on curl). A 404 is different and
    is covered by its own test."""
    server["state"]["handler"] = lambda req: httpx.Response(403, request=req)
    with pytest.raises(httpx.HTTPStatusError):
        P.fetch_page("https://ironwood.example/")
    assert server["seen"] == [CHROME, CURL, HTTPX]


def test_403_recovered_by_second_user_agent(server):
    def handler(req):
        if req.headers.get("user-agent") == CHROME:
            return httpx.Response(403, request=req)
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text="<html><body>info@norvale.example</body></html>",
                              request=req)
    server["state"]["handler"] = handler
    html, _ = P.fetch_page("https://norvale.example/people/")
    assert "info@norvale.example" in html
    assert server["seen"] == [CHROME, CURL]


def test_non_html_still_rejected_and_not_retried(server):
    server["state"]["handler"] = lambda req: httpx.Response(
        200, content=b"%PDF", headers={"content-type": "application/pdf"}, request=req)
    with pytest.raises(ValueError):
        P.fetch_page("https://x.example/a.pdf")
    assert server["seen"] == [CHROME]


def test_redirect_final_url_returned(server):
    def handler(req):
        if req.url.path == "/":
            return httpx.Response(301, headers={"location": "https://amanda.example/home"}, request=req)
        return ok_html(req)
    server["state"]["handler"] = handler
    text, final = P.fetch_page("https://amanda.example/")
    assert final == "https://amanda.example/home"
    assert "info@example.ca" in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
