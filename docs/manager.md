# Chromix 多开管理器

`chromix.manager` 是 Python SDK 自带的本地管理服务：在网页上创建浏览器 profile，一键多开，每个实例暴露独立 CDP 端点，并且各 profile 的网站登录态（例如 Boss 直聘）互相隔离。仅依赖 Python 标准库；用 Playwright 等工具接入 CDP 时，再另行安装对应客户端。

## 启动

```bash
python -m pip install './sdk/python'          # 或 pip install chromix
python -m chromix manager                      # 打开 http://127.0.0.1:8820/
```

| 参数 | 说明 |
|---|---|
| `--port` | 管理器端口，默认 `8820` |
| `--home` | 数据目录，默认 `~/.chromix-manager`（或环境变量 `CHROMIX_MANAGER_HOME`） |
| `--binary` | 指定本地 Chromix 可执行文件，等同 `CLOAKBROWSER_BINARY_PATH` |
| `--host` / `--token` | 监听非回环地址时必须设置 token；请求头为 `X-Chromix-Token` |

**macOS 注意**：SDK 默认通道目前没有可用的 macOS 资产；`v154.0.8037.57` 的 macOS 包不含 Chromix 指纹补丁（`--fingerprint-*` 不生效）。在 Apple Silicon 上请使用已带补丁的 `v152.0.7977.82`：

```bash
CLOAKBROWSER_VERSION=152.0.7977.82 python -m chromix manager
```

## 隔离模型

| 隔离项 | 实现 |
|---|---|
| Cookie / localStorage / IndexedDB / 缓存 | 每个 profile 独立 `--user-data-dir`（`<home>/profiles/<id>/`） |
| 浏览器指纹 | 每个 profile 首次启动生成并固定 `.chromix-fingerprint-seed`，可单独设置平台、语言、时区 |
| 网络出口 | 每个 profile 独立代理；HTTP(S) 代理账号由 127.0.0.1 上的本地中继注入，不出现在命令行 |
| CDP | 每个实例独立调试端口（默认自动分配，也可固定） |

同一 profile 同一时间只允许一个进程。停止时优先发送 CDP `Browser.close`，让浏览器正常写盘，确保登录态在下次启动时仍在。管理器退出不会关闭浏览器，重启后通过 `runtime.json` 和 `DevToolsActivePort` 自动接管。

## Boss 直聘多账号

1. 新建 profile，模板选「Boss直聘」（自动填入 `zh-CN`、`Asia/Shanghai`、启动页 `https://www.zhipin.com/`）。
2. 每个账号建一个 profile，分别启动，在各自窗口里扫码登录。
3. 「Cookie」面板可以按域名（默认 `zhipin.com`）查看、导出、导入或清除 cookie，只作用于当前 profile。

管理器不判断某个 cookie 是否代表“已登录”，界面只列出该域的 cookie。同一出口 IP 下短时间打开多个新访客，Boss 直聘可能要求“安全验证”，需要人工完成；长期多账号建议给每个 profile 配置独立代理。

## CDP 接入

profile 运行后，列表里会显示 `http://127.0.0.1:<port>`，也可通过 API 获取：

```python
import json, urllib.request
from playwright.sync_api import sync_playwright

info = json.load(urllib.request.urlopen("http://127.0.0.1:8820/api/profiles/<id>"))
with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp(info["status"]["cdp_http"])
    page = browser.contexts[0].pages[0]      # 复用该 profile 的持久化上下文
    print(page.title())
```

浏览器由管理器以普通方式启动（不带 `--enable-automation`），并默认追加 `--disable-blink-features=AutomationControlled`；未打补丁的构建在开启调试端口后会把 `navigator.webdriver` 设为 `true`，加上这个参数可以避免。请使用 `browser.contexts[0]`（profile 的持久化上下文）；`browser.new_context()` 创建的是临时上下文，不会保存登录态。

## 远程程序接入（VPS 部署）

外部程序只需要管理器地址和 token：先调接口启动 profile，再通过 **CDP 网关** `/cdp/<id>` 控制浏览器。网关校验 token 后，把 WebSocket 原样转发给对应浏览器。各浏览器的调试端口始终只监听 VPS 本机，对外只开放管理器这一个端口。

```bash
# VPS 上（不要用 root 运行）
python -m chromix manager --host 0.0.0.0 --token "$(openssl rand -hex 24)"
```

