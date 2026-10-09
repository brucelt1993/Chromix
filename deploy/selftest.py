"""部署自检（在容器内执行）：建临时 profile → 启动 → 经 CDP 网关执行 JS → 停止 → 删除。"""
import http.client
import json
import os
import sys

sys.path.insert(0, "/opt/chromix-sdk")
from chromix.manager.cdp import CDPSession  # noqa: E402

H = {"Content-Type": "application/json", "X-Chromix-Token": os.environ["CHROMIX_MANAGER_TOKEN"]}


def call(method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", 8820, timeout=90)
    conn.request(method, path, json.dumps(body or {}) if method != "GET" else None, H)
    res = conn.getresponse()
    data = json.loads(res.read())
    if res.status != 200:
        raise SystemExit(f"{method} {path} 失败：{data.get('error')}")
    return data


def check(headless):
    mode = "无头" if headless else "有界面(Xvfb)"
    profile = call("POST", "/api/profiles", {"name": "__selftest__", "headless": headless,
                                             "locale": "zh-CN", "start_url": "about:blank"})
    pid = profile["id"]
    try:
        call("POST", f"/api/profiles/{pid}/start")
        version = call("GET", f"/cdp/{pid}/json/version")
        page = [t for t in call("GET", f"/cdp/{pid}/json/list") if t["type"] == "page"][0]
        # 走 CDP 网关（与外部程序相同的路径）执行 JS，验证 sandbox 下渲染进程可用
        ws = page["webSocketDebuggerUrl"] + "?token=" + os.environ["CHROMIX_MANAGER_TOKEN"]
        with CDPSession(ws) as cdp:
            probe = cdp.call("Runtime.evaluate", {"expression": "[navigator.webdriver, navigator.language]",
                                                  "returnByValue": True})["result"]["value"]
        print(f"[{mode}] 浏览器 {version['Browser']}，webdriver={probe[0]}，language={probe[1]}")
        call("POST", f"/api/profiles/{pid}/stop")
    finally:
        try:
            call("DELETE", f"/api/profiles/{pid}")
        except SystemExit:
            pass


for headless in (True, False):
    check(headless)
print("自检通过")
