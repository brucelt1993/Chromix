"""chromix.manager 测试：存储校验、启动参数、代理中继、HTTP 防护，以及可选的真机多开隔离验收。"""
import base64
import http.client
import json
import os
import socket
import socketserver
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chromix.manager import cdp, gateway  # noqa: E402
from chromix.manager.proxy_relay import ProxyRelay, _rewrite_head  # noqa: E402
from chromix.manager.runner import BrowserPool, build_launch, clear_stale_lock  # noqa: E402
from chromix.manager.server import Manager, make_handler  # noqa: E402
from chromix.manager.store import ProfileError, ProfileStore, validate  # noqa: E402


# -- store -----------------------------------------------------------------

def test_validate_rejects_managed_flags_and_bad_values():
    with pytest.raises(ProfileError):
        validate({"name": "a", "args": ["--user-data-dir=/tmp/x"]})
    with pytest.raises(ProfileError):
        validate({"name": "a", "args": ["--remote-debugging-port=9222"]})
    with pytest.raises(ProfileError):
        validate({"name": "a", "timezone": "Mars/Base"})
    with pytest.raises(ProfileError):
        validate({"name": "a", "platform": "android"})
    with pytest.raises(ProfileError):
        validate({"name": ""})
    with pytest.raises(ValueError):
        validate({"name": "a", "proxy": "ftp://host:21"})
    for arg in ("--Remote-Debugging-Address=0.0.0.0", "--remote-allow-origins=*", "--proxy-pac-url=x"):
        with pytest.raises(ProfileError):
            validate({"name": "a", "args": [arg]})
    with pytest.raises(ProfileError):
        validate({"name": "a", "proxy": "socks4://u:p@10.0.0.1:1080"})
    ok = validate({"name": " a ", "args": ["--fingerprint-hardware-concurrency=4", " "]})
    assert ok["name"] == "a" and ok["args"] == ["--fingerprint-hardware-concurrency=4"]


def test_store_template_fills_only_blank_fields_and_delete_removes_dir(tmp_path):
    store = ProfileStore(tmp_path)
    boss = store.create({"name": "boss-1", "template": "boss", "locale": "", "timezone": "Asia/Tokyo"})
    assert boss["locale"] == "zh-CN" and boss["timezone"] == "Asia/Tokyo"
    assert boss["start_url"] == "https://www.zhipin.com/"
    other = store.create({"name": "boss-2", "template": "boss"})
    assert store.user_data_dir(boss["id"]) != store.user_data_dir(other["id"])
    assert ProfileStore(tmp_path).get(boss["id"])["name"] == "boss-1"  # 已持久化
    marker = store.user_data_dir(boss["id"]) / "Cookies"
    marker.write_text("x")
    store.delete(boss["id"])
    assert not marker.exists()
    with pytest.raises(KeyError):
        store.get(boss["id"])
    with pytest.raises(ProfileError):
        store.create({"name": "x", "template": "nope"})


# -- launch args -----------------------------------------------------------

@pytest.fixture
def fake_binary(tmp_path, monkeypatch):
    binary = tmp_path / "chrome"
    binary.write_text("")
    monkeypatch.setenv("CLOAKBROWSER_BINARY_PATH", str(binary))
    monkeypatch.setenv("CLOAKBROWSER_WIDEVINE", "0")
    return binary


def _flags(argv):
    return {a.partition("=")[0]: a.partition("=")[2] for a in argv if a.startswith("--")}


