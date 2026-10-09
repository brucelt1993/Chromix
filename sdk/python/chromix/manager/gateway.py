"""CDP 网关：外部程序经管理器端口（token 鉴权）连接各 profile 的浏览器。

浏览器的调试端口只监听 127.0.0.1 且没有鉴权，不能直接暴露。网关完成鉴权后
把 WebSocket 升级请求转发给对应浏览器，之后双向透传字节流，不解析 CDP 协议，
因此 Playwright / Puppeteer / 任意 CDP 客户端都能直接使用。
只转发到管理器自己启动的浏览器，不能被用来访问本机其他端口。
"""
from __future__ import annotations

import re
import socket
import threading
from urllib.parse import quote, urlsplit

CDP_ROUTE = re.compile(r"^/cdp/([0-9a-f]{8})(/.*)?$")
DISCOVERY = {"/json/version": "/json/version", "/json/list": "/json/list", "/json": "/json/list"}
_DEVTOOLS_PATH = re.compile(r"^/devtools/(?:browser|page)/[A-Za-z0-9-]{1,64}$")
# 只把 WebSocket 握手必需的头转发给浏览器（白名单），Host 改写为本机端口，token/Origin 不外泄。
_FORWARD = {"upgrade", "connection", "sec-websocket-key", "sec-websocket-version",
            "sec-websocket-extensions", "sec-websocket-protocol"}


class TunnelError(ValueError):
    pass


def public_bases(headers) -> tuple[str, str]:
    """按请求推导外部可访问的 (http_base, ws_base)，兼容 Caddy/Nginx 反代的 X-Forwarded-Proto。"""
    proto = (headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip().lower()
    proto = "https" if proto == "https" else "http"
    host = headers.get("X-Forwarded-Host") or headers.get("Host") or "127.0.0.1"
    host = host.split(",")[0].strip()
    return f"{proto}://{host}", ("wss" if proto == "https" else "ws") + f"://{host}"


def remote_urls(profile_id: str, ws_base: str, http_base: str) -> dict:
    return {"cdp_remote": f"{ws_base}/cdp/{profile_id}",
            "cdp_remote_http": f"{http_base}/cdp/{profile_id}"}


def rewrite_discovery(data, profile_id: str, ws_base: str, token: str | None):
    """把 /json/* 中浏览器本机的 ws 地址改写为网关地址。"""
    suffix = f"?token={quote(token, safe='')}" if token else ""

    def fix(item: dict) -> dict:
        item = dict(item)
        if "webSocketDebuggerUrl" in item:
            path = urlsplit(item["webSocketDebuggerUrl"]).path
            item["webSocketDebuggerUrl"] = f"{ws_base}/cdp/{profile_id}{path}{suffix}"
        item.pop("devtoolsFrontendUrl", None)  # 指向本机端口，外部不可用
        item.pop("devtoolsFrontendUrlCompat", None)
        return item

    return [fix(i) for i in data] if isinstance(data, list) else fix(data)


def target_path(suffix: str, browser_ws_path: str) -> str | None:
    """客户端请求的 /cdp/<id> 之后的部分 → 浏览器上的 ws 路径；非法时返回 None。"""
    if suffix in ("", "/"):
        return browser_ws_path
    return suffix if _DEVTOOLS_PATH.match(suffix) else None


def tunnel(handler, port: int, path: str) -> None:
    """把当前 HTTP 连接的 WebSocket 升级请求转发到 127.0.0.1:port，然后双向透传。"""
    lines = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}"]
    for key, value in handler.headers.items():
        if key.lower() in _FORWARD:
            if "\r" in value or "\n" in value:
                raise TunnelError("请求头非法")
            lines.append(f"{key}: {value}")
    upstream = socket.create_connection(("127.0.0.1", port), timeout=10)
    handler.log_request(101)
    upstream.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    upstream.settimeout(None)
    client = handler.connection
    client.settimeout(None)
    for sock in (client, upstream):
        _keepalive(sock)

    def client_to_upstream():
        try:
            # read1 先吐出 rfile 已缓冲的字节，避免握手后紧跟的数据丢失
            while data := handler.rfile.read1(65536):
                upstream.sendall(data)
        except OSError:
            pass
        finally:
            _shutdown(upstream)

    worker = threading.Thread(target=client_to_upstream, daemon=True)
    worker.start()
    try:
        while data := upstream.recv(65536):
            client.sendall(data)
    except OSError:
        pass
    finally:
        _shutdown(client)
        _shutdown(upstream)
        worker.join(5)
        upstream.close()


def _keepalive(sock: socket.socket) -> None:
    """对端断网不发 FIN 时，靠 TCP keepalive 在约 2 分钟内探测到并结束隧道，避免线程泄漏。"""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
    for option, value in ((idle, 60), (getattr(socket, "TCP_KEEPINTVL", None), 15),
                          (getattr(socket, "TCP_KEEPCNT", None), 4)):
        if option is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
            except OSError:
                pass


def _shutdown(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
