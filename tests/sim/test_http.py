import json

import httpx
import pytest

from tools.sim import http as simhttp
from tools.sim.http import check_url

ALLOWED = frozenset({"127.0.0.1", "localhost"})


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://127.0.0.1/x",
        "https://example.com/",
        "http://user:pw@127.0.0.1/",
        "http://127.0.0.1.evil.com/",
        "http://[::1]/",
        "http:///nohost",
    ],
)
def test_check_url_refuses(url: str):
    with pytest.raises(ValueError):
        check_url(url, ALLOWED)


def test_check_url_allows_local_and_listed_hosts():
    check_url("http://127.0.0.1:8100/x?y=1", ALLOWED)
    check_url("https://localhost/", ALLOWED)
    check_url("http://[::1]:80/", frozenset({"::1"}))
    check_url("http://fleet.test/", frozenset({"fleet.test"}))


def test_allowed_hosts_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SIM_ALLOWED_HOSTS", raising=False)
    assert simhttp.allowed_hosts() == ALLOWED
    monkeypatch.setenv("SIM_ALLOWED_HOSTS", "a.test, b.test")
    assert simhttp.allowed_hosts() == frozenset({"a.test", "b.test"})


def _client(handler):  # type: ignore[no-untyped-def]
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_get_prints_status_headers_and_body(capsys: pytest.CaptureFixture[str]):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="hello",
            headers={"content-type": "text/plain", "set-cookie": "sid=SECRET; Path=/"},
        )

    rc = simhttp.main(["GET", "http://127.0.0.1:1/x"], client=_client(handler))
    out = capsys.readouterr().out
    assert rc == 0 and "200" in out and "text/plain" in out and "hello" in out
    assert "sid" in out and "SECRET" not in out


def test_post_json_and_form(capsys: pytest.CaptureFixture[str]):
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(201, text="ok")

    simhttp.main(["POST", "http://127.0.0.1:1/x", "--json", '{"a": 1}'], client=_client(handler))
    assert json.loads(seen[0].content) == {"a": 1} and seen[0].method == "POST"
    simhttp.main(
        ["POST", "http://localhost:1/x", "--form", "u=a", "--form", "p=b"], client=_client(handler)
    )
    assert seen[1].content == b"u=a&p=b"


def test_redirect_not_followed_location_printed(capsys: pytest.CaptureFixture[str]):
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(302, headers={"location": "http://evil.example/steal"})

    rc = simhttp.main(["GET", "http://127.0.0.1:1/x"], client=_client(handler))
    out = capsys.readouterr().out
    assert rc == 0 and "302" in out and "http://evil.example/steal" in out
    assert len(calls) == 1


def test_body_truncated_to_20000_bytes(capsys: pytest.CaptureFixture[str]):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="a" * 30000)

    simhttp.main(["GET", "http://127.0.0.1:1/"], client=_client(handler))
    body = capsys.readouterr().out.split("\n\n", 1)[1]
    assert body.strip() == "a" * 20000


def test_refused_url_exits_2_without_request(capsys: pytest.CaptureFixture[str]):
    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not be called")

    rc = simhttp.main(["GET", "https://example.com/"], client=_client(handler))
    assert rc == 2
    assert "refused" in capsys.readouterr().err


def test_cookie_jar_roundtrip(tmp_path, monkeypatch, capsys):  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    jar = tmp_path / "jar.json"
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(200, headers={"set-cookie": "sid=abc; Path=/"})
        assert req.headers.get("cookie") == "sid=abc"
        return httpx.Response(200, text="again")

    simhttp.main(["GET", "http://127.0.0.1:1/", "--cookie-jar", str(jar)], client=_client(handler))
    assert json.loads(jar.read_text()) == {"sid": "abc"}
    simhttp.main(["GET", "http://127.0.0.1:1/", "--cookie-jar", str(jar)], client=_client(handler))
    assert state["n"] == 2


def test_cookie_jar_outside_cwd_refused(tmp_path, monkeypatch, capsys):  # type: ignore[no-untyped-def]
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not be called")

    for bad in (tmp_path / "jar.json", work / ".." / "jar.json", "/etc/jar.json"):
        rc = simhttp.main(
            ["GET", "http://127.0.0.1:1/", "--cookie-jar", str(bad)], client=_client(handler)
        )
        assert rc == 2
    assert "cookie-jar" in capsys.readouterr().err
    assert not (tmp_path / "jar.json").exists()


def test_cookie_jar_symlink_escape_refused(tmp_path, monkeypatch, capsys):  # type: ignore[no-untyped-def]
    work = tmp_path / "work"
    work.mkdir()
    (work / "link.json").symlink_to(tmp_path / "outside.json")
    monkeypatch.chdir(work)
    rc = simhttp.main(
        ["GET", "http://127.0.0.1:1/", "--cookie-jar", "link.json"],
        client=_client(lambda r: httpx.Response(200)),
    )
    assert rc == 2


def test_invalid_url_is_clean_error(capsys: pytest.CaptureFixture[str]):
    rc = simhttp.main(
        ["GET", "http://127.0.0.1:abc/x"], client=_client(lambda r: httpx.Response(200))
    )
    err = capsys.readouterr().err
    assert rc == 2 and err.startswith("error:") and "Traceback" not in err
    assert len(err.strip().splitlines()) == 1


def test_request_error_is_clean_error(capsys: pytest.CaptureFixture[str]):
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    rc = simhttp.main(["GET", "http://127.0.0.1:1/"], client=_client(handler))
    err = capsys.readouterr().err
    assert rc == 1 and err.startswith("error:") and "Traceback" not in err


def test_unexpected_httpx_error_is_clean(capsys: pytest.CaptureFixture[str]):
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.UnsupportedProtocol("nope")

    rc = simhttp.main(["GET", "http://127.0.0.1:1/"], client=_client(handler))
    assert rc == 1 and "Traceback" not in capsys.readouterr().err
