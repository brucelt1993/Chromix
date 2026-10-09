#!/bin/sh
# 容器入口：启动 Xvfb（供有界面 profile 使用）和管理器；
# 收到停止信号时先通过 API 正常关闭所有浏览器，让 Cookie 落盘，保住登录态。
set -eu

if [ -z "${CHROMIX_MANAGER_TOKEN:-}" ]; then
    echo "必须设置 CHROMIX_MANAGER_TOKEN（容器内管理器监听 0.0.0.0）" >&2
    exit 1
fi

export DISPLAY="${DISPLAY:-:99}"
rm -f "/tmp/.X${DISPLAY#:}-lock"
Xvfb "$DISPLAY" -screen 0 "${XVFB_RESOLUTION:-1920x1080x24}" -nolisten tcp >/tmp/xvfb.log 2>&1 &
xvfb_pid=$!

python3 -m chromix manager --host 0.0.0.0 --port 8820 --home "${CHROMIX_MANAGER_HOME:-/data}" &
manager_pid=$!

shutdown() {
    trap - TERM INT
    python3 - <<'EOF' || true
import http.client, os
conn = http.client.HTTPConnection("127.0.0.1", 8820, timeout=60)
conn.request("POST", "/api/stop-all", "{}", {
    "Content-Type": "application/json",
    "X-Chromix-Token": os.environ["CHROMIX_MANAGER_TOKEN"],
})
print("已关闭全部浏览器：", conn.getresponse().status)
EOF
    kill -TERM "$manager_pid" "$xvfb_pid" 2>/dev/null || true
}
trap shutdown TERM INT

# wait 被信号打断后再等一次，确保管理器真正退出
wait "$manager_pid" || true
wait "$manager_pid" 2>/dev/null || true