```python
import requests
from playwright.sync_api import sync_playwright

BASE = "https://vps.example.com"                       # 管理器地址
H = {"X-Chromix-Token": "<token>"}

pid = requests.post(f"{BASE}/api/profiles", json={"name": "job-1", "headless": True}, headers=H).json()["id"]
info = requests.post(f"{BASE}/api/profiles/{pid}/start", json={}, headers=H).json()
with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp(info["cdp_remote"], headers=H)   # wss://vps.example.com/cdp/<id>
    page = browser.contexts[0].pages[0]
    page.goto("https://example.com")
requests.post(f"{BASE}/api/profiles/{pid}/stop", json={}, headers=H)
```

| 接入地址 | 用法 |
|---|---|
| `cdp_remote`：`ws(s)://host/cdp/<id>` | 浏览器级 WebSocket，配合 `X-Chromix-Token` 请求头 |
| `cdp_remote_http`：`http(s)://host/cdp/<id>` | 发现地址，`/json/version`、`/json/list` 中的 ws 地址已改写为网关地址 |
| `.../cdp/<id>?token=<token>` | 仅在客户端无法设置请求头时使用：token 会出现在 URL 中，可能被反代访问日志记录（管理器自身日志不记录 query） |
| `.../cdp/<id>/devtools/page/<targetId>` | page 级连接 |

Puppeteer：`puppeteer.connect({ browserWSEndpoint: info.cdp_remote, headers: H })`。

**公网必须使用 HTTPS**，否则 token 以明文传输。可以用 Caddy 反代，它会自动申请证书并支持 WebSocket；管理器只监听本机即可：

```caddyfile
vps.example.com {
    reverse_proxy 127.0.0.1:8820
}
```
```bash
python -m chromix manager --token "<token>"      # 只监听 127.0.0.1，由 Caddy 对外
```
管理器会根据 `X-Forwarded-Proto` 返回 `wss://` 地址。VPS 没有显示器时，新建 profile 勾选无头模式，或用 `xvfb-run -a python -m chromix manager ...` 以有界面模式运行。

## Docker Compose 一键部署

`deploy/` 目录包含完整部署：管理器镜像（发布版浏览器 + 管理器 + Xvfb + 中文字体）、`docker-compose.yml`、可选的 Caddy 自动 HTTPS，以及一键脚本。适用于装好 Docker Engine 与 Compose v2 的 Linux 服务器（amd64 / arm64）。

```bash
git clone <你的仓库> chromix && cd chromix/deploy

./deploy.sh                      # 只绑定 127.0.0.1:8820，用 SSH 隧道或同机程序访问
./deploy.sh --domain chromix.example.com   # 推荐：域名已解析到本机，Caddy 自动申请 HTTPS
./deploy.sh --public             # 直接开放 0.0.0.0:8820（明文 HTTP，仅限可信内网）
```

脚本会执行以下步骤：
1. 首次运行时生成 `.env` 和随机令牌；
2. 构建镜像（下载固定版本的 Linux 发布包并做 SHA-256 校验）；
3. 启动服务；
4. **在容器内自检**：创建临时 profile，启动后经 CDP 网关执行一段 JS，再停止并删除；
5. 输出访问地址和令牌。

| 命令 | 作用 |
|---|---|
| `./deploy.sh status` / `logs` | 查看状态 / 日志 |
| `./deploy.sh down` | 停止；容器停止前会先通过 API 正常关闭全部浏览器，cookie 落盘后再退出 |
| `./deploy.sh --apparmor` | Ubuntu 24.04+ 开启了 `apparmor_restrict_unprivileged_userns` 时使用，会加载 `docker/apparmor.profile`（需要 sudo） |

部署细节：
- **沙箱**：使用仓库提供的 `docker/seccomp.json` 启用 Chromium 非特权 user-namespace 沙箱，同时去掉全部 capabilities、开启 `no-new-privileges`，不使用 `--privileged` / `--no-sandbox`。请在 `deploy/` 目录内执行 compose 命令，seccomp 使用的是相对路径。
- **数据**：profile、用户目录（登录态）、日志保存在数据卷 `chromix_chromix-data`，重建镜像不会丢失。备份可用 `docker run --rm -v chromix_chromix-data:/d -v $PWD:/b ubuntu tar czf /b/chromix-data.tgz -C /d .`。
- **有界面模式**：容器内运行 Xvfb（`DISPLAY=:99`），未勾选「无头模式」的 profile 会在虚拟屏幕上以有界面方式运行。
- **字体**：Linux 发布包只带 Windows 字体，镜像额外把 Noto CJK 加入浏览器的字体目录，否则中文显示为方块。代价是字体列表中多出一款 Windows 上没有的字体。
- **浏览器版本**：与官方 Docker 镜像相同，amd64 使用 `v154.0.8037.97`，arm64 使用 `v154.0.8037.57`。部署后建议确认发布包包含指纹补丁：`docker compose exec manager sh -c "grep -c uxr- /opt/chromix/chrome"`，输出大于 0 才算通过。
- **指纹验收**：部署完成后运行 `./deploy.sh fpeval`，见下文“指纹验收”。
- **资源**：`SHM_SIZE` 默认为 2GB，多开实例较多时请调大，并留意服务器内存。

