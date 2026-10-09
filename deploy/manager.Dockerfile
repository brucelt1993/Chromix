# syntax=docker/dockerfile:1
# Chromix 多开管理器镜像：发布版浏览器 + 管理器（纯标准库）+ Xvfb（有界面模式）。
# 构建上下文为仓库根目录：docker build -f deploy/manager.Dockerfile .

# 复用官方镜像的下载脚本：固定版本 + 双重 SHA-256 校验
FROM ubuntu:24.04 AS download
ARG TARGETARCH
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*
COPY docker/download.sh /usr/local/bin/download-chromix
RUN /bin/sh /usr/local/bin/download-chromix "$TARGETARCH" /opt

FROM ubuntu:24.04
LABEL org.opencontainers.image.title="Chromix Manager" \
      org.opencontainers.image.description="Multi-profile Chromix manager with CDP gateway" \
      org.opencontainers.image.source="https://github.com/xiaozhou26/Chromix" \
      org.opencontainers.image.licenses="BSD-3-Clause"
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates fontconfig fonts-liberation fonts-noto-cjk tzdata \
        libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 \
        libcairo2 libcups2t64 libdbus-1-3 libdrm2 libexpat1 libgbm1 \
        libglib2.0-0t64 libgtk-3-0t64 libnspr4 libnss3 libpango-1.0-0 \
        libudev1 libvulkan1 libx11-6 libxcb1 libxcomposite1 libxdamage1 \
        libxext6 libxfixes3 libxkbcommon0 libxrandr2 \
        python3 xvfb \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 chromix \
    && useradd --uid 10001 --gid chromix --create-home --shell /bin/sh chromix \
    && install -d -o chromix -g chromix /data \
    && install -d -m 1777 /tmp/.X11-unix
COPY --from=download /opt/chromix /opt/chromix
# 非特权 user-namespace sandbox，不使用 setuid helper（与官方镜像一致）。
# 发布包只带 Windows 字体且 SDK 的 fontconfig 只扫描包内 fonts/，因此把中文字体链接进去，否则中文页面显示方块。
RUN chown -R root:root /opt/chromix \
    && chmod 0755 /opt/chromix/chromix /opt/chromix/chrome \
        /opt/chromix/chrome-sandbox /opt/chromix/chrome_crashpad_handler \
    && mkdir -p /opt/chromix/fonts \
    && ln -s /usr/share/fonts/opentype/noto /opt/chromix/fonts/noto-cjk
# 管理器只依赖标准库，直接放入 PYTHONPATH，无需 pip/网络
COPY sdk/python/chromix /opt/chromix-sdk/chromix
COPY --chmod=0755 deploy/entrypoint.sh /usr/local/bin/chromix-manager
ENV PYTHONPATH=/opt/chromix-sdk \
    CLOAKBROWSER_BINARY_PATH=/opt/chromix/chrome \
    CHROMIX_MANAGER_HOME=/data \
    HOME=/home/chromix \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /data
EXPOSE 8820
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python3", "-c", "import http.client as h; c=h.HTTPConnection('127.0.0.1', 8820, timeout=4); c.request('GET', '/'); exit(c.getresponse().status != 200)"]
ENTRYPOINT ["/usr/local/bin/chromix-manager"]
