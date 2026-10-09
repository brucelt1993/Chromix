"""浏览器进程池：一个 profile 对应一个独立进程、独立 user-data-dir、独立 CDP 端口。

浏览器以独立会话启动，管理器退出不会带走浏览器；重启后通过
runtime.json + DevToolsActivePort 重新接管仍在运行的实例。
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from .. import api
from .._fonts import apply_font_env
from .._socks_auth import apply_native_socks_auth
from . import cdp
from .proxy_relay import ProxyRelay
from .store import ProfileError, ProfileStore

START_TIMEOUT = 45.0
STOP_TIMEOUT = 10.0


def _read_active_port(user_data_dir: Path) -> tuple[int, str] | None:
    try:
        lines = (user_data_dir / "DevToolsActivePort").read_text().split()
        return int(lines[0]), lines[1]
    except (OSError, ValueError, IndexError):
        return None


def _port_alive(port: int, ws_path: str | None = None) -> bool:
    """端口上是否有浏览器在响应；给定 ws_path 时还要求是同一个浏览器实例。"""
    try:
        url = cdp.browser_ws_url(port, timeout=1)
    except Exception:
        return False
    return ws_path is None or url.endswith(ws_path)


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def clear_stale_lock(user_data_dir: Path) -> bool:
    """删除陈旧的 Chromium profile 锁（SingletonLock -> "主机名-pid"）。

    崩溃或容器被强杀后锁会残留；容器重建后 pid 从头分配，锁里的 pid 可能恰好是
    别的进程，Chromium 会把它当作卡死的实例去结束。只有确认锁的持有者不是使用
    该目录的浏览器时才删除；其他主机的锁保持不动。
    """
    lock = user_data_dir / "SingletonLock"
    try:
        host, _, pid = os.readlink(lock).rpartition("-")
    except OSError:
        return False
    if host != socket.gethostname():
        return False
    if pid.isdigit() and _pid_alive(int(pid)):
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return False  # 无法确认（非 Linux），保守起见不删
        if os.fsencode(f"--user-data-dir={user_data_dir}") in cmdline:
            return False
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (user_data_dir / name).unlink(missing_ok=True)
    return True


def build_launch(profile: dict, user_data_dir: Path) -> tuple[list[str], dict | None, dict | None]:
    """把 profile 转成 (argv, env, 需中继的代理配置)，复用 SDK 的指纹参数组装。"""
    seed = api._profile_seed(user_data_dir)  # 每个 profile 固定种子 → 稳定且互不相同的指纹
    args = [f"--fingerprint={seed}"]
    if profile["platform"]:
        args.append(f"--fingerprint-platform={profile['platform']}")
    args += profile["args"]
    headless = profile["headless"]
    binary, chrome_args, proxy_kwargs, _ = api._prepare(
        headless, profile["proxy"] or None, args, True, profile["timezone"] or None,
        profile["locale"] or None, profile["geoip"], None, start_maximized=not headless)
    launch_kwargs: dict = {}
    apply_native_socks_auth(proxy_kwargs, launch_kwargs, chrome_args)
    apply_font_env(binary, launch_kwargs)

    relay = None
    proxy = proxy_kwargs.get("proxy")
    if proxy:
        if "username" in proxy or "password" in proxy:
            if not proxy["server"].startswith(("http://", "https://")):
                raise ProfileError("带账号的代理只支持 HTTP(S) 或 SOCKS5")
            relay = proxy  # HTTP(S) 代理带账号：由本地中继注入鉴权
        else:
            chrome_args.append(f"--proxy-server={proxy['server']}")
        if proxy.get("bypass"):
            chrome_args.append(f"--proxy-bypass-list={proxy['bypass']}")

    # 开启远程调试端口会让未打补丁的构建（如当前 macOS 发布包）暴露 navigator.webdriver=true；
    # 合并进用户已有的 --disable-blink-features，避免后者覆盖前者。
    blink = [a for a in chrome_args if a.startswith("--disable-blink-features=")]
    features = [f for a in blink for f in a.partition("=")[2].split(",") if f]
    chrome_args = [a for a in chrome_args if a not in blink]
    chrome_args.append("--disable-blink-features=" + ",".join(dict.fromkeys(features + ["AutomationControlled"])))
    chrome_args += [
        f"--user-data-dir={user_data_dir}",
        f"--remote-debugging-port={profile['cdp_port'] or 0}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        chrome_args.append("--headless=new")
    if sys.platform == "darwin":
        chrome_args.append("--use-mock-keychain")  # 避免多开时反复弹钥匙串授权
    elif sys.platform.startswith("linux"):
        chrome_args.append("--password-store=basic")
    argv = [str(binary), *chrome_args]
    if profile["start_url"]:
        argv.append(profile["start_url"])
    return argv, launch_kwargs.get("env"), relay


class BrowserPool:
    def __init__(self, store: ProfileStore):
        self.store = store
        self._lock = threading.RLock()
        self._busy: set[str] = set()
        self._procs: dict[str, subprocess.Popen] = {}
        self._relays: dict[str, ProxyRelay] = {}
        self._runtime_file = store.home / "runtime.json"
        self._logs = store.home / "logs"
        self._logs.mkdir(exist_ok=True)
        self._runtime: dict[str, dict] = {}
        self._adopt()

    # -- runtime 持久化 -------------------------------------------------
    def _save_runtime(self) -> None:
        tmp = self._runtime_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._runtime, indent=2), encoding="utf-8")
        os.replace(tmp, self._runtime_file)

    def _adopt(self) -> None:
        try:
            saved = json.loads(self._runtime_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        for profile_id, info in saved.items():
            try:
                profile = self.store.get(profile_id)
            except KeyError:
                continue
            # 端口和 ws 路径都要对上，避免把其他实例复用的端口误认成本 profile。
            active = _read_active_port(self.store.user_data_dir(profile_id))
            if (not active or (active[0], active[1]) != (info.get("port"), info.get("ws_path"))
                    or not _pid_alive(info["pid"]) or not _port_alive(info["port"], info["ws_path"])):
                continue
            if info.get("relay_port"):
                try:
                    _, _, relay = build_launch(profile, self.store.user_data_dir(profile_id))
                    if relay:
                        self._relays[profile_id] = ProxyRelay(
                            relay["server"], relay.get("username", ""), relay.get("password", ""),
                            port=info["relay_port"])
                except Exception:
                    pass  # 中继无法恢复时浏览器仍可用，只是代理请求会失败
            self._runtime[profile_id] = info
        self._save_runtime()

    # -- 状态 -----------------------------------------------------------
    def _alive(self, profile_id: str) -> bool:
        info = self._runtime.get(profile_id)
        if not info:
            return False
        proc = self._procs.get(profile_id)
        if proc is not None:
            return proc.poll() is None
        # 接管来的进程按 pid 判断：CDP 偶尔响应慢不能当成已退出（接管时已核对过身份）。
        return _pid_alive(info["pid"])

    def _cleanup(self, profile_id: str) -> None:
        self._procs.pop(profile_id, None)
        relay = self._relays.pop(profile_id, None)
        if relay:
            relay.close()
        if self._runtime.pop(profile_id, None) is not None:
            self._save_runtime()

    def status(self, profile_id: str) -> dict:
        with self._lock:
            if profile_id in self._busy:
                return {"state": "busy"}
            if not self._alive(profile_id):
                self._cleanup(profile_id)
                return {"state": "stopped"}
            info = self._runtime[profile_id]
            port = info["port"]
            return {
                "state": "running",
                "pid": info["pid"],
                "started_at": info["started_at"],
                "cdp_port": port,
                "cdp_http": f"http://127.0.0.1:{port}",
                "cdp_ws": f"ws://127.0.0.1:{port}{info['ws_path']}",
            }

    def running_port(self, profile_id: str) -> int:
        status = self.status(profile_id)
        if status["state"] != "running":
            raise ProfileError("浏览器未运行")
        return status["cdp_port"]

    def log_tail(self, profile_id: str, limit: int = 20000) -> str:
        log = self._logs / f"{profile_id}.log"
        return log.read_bytes()[-limit:].decode("utf-8", "replace") if log.exists() else ""

    # -- 启停 -----------------------------------------------------------
    def start(self, profile_id: str) -> dict:
        with self._lock:
            if profile_id in self._busy or self._alive(profile_id):
                raise ProfileError("该 profile 已在运行或正在启动")
            profile = self.store.get(profile_id)
            self._busy.add(profile_id)
        try:
            self._launch(profile_id, profile)
        finally:
            with self._lock:
                self._busy.discard(profile_id)
        return self.status(profile_id)

    def _launch(self, profile_id: str, profile: dict) -> None:
        user_data_dir = self.store.user_data_dir(profile_id)
        user_data_dir.mkdir(parents=True, exist_ok=True)
        try:
            argv, env, relay_cfg = build_launch(profile, user_data_dir)
        except ValueError as error:
            raise ProfileError(str(error)) from None
        relay = None
        if relay_cfg:
            relay = ProxyRelay(relay_cfg["server"], relay_cfg.get("username", ""),
                               relay_cfg.get("password", ""))
            argv.insert(1, f"--proxy-server={relay.server}")
        (user_data_dir / "DevToolsActivePort").unlink(missing_ok=True)
        clear_stale_lock(user_data_dir)  # 调用方已确认本 profile 没有在运行
        log = open(self._logs / f"{profile_id}.log", "ab")
        log.write(f"\n=== {time.strftime('%F %T')} {' '.join(argv)}\n".encode())
        log.flush()
        popen_kwargs = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                        if os.name == "nt" else {"start_new_session": True})
        try:
            proc = subprocess.Popen(argv, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, **popen_kwargs)
        except OSError:
            if relay:
                relay.close()
            raise
        finally:
            log.close()

        deadline = time.monotonic() + START_TIMEOUT
        active = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            active = _read_active_port(user_data_dir)
            if active and _port_alive(active[0]):
                break
            active = None
            time.sleep(0.2)
        if not active:
            if proc.poll() is None:
                proc.kill()
                proc.wait(5)
            if relay:
                relay.close()
            tail = self.log_tail(profile_id, 1500)
            raise ProfileError(f"浏览器启动失败（退出码 {proc.poll()}）：\n{tail}")

        with self._lock:
            self._procs[profile_id] = proc
            if relay:
                self._relays[profile_id] = relay
            self._runtime[profile_id] = {
                "pid": proc.pid, "port": active[0], "ws_path": active[1],
                "relay_port": relay.port if relay else 0, "started_at": int(time.time()),
            }
            self._save_runtime()

    def stop(self, profile_id: str) -> dict:
        with self._lock:
            if profile_id in self._busy:
                raise ProfileError("该 profile 正在启动或停止")
            if not self._alive(profile_id):
                self._cleanup(profile_id)
                return {"state": "stopped"}
            self._busy.add(profile_id)
            info = dict(self._runtime[profile_id])
            proc = self._procs.get(profile_id)
        exited = False
        try:
            # 先走 CDP Browser.close，让浏览器正常落盘 Cookie（保住登录态）。
            try:
                cdp.close_browser(info["port"])
            except Exception:
                pass
            exited = self._wait_exit(proc, info, STOP_TIMEOUT)
            if not exited:
                self._signal(proc, info, force=False)
                exited = self._wait_exit(proc, info, 5)
            if not exited:
                self._signal(proc, info, force=True)
                exited = self._wait_exit(proc, info, 5)
        finally:
            with self._lock:
                self._busy.discard(profile_id)
                if exited:
                    self._cleanup(profile_id)
        if not exited:
            raise ProfileError(f"浏览器进程 {info['pid']} 未能结束，请手动关闭")
        return {"state": "stopped"}

    def delete(self, profile_id: str) -> None:
        """删除 profile 及其用户目录；与 start 互斥，避免删掉正在使用的目录。"""
        with self._lock:
            if profile_id in self._busy or self._alive(profile_id):
                raise ProfileError("请先停止浏览器再删除")
            self._busy.add(profile_id)
        try:
            self.store.delete(profile_id)
        finally:
            with self._lock:
                self._busy.discard(profile_id)

    @staticmethod
    def _wait_exit(proc, info, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (proc.poll() is not None) if proc else not _pid_alive(info["pid"]):
                return True
            time.sleep(0.2)
        return False

    @staticmethod
    def _signal(proc, info, force: bool) -> None:
        try:
            if proc:
                proc.kill() if force else proc.terminate()
            else:
                os.kill(info["pid"], signal.SIGKILL if force and os.name != "nt" else signal.SIGTERM)
        except OSError:
            pass

    def stop_all(self) -> None:
        for profile_id in list(self._runtime):
            try:
                self.stop(profile_id)
            except ProfileError:
                pass