## 指纹验收

部署或更换浏览器版本后运行。脚本经管理器创建两个临时 profile，每个启动两次并采集指纹，结束后自动删除：

```bash
./deploy.sh fpeval                                          # Docker 部署
python -m chromix.manager.fpeval --manager http://127.0.0.1:8820 --token <token>   # 直接运行
```

| 判定 | 失败意味着 |
|---|---|
| 平台人设生效 / 语言与时区生效 | 对应的启动参数没有被浏览器接受 |
| navigator.webdriver 为 false | 页面能直接识别为自动化浏览器 |
| JS 与 HTTP 请求头一致 | 页面 JS 读到的值和服务器收到的请求头矛盾 |
| 重启后指纹稳定 | 同一账号每次登录看起来像不同设备 |
| GPU、字体与人设自洽 | 例如 Windows 人设却报 Apple GPU 或 macOS 字体 |
| 不同 profile 的设备指纹可区分 | Canvas、音频哈希相同，同一台机器上的多个账号可被关联为同一设备 |

有失败项时退出码为 1；加 `--json result.json` 可保存原始数据。默认测 Windows 人设，可用 `--persona` 切换。

**macOS v152.0.7977.82 的实测结果（Apple M5）**：人设、语言、时区、webdriver、重启稳定性、JS 与请求头一致性都通过；GPU 和字体与 Windows 人设矛盾；两个 profile 的 Canvas、音频哈希与关闭指纹时完全相同。另外，当前 SDK 按 v154 的开关名生成参数，v152 内核只认旧开关名，例如屏幕参数需要写成 `--uxr-screen-width`。因此 macOS 上的多个 profile 能隔离登录态，但无法避免被关联为同一设备。

## HTTP API

写请求必须带 `Content-Type: application/json`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/info` | 数据目录、模板、浏览器信息 |
| GET / POST | `/api/profiles` | 列表 / 新建（可传 `"template": "boss"`） |
| GET / PUT / DELETE | `/api/profiles/{id}` | 详情 / 修改（下次启动生效）/ 删除（需先停止，会删除用户目录） |
| POST | `/api/profiles/{id}/start` · `/stop` | 启动 / 停止，返回 `cdp_http`、`cdp_ws` |
| GET / POST / DELETE | `/api/profiles/{id}/cookies?domain=zhipin.com` | 查询 / 导入 `{"cookies": [...]}` / 按域清除 |
| GET | `/api/profiles/{id}/log` | 浏览器日志末尾 |
| GET (WebSocket) | `/cdp/{id}` | CDP 网关，见上节 |
| GET | `/cdp/{id}/json/version` · `/json/list` | CDP 发现接口（ws 地址已改写） |
| POST | `/api/stop-all` | 停止全部 |

## 安全边界

- 默认只监听 `127.0.0.1`；CDP 端口由 Chromium 绑定在本机回环地址。拿到 CDP 端口即可完全控制对应浏览器，不要转发到公网。
- 被管理的浏览器会访问任意网站，因此 API 校验 Host 与 Origin，写操作强制 JSON 以触发 CORS 预检，防止网页跨站调用管理接口。
- 删除 profile 会永久删除其用户目录（包括登录态），界面上需要二次点击确认。
- API 不回传代理密码（显示为 `******`，编辑时保持不变即沿用原密码）；数据目录权限为 `0700`。
- HTTP(S) 代理鉴权中继监听在 `127.0.0.1`，本机任意进程都能通过它使用该代理账号（Chromium 无法静默向本地中继提交凭据）。在多用户共享的机器上，建议改用带补丁浏览器的 SOCKS5 原生鉴权，或使用按 IP 白名单授权的代理。
- 用 `--host 0.0.0.0` 时 token 走明文 HTTP，只应在可信内网中使用。

## 测试

```bash
python -m pytest sdk/python/tests/test_manager.py
# 真机验收：多开、CDP、cookie 隔离与持久化、管理器重启接管、代理中继
CHROMIX_E2E_BINARY=/path/to/Chromium CHROMIX_E2E_PATCHED=1 python -m pytest sdk/python/tests/test_manager.py
```