def test_build_launch_isolates_profiles_with_distinct_dirs_and_seeds(tmp_path, fake_binary):
    store = ProfileStore(tmp_path / "home")
    a = store.create({"name": "a", "template": "boss", "platform": "windows"})
    b = store.create({"name": "b", "template": "boss"})
    argv_a, _, relay_a = build_launch(a, store.user_data_dir(a["id"]))
    argv_b, _, _ = build_launch(b, store.user_data_dir(b["id"]))
    fa, fb = _flags(argv_a), _flags(argv_b)
    assert argv_a[0] == str(fake_binary) and argv_a[-1] == "https://www.zhipin.com/"
    assert fa["--user-data-dir"] != fb["--user-data-dir"]
    assert fa["--remote-debugging-port"] == "0"
    assert fa["--fingerprint-platform"] == "windows"
    assert fa["--fingerprint-timezone"] == "Asia/Shanghai" and fa["--lang"] == "zh-CN"
    assert "--enable-automation" not in fa and relay_a is None
    assert fa["--disable-blink-features"] == "AutomationControlled"
    c = store.create({"name": "c", "args": ["--disable-blink-features=Foo"]})
    assert _flags(build_launch(c, store.user_data_dir(c["id"]))[0])["--disable-blink-features"] == \
        "Foo,AutomationControlled"
    seed_a = (store.user_data_dir(a["id"]) / ".chromix-fingerprint-seed").read_text().strip()
    assert fa["--fingerprint"] == seed_a
    # 同一 profile 再次启动种子不变 → 指纹稳定
    assert _flags(build_launch(a, store.user_data_dir(a["id"]))[0])["--fingerprint"] == seed_a


def test_build_launch_routes_proxy_credentials_to_relay(tmp_path, fake_binary):
    store = ProfileStore(tmp_path)
    plain = store.create({"name": "p", "proxy": "http://10.0.0.1:8080"})
    authed = store.create({"name": "q", "proxy": "http://u%40x:p%3Ass@10.0.0.1:8080"})
    argv, _, relay = build_launch(plain, store.user_data_dir(plain["id"]))
    assert _flags(argv)["--proxy-server"] == "http://10.0.0.1:8080" and relay is None
    argv, _, relay = build_launch(authed, store.user_data_dir(authed["id"]))
    assert "--proxy-server" not in _flags(argv)
    assert relay == {"server": "http://10.0.0.1:8080", "username": "u@x", "password": "p:ss"}
    assert not any("p:ss" in a or "p%3Ass" in a for a in argv)  # 凭据不出现在命令行


# -- proxy relay -----------------------------------------------------------

def test_rewrite_head_replaces_auth_and_closes_plain_http():
    head = b"GET http://a/ HTTP/1.1\r\nHost: a\r\nProxy-Authorization: Basic old\r\nConnection: keep-alive"
    out = _rewrite_head(head, b"TkVX")
    assert b"Basic old" not in out and out.count(b"Proxy-Authorization") == 1
    assert b"Connection: close" in out and out.endswith(b"\r\n\r\n")
    connect = _rewrite_head(b"CONNECT a:443 HTTP/1.1\r\nHost: a:443", b"TkVX")
    assert b"Connection: close" not in connect and b"Proxy-Authorization: Basic TkVX" in connect


def test_relay_forces_close_on_plain_http_response():
    class Upstream(socketserver.BaseRequestHandler):
        def handle(self):
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += self.request.recv(4096)
            self.request.sendall(b"HTTP/1.1 200 OK\r\nConnection: keep-alive\r\nContent-Length: 2\r\n\r\nok")

    upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    relay = ProxyRelay(f"http://127.0.0.1:{upstream.server_address[1]}", "u", "p")
    try:
        with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as client:
            client.sendall(b"GET http://a.test/ HTTP/1.1\r\nHost: a.test\r\n\r\n")
            data = b""
            while chunk := client.recv(4096):
                data += chunk
    finally:
        relay.close()
        upstream.shutdown()
        upstream.server_close()
    assert b"keep-alive" not in data and b"Connection: close" in data and data.endswith(b"\r\n\r\nok")


