#!/usr/bin/env bash
# Chromix 多开管理器一键部署。
#   ./deploy.sh                      只绑定宿主 127.0.0.1:8820（SSH 隧道或同机程序使用）
#   ./deploy.sh --domain a.com       Caddy 自动 HTTPS，对外 https://a.com（推荐公网方式）
#   ./deploy.sh --public             直接把 8820 绑到 0.0.0.0（明文 HTTP，仅限可信内网）
#   ./deploy.sh --apparmor           Ubuntu 24.04+ 限制非特权 userns 时加载专用 AppArmor 配置
#   ./deploy.sh down                 停止（浏览器会先正常关闭，数据卷保留）
#   ./deploy.sh fpeval               指纹验收：两个临时 profile 的生效/自洽/稳定/区分度判定
set -euo pipefail
cd "$(dirname "$0")"

DOMAIN_ARG="" PUBLIC=0 APPARMOR=0 ACTION=up
while [ $# -gt 0 ]; do
    case "$1" in
        --domain) DOMAIN_ARG="${2:?--domain 需要域名}"; shift 2 ;;
        --public) PUBLIC=1; shift ;;
        --apparmor) APPARMOR=1; shift ;;
        down|logs|status|fpeval) ACTION="$1"; shift ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done

command -v docker >/dev/null || { echo "未找到 docker，请先安装 Docker Engine" >&2; exit 1; }
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null; then
    COMPOSE=(docker-compose)
else
    echo "需要 Docker Compose v2（docker compose 或 docker-compose）" >&2; exit 1
fi

set_env() {  # set_env KEY VALUE：更新或追加 .env 中的一行
    local tmp
    tmp="$(mktemp)"
    grep -v "^$1=" .env > "$tmp" || true
    printf '%s=%s\n' "$1" "$2" >> "$tmp"
    cat "$tmp" > .env
    rm -f "$tmp"
}
get_env() { grep "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2-; }

if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    token="$(openssl rand -hex 24 2>/dev/null || python3 -c 'import secrets; print(secrets.token_hex(24))')"
    set_env CHROMIX_MANAGER_TOKEN "$token"
    echo "已生成 .env 和随机访问令牌"
fi

FILES=(-f docker-compose.yml)
if [ "$APPARMOR" = 1 ]; then FILES+=(-f docker-compose.apparmor.yml); fi
PROFILE=()
if [ -n "$DOMAIN_ARG" ]; then set_env DOMAIN "$DOMAIN_ARG"; fi
if [ -n "$(get_env DOMAIN)" ]; then PROFILE=(--profile https); fi
compose() { "${COMPOSE[@]}" "${FILES[@]}" ${PROFILE[@]+"${PROFILE[@]}"} "$@"; }

case "$ACTION" in
    down) compose down; exit 0 ;;
    logs) compose logs -f --tail=200; exit 0 ;;
    status) compose ps; exit 0 ;;
    fpeval) compose exec -T manager python3 -m chromix.manager.fpeval "$@"; exit $? ;;
esac

if [ "$(get_env CHROMIX_MANAGER_TOKEN)" = "change-me-to-a-long-random-string" ]; then
    echo "请先修改 .env 中的 CHROMIX_MANAGER_TOKEN" >&2; exit 1
fi
if [ "$PUBLIC" = 1 ]; then set_env BIND_ADDR 0.0.0.0; fi

if [ "$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" = 1 ]; then
    if [ "$APPARMOR" = 1 ]; then
        echo "加载 AppArmor 配置 chromix-docker（需要 sudo）..."
        sudo apparmor_parser --replace --skip-cache ../docker/apparmor.profile
    else
        echo "提示：宿主限制了非特权 user namespace，若自检失败请加 --apparmor 重新运行（说明见 docs/docker.md）" >&2
    fi
fi

compose up -d --build

echo "等待管理器就绪..."
for _ in $(seq 1 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$(compose ps -q manager)" 2>/dev/null)" = healthy ] && break
    sleep 2
done

echo "自检：在容器内启动并关闭一个临时浏览器..."
if ! compose exec -T manager python3 - < selftest.py; then
    echo "自检失败：请查看 ./deploy.sh logs；若日志含 No usable sandbox，请加 --apparmor 重试" >&2
    exit 1
fi
# 发布包是否编入 Chromix 指纹补丁（uxr-* 开关）；缺失时浏览器仍可用，但 --fingerprint-* 不生效
if [ "$(compose exec -T manager sh -c 'grep -a -c uxr- /opt/chromix/chrome || true' | tr -d '\r')" = 0 ]; then
    echo "警告：当前浏览器发布包未包含指纹补丁，平台/语言/时区等人设参数不会生效" >&2
else
    echo "指纹补丁检查通过"
fi

TOKEN="$(get_env CHROMIX_MANAGER_TOKEN)"
if [ -n "$(get_env DOMAIN)" ]; then
    BASE="https://$(get_env DOMAIN)"
elif [ "$(get_env BIND_ADDR)" = 0.0.0.0 ]; then
    BASE="http://<服务器IP>:$(get_env MANAGER_PORT)"
else
    BASE="http://127.0.0.1:$(get_env MANAGER_PORT)  （远程请先 ssh -L $(get_env MANAGER_PORT):127.0.0.1:$(get_env MANAGER_PORT) 服务器）"
fi
cat <<EOF

部署完成
  管理界面：$BASE
  访问令牌：$TOKEN   （界面里输入；程序请求头 X-Chromix-Token）
  CDP 网关：启动 profile 后返回的 cdp_remote 字段，形如 ${BASE%% *}/cdp/<id>（ws/wss）
  数据卷：  chromix_chromix-data（profile、登录态、日志）
  常用命令：./deploy.sh status | logs | down | fpeval（指纹验收）
EOF
