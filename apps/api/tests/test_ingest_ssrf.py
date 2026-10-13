"""SSRF guard tests for fetch_url_text (specs/08 SEC-06 §4.5). Offline: DNS + HTTP mocked."""
import socket

import httpx
import pytest

from app.services import ingest as ing


class FakeResp:
    def __init__(self, status=200, content=b"hello", headers=None, encoding="utf-8"):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self.encoding = encoding

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"status {self.status_code}", request=None, response=None)  # type: ignore[arg-type]


class FakeClient:
    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list = []

    async def get(self, url, **kwargs):
        self.calls.append(url)
        assert kwargs.get("follow_redirects") is False, "must not auto-follow (rebinding check)"
        if url in self.routes:
            r = self.routes[url]
            if isinstance(r, Exception):
                raise r
            return r
        return FakeResp(status=404, content=b"nope")


def _pub(monkeypatch, ip="93.184.216.34"):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (ip, 0))])


def _priv(monkeypatch, ip="127.0.0.1"):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (ip, 0))])


async def test_scheme_blocked():
    with pytest.raises(ValueError, match="URL_FETCH_BLOCKED"):
        await ing.fetch_url_text("file:///etc/passwd", client=FakeClient({}))
    with pytest.raises(ValueError, match="URL_FETCH_BLOCKED"):
        await ing.fetch_url_text("ftp://example.com/x", client=FakeClient({}))
    with pytest.raises(ValueError, match="URL_FETCH_BLOCKED"):
        await ing.fetch_url_text("", client=FakeClient({}))


async def test_localhost_blocked_no_dns(monkeypatch):
    # localhost short-circuits before DNS
    with pytest.raises(ValueError, match="URL_FETCH_BLOCKED"):
        await ing.fetch_url_text("http://localhost/admin", client=FakeClient({}))


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1"])
async def test_private_ips_blocked(monkeypatch, ip):
    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(fam, 1, 6, "", (ip, 0))])
    with pytest.raises(ValueError, match="URL_FETCH_BLOCKED"):
        await ing.fetch_url_text(f"http://internal-{ip}.test/", client=FakeClient({}))


async def test_public_fetch_ok(monkeypatch):
    _pub(monkeypatch)
    c = FakeClient({"https://example.com/a": FakeResp(content=b"hello world")})
    assert await ing.fetch_url_text("https://example.com/a", client=c) == "hello world"


async def test_redirect_to_private_blocked(monkeypatch):
    _pub(monkeypatch)

    orig = ing._assert_public_host
    calls = []

    def spy(host):
        calls.append(host)
        if "evil" in host:
            raise ValueError("URL_FETCH_BLOCKED: host not allowed")
        return orig(host)

    monkeypatch.setattr(ing, "_assert_public_host", spy)
    c = FakeClient({
        "http://example.com/start": FakeResp(status=302, headers={"location": "http://evil.internal/secret"}),
    })
    with pytest.raises(ValueError, match="URL_FETCH_BLOCKED"):
        await ing.fetch_url_text("http://example.com/start", client=c)
    assert calls[0] == "example.com"


async def test_too_many_redirects(monkeypatch):
    _pub(monkeypatch)
    routes = {}
    cur = "http://example.com/0"
    for i in range(5):
        nxt = f"http://example.com/{i + 1}"
        routes[cur] = FakeResp(status=302, headers={"location": nxt})
        cur = nxt
    with pytest.raises(ValueError, match="URL_FETCH_BLOCKED: too many redirects"):
        await ing.fetch_url_text("http://example.com/0", client=FakeClient(routes))


async def test_timeout_maps_failed(monkeypatch):
    _pub(monkeypatch)
    c = FakeClient({"https://example.com/a": httpx.TimeoutException("t")})
    with pytest.raises(ValueError, match="URL_FETCH_FAILED"):
        await ing.fetch_url_text("https://example.com/a", client=c)


async def test_dns_failure_maps_failed(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("nope")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(ValueError, match="URL_FETCH_FAILED"):
        await ing.fetch_url_text("https://nonexistent-xyz.test/", client=FakeClient({}))
