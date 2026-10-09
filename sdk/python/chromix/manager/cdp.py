"""最小 CDP 客户端（标准库 WebSocket），用于优雅关闭浏览器和 Cookie 管理。

只实现管理器需要的子集：客户端掩码文本帧、分片重组、ping/pong、close。
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import struct
from urllib.parse import urlsplit


class CDPError(RuntimeError):
    pass


def fetch_json(port: int, path: str, timeout: float = 2.0):
    # 直接连回环地址：urllib 会套用系统/环境代理（如 Clash），把本机请求发给代理。
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        return json.loads(conn.getresponse().read())
    finally:
        conn.close()


def browser_ws_url(port: int, timeout: float = 2.0) -> str:
    return fetch_json(port, "/json/version", timeout)["webSocketDebuggerUrl"]


def _mask(payload: bytes, mask: bytes) -> bytes:
    n = len(payload)
    key = int.from_bytes((mask * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(payload, "big") ^ key).to_bytes(n, "big")


class CDPSession:
    def __init__(self, ws_url: str, timeout: float = 10.0):
        url = urlsplit(ws_url)
        if url.scheme != "ws":
            raise CDPError(f"只支持本地 ws:// 端点：{ws_url}")
        self._sock = socket.create_connection((url.hostname, url.port or 80), timeout=timeout)
        self._buf = bytearray()
        self._next_id = 0
        key = base64.b64encode(os.urandom(16)).decode()
        target = (url.path or "/") + (f"?{url.query}" if url.query else "")
        request = (f"GET {target} HTTP/1.1\r\nHost: {url.hostname}:{url.port}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self._sock.sendall(request.encode())
        head = self._read_until(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            self._sock.close()
            raise CDPError(f"WebSocket 握手失败：{head[:200]!r}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(min(1 << 20, max(65536, n - len(self._buf))))
            if not chunk:
                raise CDPError("CDP 连接已关闭")
            self._buf += chunk
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise CDPError("CDP 连接已关闭")
            self._buf += chunk
        index = self._buf.index(marker)
        head = bytes(self._buf[:index])
        del self._buf[:index + len(marker)]
        return head

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        self._sock.sendall(header + mask + _mask(payload, mask))

    def _recv_message(self) -> str:
        parts = []
        while True:
            b1, b2 = self._read_exact(2)
            opcode, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if b2 & 0x80 else None
            payload = self._read_exact(n)
            if mask:
                payload = _mask(payload, mask)
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:
                raise CDPError("CDP 连接已关闭")
            if opcode in (0x0, 0x1, 0x2):
                parts.append(payload)
                if b1 & 0x80:
                    return b"".join(parts).decode("utf-8")

    def call(self, method: str, params: dict | None = None, session_id: str | None = None) -> dict:
        self._next_id += 1
        message = {"id": self._next_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        self._send_frame(0x1, json.dumps(message).encode())
        while True:
            reply = json.loads(self._recv_message())
            if reply.get("id") != self._next_id:
                continue  # 事件或其他会话的消息
            if "error" in reply:
                raise CDPError(f"{method}: {reply['error'].get('message')}")
            return reply.get("result", {})


def domain_matches(cookie_domain: str, domain: str) -> bool:
    """cookie 的 domain 是否属于 domain（含子域），如 .zhipin.com 属于 zhipin.com。"""
    host = cookie_domain.lstrip(".").lower()
    domain = domain.lstrip(".").lower()
    return host == domain or host.endswith("." + domain)


def _ensure_window(cdp: CDPSession) -> None:
    """macOS 有界面模式下关掉最后一个窗口进程仍在，此时浏览器级 Storage.* 会报
    "Browser context management is not supported"；先补开一个空白页。"""
    if not any(t["type"] == "page" for t in cdp.call("Target.getTargets")["targetInfos"]):
        cdp.call("Target.createTarget", {"url": "about:blank"})


def get_cookies(port: int, domain: str | None = None) -> list[dict]:
    with CDPSession(browser_ws_url(port)) as cdp:
        _ensure_window(cdp)
        cookies = cdp.call("Storage.getCookies")["cookies"]
    return [c for c in cookies if not domain or domain_matches(c["domain"], domain)]


def delete_cookies(port: int, domain: str) -> int:
    """删除 domain（含子域）下的全部 cookie，返回删除数量。

    浏览器级 target 没有按条件删除的接口，所以临时创建一个后台空白页，
    在它的会话里逐条调用 Network.deleteCookies（精确匹配 name/domain/path）。
    """
    with CDPSession(browser_ws_url(port)) as cdp:
        _ensure_window(cdp)
        targets = [c for c in cdp.call("Storage.getCookies")["cookies"]
                   if domain_matches(c["domain"], domain)]
        if not targets:
            return 0
        target_id = cdp.call("Target.createTarget", {"url": "about:blank", "background": True})["targetId"]
        try:
            session = cdp.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
            for c in targets:
                params = {"name": c["name"], "domain": c["domain"], "path": c["path"]}
                if c.get("partitionKey"):
                    params["partitionKey"] = c["partitionKey"]
                cdp.call("Network.deleteCookies", params, session_id=session)
        finally:
            cdp.call("Target.closeTarget", {"targetId": target_id})
    return len(targets)


_COOKIE_PARAM_KEYS = ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite",
                      "expires", "priority", "sourceScheme", "sourcePort", "partitionKey")


def set_cookies(port: int, cookies: list[dict]) -> int:
    """导入 cookie（兼容本模块导出的 CDP Cookie 结构），返回导入数量。"""
    params = []
    for c in cookies:
        if not isinstance(c, dict) or not c.get("name") or "domain" not in c:
            raise CDPError("cookie 至少需要 name 和 domain 字段")
        item = {k: c[k] for k in _COOKIE_PARAM_KEYS if k in c}
        item.setdefault("path", "/")
        if c.get("session") or item.get("expires", 0) in (-1, None):
            item.pop("expires", None)
        params.append(item)
    with CDPSession(browser_ws_url(port)) as cdp:
        _ensure_window(cdp)
        cdp.call("Storage.setCookies", {"cookies": params})
    return len(params)


def close_browser(port: int) -> None:
    with CDPSession(browser_ws_url(port), timeout=5) as cdp:
        try:
            cdp.call("Browser.close")
        except CDPError:
            pass  # 浏览器关闭时可能直接断开连接，属于预期
