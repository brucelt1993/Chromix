"""管理器 HTTP 服务：REST API + 单页管理界面（纯标准库）。

安全边界：默认只监听 127.0.0.1。被管理的浏览器会访问任意网站，所以
API 拒绝跨站请求（校验 Host/Origin，写操作强制 application/json 触发预检），
监听非回环地址时必须配置 token。
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlsplit

from .. import api
from . import cdp, gateway
from .runner import BrowserPool
from .store import TEMPLATES, ProfileError, ProfileStore

_ROUTE = re.compile(r"^/api/profiles/([0-9a-f]{8})(?:/(start|stop|cookies|log))?$")
_MAX_BODY = 8 * 1024 * 1024


class _HTTPError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


_MASK = "******"


def mask_proxy(proxy: str) -> str:
    """API 不回传代理密码；编辑时原样提交打码值即表示不修改。"""
    url = urlsplit(proxy if "://" in proxy else "http://" + proxy) if proxy else None
    if not url or url.password is None:
        return proxy
    return proxy.replace(":" + url.password + "@", ":" + _MASK + "@", 1)


class Manager:
    def __init__(self, home: str, token: str | None = None):
        self.store = ProfileStore(home)
        self.pool = BrowserPool(self.store)
        self.token = token

    def profile_view(self, profile: dict, bases: tuple[str, str]) -> dict:
        profile = {**profile, "proxy": mask_proxy(profile["proxy"])}
        seed_file = self.store.user_data_dir(profile["id"]) / api._PROFILE_SEED_FILE
        try:
            seed = int(seed_file.read_text())
        except (OSError, ValueError):
            seed = None  # 首次启动时生成
        return {**profile, "seed": seed, "user_data_dir": str(self.store.user_data_dir(profile["id"])),
                "status": self.status(profile["id"], bases)}

    def status(self, profile_id: str, bases: tuple[str, str]) -> dict:
        """运行中的实例额外给出经网关的外部 CDP 地址。"""
        status = self.pool.status(profile_id)
        if status["state"] == "running":
            status.update(gateway.remote_urls(profile_id, bases[1], bases[0]))
        return status


def make_handler(manager: Manager, allowed_hosts: set[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ChromixManager/1.0"

        def log_message(self, fmt, *args):
            message = re.sub(r"\?[^\s\"]*", "?…", fmt % args)  # 不记录 query，避免 token 进日志
            sys.stderr.write("[manager] %s %s\n" % (self.address_string(), message))

        # -- 基础 ------------------------------------------------------
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _guard(self, mutating: bool, need_token: bool = True, query_token: str | None = None) -> None:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
            # 有 token 时 token 本身挡住 DNS rebinding；无 token 只认回环 Host。
            if not manager.token and host not in allowed_hosts:
                raise _HTTPError(403, "Host 不被允许")
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
                raise _HTTPError(403, "拒绝跨站请求")
            if mutating and not (self.headers.get("Content-Type") or "").startswith("application/json"):
                raise _HTTPError(415, "需要 Content-Type: application/json")
            if manager.token and need_token:
                given = self.headers.get("X-Chromix-Token") or query_token or ""
                if not hmac.compare_digest(given.encode(), manager.token.encode()):
                    raise _HTTPError(401, "token 无效")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0:
                raise _HTTPError(400, "Content-Length 非法")
            if length > _MAX_BODY:
                raise _HTTPError(413, "请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                raise _HTTPError(400, "JSON 格式错误") from None
            if not isinstance(data, dict):
                raise _HTTPError(400, "请求体必须是 JSON 对象")
            return data

        def _dispatch(self, method: str) -> None:
            url = urlsplit(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if method == "GET" and url.path in ("/", "/index.html"):
                    self._guard(False, need_token=False)  # 页面本身不含数据，token 在页面里输入
                    page = resources.files(__package__).joinpath("static/index.html").read_bytes()
                    return self._send(200, page, "text/html; charset=utf-8")
                match = gateway.CDP_ROUTE.match(url.path)
                if match and method == "GET":
                    self._guard(False, query_token=query.get("token"))
                    return self._cdp(match.group(1), match.group(2) or "", query.get("token"))
                if not url.path.startswith("/api/"):
                    raise _HTTPError(404, "未找到")
                self._guard(method != "GET")
                result = self._api(method, url.path, query)
                self._json(200, result)
            except _HTTPError as error:
                self._json(error.status, {"error": str(error)})
            except KeyError:
                self._json(404, {"error": "profile 不存在"})
            except (ProfileError, ValueError) as error:  # 含 gateway.TunnelError
                self._json(400, {"error": str(error)})
            except cdp.CDPError as error:
                self._json(502, {"error": f"CDP 调用失败：{error}"})
            except Exception as error:  # 兜底，避免单个请求打挂服务
                self._json(500, {"error": f"{type(error).__name__}: {error}"})

        def _bases(self) -> tuple[str, str]:
            return gateway.public_bases(self.headers)

        def _cdp(self, profile_id: str, suffix: str, query_token: str | None) -> None:
            manager.store.get(profile_id)
            status = manager.pool.status(profile_id)
            if status["state"] != "running":
                raise _HTTPError(409, "浏览器未运行，请先调用 start")
            port = status["cdp_port"]
            discovery = gateway.DISCOVERY.get(suffix.rstrip("/") or "/")
            if discovery:
                data = cdp.fetch_json(port, discovery)
                ws_base = self._bases()[1]
                return self._json(200, gateway.rewrite_discovery(data, profile_id, ws_base, query_token))
            if (self.headers.get("Upgrade") or "").lower() != "websocket":
                raise _HTTPError(400, "需要 WebSocket 升级请求，或访问 /json/version")
            path = gateway.target_path(suffix, urlsplit(status["cdp_ws"]).path)
            if not path:
                raise _HTTPError(404, "未知的 CDP 路径")
            self.close_connection = True
            gateway.tunnel(self, port, path)

        def _api(self, method: str, path: str, query: dict):
            if path == "/api/info" and method == "GET":
                return {"home": str(manager.store.home), "templates": TEMPLATES,
                        "binary": api.binary_info(), "token_required": bool(manager.token)}
            if path == "/api/profiles":
                if method == "GET":
                    return [manager.profile_view(p, self._bases()) for p in manager.store.list()]
                if method == "POST":
                    return manager.profile_view(manager.store.create(self._body()), self._bases())
            if path == "/api/stop-all" and method == "POST":
                manager.pool.stop_all()
                return {"ok": True}
            match = _ROUTE.match(path)
            if not match:
                raise _HTTPError(404, "未找到")
            profile_id, action = match.groups()
            manager.store.get(profile_id)  # 不存在时抛 KeyError → 404
            if action is None:
                if method == "GET":
                    return manager.profile_view(manager.store.get(profile_id), self._bases())
                if method == "PUT":
                    data = self._body()
                    current = manager.store.get(profile_id)["proxy"]
                    if data.get("proxy") == mask_proxy(current):
                        data["proxy"] = current
                    return manager.profile_view(manager.store.update(profile_id, data), self._bases())
                if method == "DELETE":
                    manager.pool.delete(profile_id)
                    return {"ok": True}
            elif action == "start" and method == "POST":
                manager.pool.start(profile_id)
                return manager.status(profile_id, self._bases())
            elif action == "stop" and method == "POST":
                return manager.pool.stop(profile_id)
            elif action == "log" and method == "GET":
                return {"log": manager.pool.log_tail(profile_id)}
            elif action == "cookies":
                port = manager.pool.running_port(profile_id)
                domain = query.get("domain", "").strip()
                if method == "GET":
                    return {"cookies": cdp.get_cookies(port, domain or None)}
                if method == "DELETE":
                    if not domain:
                        raise ProfileError("删除 cookie 必须指定 domain")
                    return {"deleted": cdp.delete_cookies(port, domain)}
                if method == "POST":
                    cookies = self._body().get("cookies")
                    if not isinstance(cookies, list):
                        raise ProfileError("需要 cookies 数组")
                    return {"imported": cdp.set_cookies(port, cookies)}
            raise _HTTPError(405, "方法不支持")

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def serve(home: str, host: str = "127.0.0.1", port: int = 8820, token: str | None = None) -> None:
    if not _is_loopback(host) and not token:
        raise SystemExit("监听非回环地址时必须通过 --token 设置访问令牌")
    manager = Manager(home, token)
    allowed = {"127.0.0.1", "localhost", "[::1]", host.lower()}
    httpd = ThreadingHTTPServer((host, port), make_handler(manager, allowed))
    httpd.daemon_threads = True
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"Chromix 管理器已启动：http://{shown}:{httpd.server_address[1]}/  数据目录：{manager.store.home}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        print("管理器已退出；仍在运行的浏览器保持打开，下次启动管理器会自动接管。")
