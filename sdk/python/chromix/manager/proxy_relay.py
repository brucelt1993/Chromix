"""HTTP(S) 上游代理的本地鉴权中继。

Chromium 的 --proxy-server 不接受账号密码；Playwright 靠 CDP 应答 407，
但管理器启动的浏览器要给外部 CDP 客户端用，不能占着这个通道。
因此在 127.0.0.1 上起一个中继：给每个请求头补 Proxy-Authorization 后原样转发。
非 CONNECT 请求强制 Connection: close，保证每条连接只有一个请求头需要改写。
"""
from __future__ import annotations

import base64
import os
import select
import socket
import socketserver
import ssl
import threading
from urllib.parse import urlsplit

_MAX_HEAD = 64 * 1024
_DROP = (b"proxy-authorization", b"proxy-connection", b"connection")


def _rewrite_head(head: bytes, auth: bytes) -> bytes:
    lines = head.split(b"\r\n")
    connect = lines[0].upper().startswith(b"CONNECT ")
    kept = [lines[0]] + [l for l in lines[1:] if l.split(b":", 1)[0].strip().lower() not in _DROP]
    kept.append(b"Proxy-Authorization: Basic " + auth)
    if not connect:
        kept += [b"Connection: close", b"Proxy-Connection: close"]
    return b"\r\n".join(kept) + b"\r\n\r\n"


def _close_response(head: bytes) -> bytes:
    """强制上游响应带 Connection: close，防止浏览器复用连接发出未注入鉴权的第二个请求。"""
    lines = head.split(b"\r\n")
    kept = [lines[0]] + [l for l in lines[1:] if l.split(b":", 1)[0].strip().lower()
                         not in (b"connection", b"proxy-connection", b"keep-alive")]
    return b"\r\n".join(kept + [b"Connection: close", b"Proxy-Connection: close"]) + b"\r\n\r\n"


def _pipe(a: socket.socket, b: socket.socket, rewrite_response: bool = False) -> None:
    """a=客户端，b=上游；rewrite_response 时改写上游返回的第一个响应头。"""
    sockets = [a, b]
    pending = b"" if rewrite_response else None
    while True:
        # SSLSocket 可能缓存了已解密数据，select 看不到，需优先读出。
        readable = [s for s in sockets if isinstance(s, ssl.SSLSocket) and s.pending()]
        if not readable:
            readable, _, _ = select.select(sockets, [], [], 300)
        if not readable:
            return
        for sock in readable:
            data = sock.recv(65536)
            if not data:
                if pending:
                    a.sendall(pending)
                return
            if sock is b and pending is not None:
                pending += data
                if b"\r\n\r\n" not in pending:
                    if len(pending) > _MAX_HEAD:
                        return
                    continue
                head, _, rest = pending.partition(b"\r\n\r\n")
                data, pending = _close_response(head) + rest, None
            (b if sock is a else a).sendall(data)


class ProxyRelay:
    def __init__(self, upstream: str, username: str, password: str, port: int = 0):
        url = urlsplit(upstream)
        self._upstream = (url.hostname, url.port or (443 if url.scheme == "https" else 80))
        self._tls = url.scheme == "https"
        self._host_name = url.hostname
        auth = base64.b64encode(f"{username}:{password}".encode("utf-8"))
        relay = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    relay._handle(self.request, auth)
                except OSError:
                    pass  # 客户端超时或断开，属于正常情况

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            # Windows 的 SO_REUSEADDR 允许抢占他人正在监听的端口，只在 POSIX 上开启。
            allow_reuse_address = os.name != "nt"

        self._server = Server(("127.0.0.1", port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True,
                                        name=f"chromix-relay-{self.port}")
        self._thread.start()

    @property
    def server(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _connect_upstream(self) -> socket.socket:
        sock = socket.create_connection(self._upstream, timeout=30)
        if self._tls:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self._host_name)
        sock.settimeout(None)
        return sock

    def _handle(self, client: socket.socket, auth: bytes) -> None:
        client.settimeout(60)  # 防止慢速连接长期占住线程
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = client.recv(65536)
            if not chunk or len(buf) > _MAX_HEAD:
                return
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        try:
            upstream = self._connect_upstream()
        except OSError:
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            return
        with upstream:
            upstream.sendall(_rewrite_head(head, auth) + rest)
            client.settimeout(None)
            _pipe(client, upstream, rewrite_response=not head.upper().startswith(b"CONNECT "))

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
