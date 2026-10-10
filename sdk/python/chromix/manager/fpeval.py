"""指纹验收：经管理器 API 与 CDP 网关采集两个临时 profile 的指纹并给出判定。

    python -m chromix.manager.fpeval --manager http://127.0.0.1:8820 --token <token>
    ./deploy.sh fpeval                     # 在部署好的容器内运行

判定五类问题：人设是否生效、JS 与 HTTP 请求头是否一致、重启后是否稳定、
不同 profile 能否区分（Canvas / 音频 / WebGL 相同即可被关联为同一设备）、
人设与渲染层是否自洽（如 Windows 人设却报 Apple GPU / macOS 字体）。
只依赖标准库，结果是行为观测，不是对构建来源的证明。
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .cdp import CDPSession

PERSONA = {"windows": {"platform": "Win32", "ch": "Windows", "ua": "Windows NT"},
           "linux": {"platform": "Linux x86_64", "ch": "Linux", "ua": "Linux"},
           "macos": {"platform": "MacIntel", "ch": "macOS", "ua": "Macintosh"}}
# 只在其他系统上出现的渲染层特征，用于判断人设是否自洽
FOREIGN = {"windows": {"gpu": ("Apple", "Metal", "Mesa", "llvmpipe", "SwiftShader"),
                       "fonts": ("Helvetica Neue", "PingFang SC", "Apple Color Emoji", "Menlo", "DejaVu Sans", "Ubuntu")},
           "macos": {"gpu": ("Direct3D", "D3D11", "Mesa", "llvmpipe"),
                     "fonts": ("Segoe UI", "Microsoft YaHei", "SimSun", "DejaVu Sans", "Ubuntu")},
           "linux": {"gpu": ("Direct3D", "D3D11", "Apple", "Metal"),
                     "fonts": ("Segoe UI", "Microsoft YaHei", "Helvetica Neue", "PingFang SC", "Apple Color Emoji")}}
# 高熵渲染哈希：两个 profile 相同即可被关联为同一设备。GPU 型号等低熵字段在真实用户中本就大量重复，不参与判定。
DEVICE_KEYS = ("canvasHash", "audioSum")

PROBE = r"""(async () => {
  const sha = async s => { const b = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
    return [...new Uint8Array(b)].slice(0, 8).map(x => x.toString(16).padStart(2, '0')).join(''); };
  const n = navigator, r = {};
  r.ua = n.userAgent; r.platform = n.platform; r.languages = n.languages; r.webdriver = n.webdriver;
  r.hardwareConcurrency = n.hardwareConcurrency; r.deviceMemory = n.deviceMemory;
  r.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  r.screen = [screen.width, screen.height, screen.availWidth, screen.availHeight, screen.colorDepth, devicePixelRatio];
  r.uaPlatform = n.userAgentData ? n.userAgentData.platform : null;
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    r.webglVendor = ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR);
    r.webglRenderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
    r.webglParamsHash = await sha(JSON.stringify([gl.getParameter(gl.MAX_TEXTURE_SIZE), gl.getSupportedExtensions()]));
  } catch (e) { r.webglRenderer = 'error: ' + e; }
  const c = document.createElement('canvas'); c.width = 280; c.height = 60; const x = c.getContext('2d');
  x.textBaseline = 'top'; x.font = '16px Arial'; x.fillStyle = '#f60'; x.fillRect(100, 1, 62, 20);
  x.fillStyle = '#069'; x.fillText('Chromix 指纹 Cwm fjordbank', 2, 15); x.fillStyle = 'rgba(102,204,0,0.7)';
  x.fillText('Chromix 指纹 Cwm fjordbank', 4, 17); x.beginPath(); x.arc(50, 30, 20, 0, Math.PI * 2); x.stroke();
  r.canvasHash = await sha(c.toDataURL());
  try {
    const ac = new OfflineAudioContext(1, 5000, 44100), o = ac.createOscillator(), comp = ac.createDynamicsCompressor();
    o.type = 'triangle'; o.frequency.value = 10000; o.connect(comp); comp.connect(ac.destination); o.start(0);
    const d = (await ac.startRendering()).getChannelData(0); let s = 0;
    for (let i = 4500; i < 5000; i++) s += Math.abs(d[i]); r.audioSum = s.toFixed(10);
  } catch (e) { r.audioSum = 'error: ' + e; }
  const fonts = ['Arial', 'Calibri', 'Segoe UI', 'Microsoft YaHei', 'SimSun', 'Consolas', 'Helvetica Neue', 'PingFang SC',
                 'Menlo', 'Apple Color Emoji', 'Noto Sans CJK SC', 'DejaVu Sans', 'Ubuntu'];
  const m = document.createElement('canvas').getContext('2d');
  const w = f => { m.font = `72px ${f}`; return m.measureText('mmmmmmmmmlli 字体').width; };
  r.fonts = fonts.filter(f => ['monospace', 'serif', 'sans-serif'].some(b => w(`"${f}",${b}`) !== w(b)));
  r.httpHeaders = JSON.parse(document.getElementById('h').textContent);
  return r;
})()"""


class _Echo(BaseHTTPRequestHandler):
    """回显请求头；Accept-CH 让刷新后的请求带上高熵 Client Hints。"""

    def do_GET(self):
        body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Accept-CH", "Sec-CH-UA-Platform-Version, Sec-CH-UA-Arch, Sec-CH-UA-Full-Version-List")
        self.end_headers()
        self.wfile.write(b"<!doctype html><title>fpeval</title><pre id=h>" + body + b"</pre>")

    def log_message(self, *args):
        pass


class Client:
    def __init__(self, base: str, token: str):
        url = urlsplit(base)
        self.host, self.port, self.token = url.hostname, url.port or 80, token

    def call(self, method: str, path: str, body=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=120)
        headers = {"X-Chromix-Token": self.token, "Content-Type": "application/json"}
        conn.request(method, path, json.dumps(body or {}) if method != "GET" else None, headers)
        res = conn.getresponse()
        data = json.loads(res.read())
        if res.status != 200:
            raise RuntimeError(f"{method} {path}: {data.get('error', data)}")
        return data


def _evaluate(cdp: CDPSession, expression: str):
    result = cdp.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                           "returnByValue": True})
    if "exceptionDetails" in result:
        raise RuntimeError(result["exceptionDetails"].get("text", "探针执行失败"))
    return result["result"].get("value")


def _navigate(cdp: CDPSession, url: str) -> None:
    cdp.call("Page.navigate", {"url": url})
    for _ in range(100):
        time.sleep(0.1)
        try:
            if _evaluate(cdp, "location.href") == url and _evaluate(cdp, "document.readyState") == "complete":
                return
        except Exception:
            pass  # 导航中执行上下文被替换
    raise RuntimeError(f"页面加载超时：{url}")


def collect(client: Client, profile_id: str, origin: str) -> dict:
    """启动 profile → 经网关在页面里执行探针 → 停止。"""
    client.call("POST", f"/api/profiles/{profile_id}/start")
    try:
        pages = client.call("GET", f"/cdp/{profile_id}/json/list")
        page = next(t for t in pages if t["type"] == "page")
        with CDPSession(page["webSocketDebuggerUrl"] + "?token=" + client.token, timeout=60) as cdp:
            _navigate(cdp, origin)
            _navigate(cdp, origin + "?ch=1")  # 第二次请求携带 Accept-CH 申请的 Client Hints
            return _evaluate(cdp, PROBE)
    finally:
        client.call("POST", f"/api/profiles/{profile_id}/stop")


def judge(persona: str, locale: str, timezone: str, runs: dict[str, list[dict]]) -> list[dict]:
    """runs: {profile 名: [第一次启动, 重启后]} → 判定列表（PASS / WARN / FAIL）。"""
    checks = []

    def add(name, ok, detail, level="FAIL"):
        checks.append({"check": name, "result": "PASS" if ok else level, "detail": detail})

    expect = PERSONA[persona]
    first = [r[0] for r in runs.values()]
    for name, rs in runs.items():
        r = rs[0]
        h = r["httpHeaders"]
        add(f"{name}: 平台人设生效",
            r["platform"] == expect["platform"] and r["uaPlatform"] == expect["ch"] and expect["ua"] in r["ua"]
            and h.get("sec-ch-ua-platform", "").strip('"') == expect["ch"],
            f'platform={r["platform"]} uaData={r["uaPlatform"]} sec-ch-ua-platform={h.get("sec-ch-ua-platform")}')
        add(f"{name}: 语言与时区生效",
            r["languages"][:1] == [locale] and r["timezone"] == timezone,
            f'languages={r["languages"]} timezone={r["timezone"]}')
        add(f"{name}: navigator.webdriver 为 false", r["webdriver"] is False, f'webdriver={r["webdriver"]}')
        add(f"{name}: JS 与 HTTP 请求头一致",
            h.get("user-agent") == r["ua"] and h.get("accept-language", "").split(",")[0] == r["languages"][0],
            f'accept-language={h.get("accept-language")}')
        changed = [k for k in r if k != "httpHeaders" and rs[0].get(k) != rs[1].get(k)]
        add(f"{name}: 重启后指纹稳定", not changed, "变化项：" + (", ".join(changed) or "无"))
        foreign = FOREIGN[persona]
        gpu_bad = [g for g in foreign["gpu"] if g in str(r.get("webglRenderer"))]
        font_bad = [f for f in r["fonts"] if f in foreign["fonts"]]
        add(f"{name}: GPU 与人设自洽", not gpu_bad, f'webglRenderer={r.get("webglRenderer")}')
        add(f"{name}: 字体与人设自洽", not font_bad, "异常字体：" + (", ".join(font_bad) or "无"))
    if len(first) >= 2:
        a, b = first[0], first[1]
        same = [k for k in DEVICE_KEYS if a.get(k) == b.get(k)]
        add("不同 profile 的设备指纹可区分（Canvas/音频）", not same,
            "相同项：" + (", ".join(same) or "无") + ("；同机多个 profile 可被关联为同一设备" if same else ""))
        add("不同 profile 的硬件参数有差异（CPU/内存/屏幕）",
            [a["hardwareConcurrency"], a["deviceMemory"], a["screen"]] != [b["hardwareConcurrency"], b["deviceMemory"], b["screen"]],
            f'A={a["hardwareConcurrency"]}核/{a["deviceMemory"]}GB/{a["screen"][:2]} '
            f'B={b["hardwareConcurrency"]}核/{b["deviceMemory"]}GB/{b["screen"][:2]}', level="WARN")
    return checks


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m chromix.manager.fpeval", description=__doc__.split("\n")[0])
    ap.add_argument("--manager", default="http://127.0.0.1:8820")
    ap.add_argument("--token", default=os.environ.get("CHROMIX_MANAGER_TOKEN", ""))
    ap.add_argument("--persona", choices=tuple(PERSONA), default="windows")
    ap.add_argument("--locale", default="zh-CN")
    ap.add_argument("--timezone", default="Asia/Shanghai")
    ap.add_argument("--headed", action="store_true", help="以有界面模式测评（需要显示器或 Xvfb）")
    ap.add_argument("--json", help="把原始数据和判定写入该文件")
    args = ap.parse_args(argv)

    client = Client(args.manager, args.token)
    echo = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)  # 浏览器与本脚本在同一主机/容器
    threading.Thread(target=echo.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{echo.server_address[1]}/"
    created, runs = [], {}
    try:
        for name in ("profile-A", "profile-B"):
            profile = client.call("POST", "/api/profiles", {
                "name": f"__fpeval_{name}", "platform": args.persona, "locale": args.locale,
                "timezone": args.timezone, "headless": not args.headed, "start_url": "about:blank"})
            created.append(profile["id"])
            runs[name] = [collect(client, profile["id"], origin) for _ in range(2)]
            print(f"已采集 {name}（两次启动）", file=sys.stderr)
    finally:
        for profile_id in created:
            try:
                client.call("DELETE", f"/api/profiles/{profile_id}")
            except Exception:
                pass
        echo.shutdown()

    checks = judge(args.persona, args.locale, args.timezone, runs)
    sample = runs["profile-A"][0]
    print(f"\n浏览器：{sample['httpHeaders'].get('user-agent')}")
    print(f"人设：{args.persona} / {args.locale} / {args.timezone}\n")
    for c in checks:
        print(f"[{c['result']:4}] {c['check']}\n        {c['detail']}")
    failed = sum(c["result"] == "FAIL" for c in checks)
    warned = sum(c["result"] == "WARN" for c in checks)
    print(f"\n结论：{len(checks) - failed - warned} 通过，{warned} 警告，{failed} 失败")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as stream:
            json.dump({"checks": checks, "runs": runs}, stream, ensure_ascii=False, indent=2)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