def test_clear_stale_lock(tmp_path):
    lock = tmp_path / "SingletonLock"
    host = socket.gethostname()

    def make(target):
        for name in ("SingletonLock", "SingletonSocket"):
            (tmp_path / name).unlink(missing_ok=True)
        lock.symlink_to(target)
        (tmp_path / "SingletonSocket").symlink_to("/tmp/x")

    make(f"{host}-999999")  # 持有进程已不存在
    assert clear_stale_lock(tmp_path) and not os.path.lexists(lock)
    assert not os.path.lexists(tmp_path / "SingletonSocket")
    make(f"other-host-{os.getpid()}")  # 其他主机的锁不动
    assert not clear_stale_lock(tmp_path) and os.path.lexists(lock)
    make(f"{host}-{os.getpid()}")  # pid 存活但不是使用该目录的浏览器（容器重建后 pid 被复用）
    removed = clear_stale_lock(tmp_path)
    assert removed == sys.platform.startswith("linux")  # 无 /proc 时无法确认，保守保留
    assert not clear_stale_lock(tmp_path / "missing")


def test_adopt_ignores_dead_or_mismatched_runtime(tmp_path):
    store = ProfileStore(tmp_path)
    p = store.create({"name": "a"})
    (store.user_data_dir(p["id"]) / "DevToolsActivePort").write_text("9\n/devtools/browser/x\n")
    (tmp_path / "runtime.json").write_text(json.dumps({p["id"]: {
        "pid": os.getpid(), "port": 9, "ws_path": "/devtools/browser/x", "relay_port": 0, "started_at": 0}}))
    pool = BrowserPool(store)  # 端口上没有对应浏览器 → 不接管
    assert pool.status(p["id"]) == {"state": "stopped"}
    assert json.loads((tmp_path / "runtime.json").read_text()) == {}


def test_relay_injects_credentials_and_tunnels_bytes():
    seen = []

    class Upstream(socketserver.BaseRequestHandler):
        def handle(self):
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += self.request.recv(4096)
            seen.append(buf.split(b"\r\n\r\n")[0])
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.request.sendall(b"echo:" + self.request.recv(4096))

    upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    relay = ProxyRelay(f"http://127.0.0.1:{upstream.server_address[1]}", "user", "p@ss")
    try:
        with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as client:
            client.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
            assert b"200" in client.recv(4096)
            client.sendall(b"ping")
            assert client.recv(4096) == b"echo:ping"
    finally:
        relay.close()
        upstream.shutdown()
        upstream.server_close()
    expected = base64.b64encode(b"user:p@ss")
    assert b"Proxy-Authorization: Basic " + expected in seen[0]


# -- HTTP API --------------------------------------------------------------

_MANAGERS = {}


def manager_of(port):
    return _MANAGERS[port]


