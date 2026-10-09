"""Profile 持久化：每个 profile 一份配置 + 一个独立 user-data-dir。

独立 user-data-dir 是登录隔离的根本：Cookie、localStorage、IndexedDB、
Service Worker 和缓存都落在各自目录，浏览器进程之间互不可见。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .._network import split_proxy

PLATFORMS = ("", "windows", "macos", "linux")
# 由管理器负责的参数，禁止 profile 通过 extra args 覆盖，否则会破坏隔离或 CDP 端口管理。
# --remote-allow-origins 会放开 CDP 的 Origin 校验，让网页有机会连上本机调试端口。
MANAGED_FLAGS = (
    "--user-data-dir", "--remote-debugging-port", "--remote-debugging-pipe",
    "--remote-debugging-address", "--remote-allow-origins", "--fingerprint",
    "--proxy-server", "--proxy-pac-url", "--proxy-auto-detect", "--no-proxy-server",
    "--headless", "--profile-directory",
)
_LOCALE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")

DEFAULTS = {
    "name": "",
    "group": "",
    "notes": "",
    "platform": "",
    "locale": "",
    "timezone": "",
    "proxy": "",
    "geoip": False,
    "headless": False,
    "start_url": "",
    "cdp_port": 0,
    "args": [],
}

# 预设模板：Boss 直聘场景固定中文与上海时区，启动即打开首页。
TEMPLATES = {
    "boss": {"locale": "zh-CN", "timezone": "Asia/Shanghai",
             "start_url": "https://www.zhipin.com/", "group": "Boss直聘"},
}


class ProfileError(ValueError):
    """配置非法或状态冲突，HTTP 层映射为 4xx。"""


def _valid_timezone(name: str) -> bool:
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return True
    except Exception:
        return False


def validate(data: dict) -> dict:
    """返回规范化后的配置字段；非法值抛 ProfileError。"""
    out = {}
    for key, default in DEFAULTS.items():
        value = data.get(key, default)
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise ProfileError(f"{key} 必须是布尔值")
        elif isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ProfileError(f"{key} 必须是整数")
        elif isinstance(default, list):
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ProfileError(f"{key} 必须是字符串数组")
            value = [v.strip() for v in value if v.strip()]
        else:
            if not isinstance(value, str):
                raise ProfileError(f"{key} 必须是字符串")
            value = value.strip()
        out[key] = value

    if not out["name"] or len(out["name"]) > 64:
        raise ProfileError("名称不能为空且不超过 64 个字符")
    if out["platform"] not in PLATFORMS:
        raise ProfileError("platform 只能是 windows / macos / linux 或留空（本机）")
    if out["locale"] and not _LOCALE.match(out["locale"]):
        raise ProfileError("locale 格式应类似 zh-CN")
    if out["timezone"] and not _valid_timezone(out["timezone"]):
        raise ProfileError("timezone 必须是 IANA 时区，例如 Asia/Shanghai")
    if out["proxy"]:
        proxy = split_proxy(out["proxy"])  # 非法时抛 ValueError
        if ("username" in proxy or "password" in proxy) and \
                urlsplit(proxy["server"]).scheme not in ("http", "https", "socks", "socks5", "socks5h"):
            raise ProfileError("带账号的代理只支持 HTTP(S) 或 SOCKS5")
    if out["cdp_port"] and not 1024 <= out["cdp_port"] <= 65535:
        raise ProfileError("cdp_port 取 0（自动）或 1024-65535")
    if out["start_url"]:
        scheme = urlsplit(out["start_url"]).scheme
        if scheme not in ("http", "https", "about", "chrome"):
            raise ProfileError("start_url 仅支持 http(s)/about/chrome 协议")
    for arg in out["args"]:
        if not arg.startswith("--"):
            raise ProfileError(f"附加参数必须以 -- 开头：{arg}")
        # Chromium 在 Windows 上不区分开关大小写，统一小写比较防绕过。
        if arg.partition("=")[0].lower() in MANAGED_FLAGS:
            raise ProfileError(f"{arg.partition('=')[0]} 由管理器控制，不能手动指定")
    return out


class ProfileStore:
    """profiles.json 的线程安全读写；目录布局：<home>/profiles/<id>/。"""

    def __init__(self, home: str | os.PathLike):
        self.home = Path(home).expanduser().resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.home, 0o700)  # 用户目录含登录态与代理凭据，只允许本人访问
        self._file = self.home / "profiles.json"
        self._lock = threading.RLock()
        self._profiles: dict[str, dict] = {}
        if self._file.exists():
            self._profiles = json.loads(self._file.read_text(encoding="utf-8"))

    def _save(self) -> None:
        fd, tmp = tempfile.mkstemp(prefix="profiles.", dir=self.home)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(self._profiles, stream, ensure_ascii=False, indent=2)
        os.replace(tmp, self._file)

    def user_data_dir(self, profile_id: str) -> Path:
        return self.home / "profiles" / profile_id

    def list(self) -> list[dict]:
        with self._lock:
            return sorted((dict(p) for p in self._profiles.values()),
                          key=lambda p: p["created_at"])

    def get(self, profile_id: str) -> dict:
        with self._lock:
            if profile_id not in self._profiles:
                raise KeyError(profile_id)
            return dict(self._profiles[profile_id])

    def create(self, data: dict) -> dict:
        data = dict(data)
        template_name = data.pop("template", "") or ""
        if template_name not in ("", *TEMPLATES):
            raise ProfileError(f"未知模板：{template_name}")
        # 模板只填补未填写的字段，显式值优先。
        for key, value in TEMPLATES.get(template_name, {}).items():
            if not data.get(key):
                data[key] = value
        fields = validate(data)
        with self._lock:
            profile_id = secrets.token_hex(4)
            while profile_id in self._profiles:
                profile_id = secrets.token_hex(4)
            now = int(time.time())
            profile = {"id": profile_id, **fields, "created_at": now, "updated_at": now}
            self._profiles[profile_id] = profile
            self.user_data_dir(profile_id).mkdir(parents=True, exist_ok=True)
            self._save()
            return dict(profile)

    def update(self, profile_id: str, data: dict) -> dict:
        with self._lock:
            current = self.get(profile_id)
            fields = validate({**current, **data})
            current.update(fields, updated_at=int(time.time()))
            self._profiles[profile_id] = current
            self._save()
            return dict(current)

    def delete(self, profile_id: str) -> None:
        with self._lock:
            self.get(profile_id)
            del self._profiles[profile_id]
            self._save()
            shutil.rmtree(self.user_data_dir(profile_id), ignore_errors=True)