@pytest.fixture
def api_server(tmp_path):
    manager = Manager(str(tmp_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(manager, {"127.0.0.1", "localhost"}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _MANAGERS[httpd.server_address[1]] = manager
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def _req(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Content-Type": "application/json"} if body is not None else {}
    hdrs.update(headers or {})
    conn.request(method, path, json.dumps(body) if body is not None else None, hdrs)
    res = conn.getresponse()
    data = res.read()
    conn.close()
    try:
        return res.status, json.loads(data)
    except ValueError:
        return res.status, data


def test_api_crud_and_cross_site_guards(api_server):
    status, page = _req(api_server, "GET", "/")
    assert status == 200 and b"Chromix" in page
    status, created = _req(api_server, "POST", "/api/profiles", {"name": "boss", "template": "boss"})
    assert status == 200 and created["status"]["state"] == "stopped"
    pid = created["id"]
    assert _req(api_server, "PUT", f"/api/profiles/{pid}", {"notes": "hr"})[1]["notes"] == "hr"
    assert _req(api_server, "GET", "/api/profiles")[1][0]["id"] == pid
    # 未运行时 cookie 操作给出明确错误
    assert _req(api_server, "GET", f"/api/profiles/{pid}/cookies")[0] == 400
    # 跨站防护：非 JSON 写请求、外站 Origin、伪造 Host
    conn = http.client.HTTPConnection("127.0.0.1", api_server, timeout=10)
    conn.request("POST", f"/api/profiles/{pid}/start", "{}", {"Content-Type": "text/plain"})
    assert conn.getresponse().status == 415
    conn.close()
    assert _req(api_server, "POST", "/api/stop-all", {}, {"Origin": "https://evil.example"})[0] == 403
    assert _req(api_server, "GET", "/api/profiles", headers={"Host": "evil.example"})[0] == 403
    assert _req(api_server, "GET", "/api/profiles/deadbeef")[0] == 404
    conn = http.client.HTTPConnection("127.0.0.1", api_server, timeout=10)
    conn.putrequest("POST", "/api/profiles")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", "-1")
    conn.endheaders()
    assert conn.getresponse().status == 400
    conn.close()
    # 代理密码不回传；提交打码值表示不修改
    proxied = _req(api_server, "PUT", f"/api/profiles/{pid}", {"proxy": "http://u:secret@10.0.0.1:8080"})[1]
    assert proxied["proxy"] == "http://u:******@10.0.0.1:8080"
    assert b"secret" not in json.dumps(_req(api_server, "GET", "/api/profiles")[1]).encode()
    _req(api_server, "PUT", f"/api/profiles/{pid}", {"proxy": proxied["proxy"], "notes": "x"})
    assert manager_of(api_server).store.get(pid)["proxy"] == "http://u:secret@10.0.0.1:8080"
    assert _req(api_server, "DELETE", f"/api/profiles/{pid}", {})[0] == 200
    assert _req(api_server, "GET", "/api/profiles")[1] == []


def test_api_token_required_when_configured(tmp_path):
    manager = Manager(str(tmp_path), token="s3cret")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(manager, {"127.0.0.1"}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        assert _req(port, "GET", "/api/profiles")[0] == 401
        assert _req(port, "GET", "/")[0] == 200  # 页面可打开，token 在页面里输入
        assert _req(port, "GET", "/api/profiles", headers={"X-Chromix-Token": "s3cret"})[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_gateway_helpers():
    assert gateway.public_bases({"Host": "vps.example:8820"}) == ("http://vps.example:8820", "ws://vps.example:8820")
    assert gateway.public_bases({"Host": "a.example", "X-Forwarded-Proto": "https"}) == \
        ("https://a.example", "wss://a.example")
    data = {"Browser": "x", "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/abc",
            "devtoolsFrontendUrl": "/devtools/inspector.html?ws=127.0.0.1:9333/x"}
    out = gateway.rewrite_discovery(data, "0123abcd", "wss://a.example", "t&k")
    assert out["webSocketDebuggerUrl"] == "wss://a.example/cdp/0123abcd/devtools/browser/abc?token=t%26k"
    assert "devtoolsFrontendUrl" not in out and "127.0.0.1" not in json.dumps(out)
    assert gateway.target_path("", "/devtools/browser/b") == "/devtools/browser/b"
    assert gateway.target_path("/devtools/page/ABC-1", "/x") == "/devtools/page/ABC-1"
    for bad in ("/../json", "/devtools/browser/a b", "//evil:22/", "/json/protocol"):
        assert gateway.target_path(bad, "/x") is None

    class FakeHandler:
        headers = {"Upgrade": "websocket", "Sec-WebSocket-Key": "a\r\n Origin: evil"}
    with pytest.raises(gateway.TunnelError):
        gateway.tunnel(FakeHandler(), 1, "/devtools/browser/x")  # 拒绝在连接上游之前


def test_gateway_requires_running_browser_and_token(tmp_path):
    manager = Manager(str(tmp_path), token="tok")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(manager, {"127.0.0.1"}))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        pid = manager.store.create({"name": "a"})["id"]
        assert _req(port, "GET", f"/cdp/{pid}/json/version")[0] == 401
        assert _req(port, "GET", f"/cdp/{pid}/json/version?token=tok")[0] == 409
        assert _req(port, "GET", f"/cdp/{pid}/json/version", headers={"X-Chromix-Token": "tok"})[0] == 409
        assert _req(port, "GET", "/cdp/deadbeef?token=tok")[0] == 404
        assert _req(port, "GET", f"/cdp/{pid}/json/version?tok%65n=tok")[0] == 409
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cookie_domain_matching():
    assert cdp.domain_matches(".zhipin.com", "zhipin.com")
    assert cdp.domain_matches("www.zhipin.com", "zhipin.com")
    assert not cdp.domain_matches("notzhipin.com", "zhipin.com")


# -- 真机验收：多开 + CDP + 登录隔离 ----------------------------------------

_BINARY = os.environ.get("CHROMIX_E2E_BINARY")


@pytest.mark.skipif(not _BINARY, reason="设置 CHROMIX_E2E_BINARY 指向 Chromix 可执行文件后运行")
def test_e2e_multi_open_cdp_and_cookie_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOAKBROWSER_BINARY_PATH", _BINARY)
    store = ProfileStore(tmp_path)
    pool = BrowserPool(store)
    a = store.create({"name": "A", "template": "boss", "headless": True, "start_url": "about:blank"})
    b = store.create({"name": "B", "template": "boss", "headless": True, "start_url": "about:blank"})
    try:
        sa, sb = pool.start(a["id"]), pool.start(b["id"])
        assert sa["state"] == sb["state"] == "running" and sa["cdp_port"] != sb["cdp_port"]
        login = {"name": "wt2", "value": "account-A", "domain": ".zhipin.com", "path": "/",
                 "secure": True, "httpOnly": True, "expires": time.time() + 86400}
        assert cdp.set_cookies(sa["cdp_port"], [login]) == 1
        assert [c["value"] for c in cdp.get_cookies(sa["cdp_port"], "zhipin.com")] == ["account-A"]
        assert cdp.get_cookies(sb["cdp_port"], "zhipin.com") == []  # B 看不到 A 的登录态
        with pytest.raises(ProfileError):
            pool.start(a["id"])  # 同一 profile 不可重复启动

        # 重启 A：登录态持久化在 A 自己的用户目录
        pool.stop(a["id"])
        assert pool.status(a["id"])["state"] == "stopped"
        sa = pool.start(a["id"])
        assert [c["value"] for c in cdp.get_cookies(sa["cdp_port"], "zhipin.com")] == ["account-A"]

        # 管理器重启后接管仍在运行的浏览器
        adopted = BrowserPool(store)
        assert adopted.status(a["id"])["cdp_port"] == sa["cdp_port"]

        assert cdp.delete_cookies(sa["cdp_port"], "zhipin.com") == 1
        assert cdp.get_cookies(sa["cdp_port"], "zhipin.com") == []

        # 标准 CDP 客户端（Playwright）可直接接入
        playwright = pytest.importorskip("playwright.sync_api")
        with playwright.sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(sa["cdp_http"])
            page = browser.contexts[0].new_page()
            page.set_content("<p>ok</p>")
            assert page.evaluate("navigator.webdriver") is False
            if os.environ.get("CHROMIX_E2E_PATCHED") == "1":  # 指纹人设需要带 Chromix 补丁的构建
                assert page.evaluate("navigator.language") == "zh-CN"
                assert page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") == "Asia/Shanghai"
            browser.close()
    finally:
        pool.stop_all()


@pytest.mark.skipif(not _BINARY, reason="设置 CHROMIX_E2E_BINARY 指向 Chromix 可执行文件后运行")
def test_e2e_remote_program_connects_through_cdp_gateway(tmp_path, monkeypatch):
    """模拟外部程序：只知道管理器地址和 token，先调 start，再经网关用 CDP 控制浏览器。"""
    monkeypatch.setenv("CLOAKBROWSER_BINARY_PATH", _BINARY)
    playwright = pytest.importorskip("playwright.sync_api")
    manager = Manager(str(tmp_path), token="s3cret")
    httpd = ThreadingHTTPServer(("0.0.0.0", 0), make_handler(manager, set()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    token = {"X-Chromix-Token": "s3cret"}
    try:
        pid = _req(port, "POST", "/api/profiles", {"name": "remote", "headless": True,
                                                    "start_url": "about:blank"}, token)[1]["id"]
        status = _req(port, "POST", f"/api/profiles/{pid}/start", {}, token)[1]
        assert status["cdp_remote"] == f"ws://127.0.0.1:{port}/cdp/{pid}"
        version = _req(port, "GET", f"/cdp/{pid}/json/version", headers=token)[1]
        assert version["webSocketDebuggerUrl"].startswith(f"ws://127.0.0.1:{port}/cdp/{pid}/devtools/browser/")
        assert str(status["cdp_port"]) not in json.dumps(version)  # 不泄露内部端口
        assert _req(port, "GET", f"/cdp/{pid}/json/version",
                    headers={**token, "Origin": "https://evil.example"})[0] == 403

        with playwright.sync_playwright() as pw:
            # 三种常见接入方式：HTTP 发现地址 + 请求头、ws 地址 + 请求头、ws 地址 + query token
            for endpoint, headers in ((status["cdp_remote_http"], token), (status["cdp_remote"], token),
                                      (status["cdp_remote"] + "?token=s3cret", None)):
                browser = pw.chromium.connect_over_cdp(endpoint, headers=headers)
                page = browser.contexts[0].new_page()
                page.set_content("<title>gateway-ok</title>")
                assert page.title() == "gateway-ok"
                assert page.evaluate("navigator.webdriver") is False
                page.close()
                browser.close()
            with pytest.raises(Exception):
                pw.chromium.connect_over_cdp(status["cdp_remote"], timeout=5000)  # 无 token 拒绝

        # page 级 ws 路径同样可经网关访问
        targets = _req(port, "GET", f"/cdp/{pid}/json/list?token=s3cret")[1]
        page_ws = [t for t in targets if t["type"] == "page"][0]["webSocketDebuggerUrl"]
        with cdp.CDPSession(page_ws) as session:
            assert session.call("Runtime.evaluate", {"expression": "1+1"})["result"]["value"] == 2
    finally:
        manager.pool.stop_all()
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.skipif(not _BINARY, reason="设置 CHROMIX_E2E_BINARY 指向 Chromix 可执行文件后运行")
def test_e2e_authenticated_http_proxy_through_relay(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOAKBROWSER_BINARY_PATH", _BINARY)
    expected = b"Proxy-Authorization: Basic " + base64.b64encode(b"hr:p@ss")

    class Upstream(socketserver.BaseRequestHandler):
        """假上游代理：鉴权正确才返回页面，否则 407。"""
        def handle(self):
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                buf += chunk
            body = b"<title>via-proxy</title>" if expected in buf else b""
            status = b"200 OK" if body else b"407 Proxy Authentication Required"
            self.request.sendall(b"HTTP/1.1 " + status + b"\r\nContent-Type: text/html\r\nContent-Length: "
                                 + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)

    upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    store = ProfileStore(tmp_path)
    pool = BrowserPool(store)
    p = store.create({"name": "proxy", "headless": True, "start_url": "about:blank",
                      "proxy": f"http://hr:p%40ss@127.0.0.1:{upstream.server_address[1]}"})
    try:
        status = pool.start(p["id"])
        playwright = pytest.importorskip("playwright.sync_api")
        with playwright.sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(status["cdp_http"])
            page = browser.contexts[0].new_page()
            page.goto("http://jobs.example.test/")
            assert page.title() == "via-proxy"
            browser.close()
    finally:
        pool.stop_all()
        upstream.shutdown()
        upstream.server_close()
