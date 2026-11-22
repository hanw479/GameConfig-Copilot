# syntax=docker/dockerfile:1
#
# GameConfig Copilot 运行镜像（S8-01）。
#
# 设计约束
# --------
# * base = python:3.11-slim：`fastmcp`（MCP 传输）要求 Python >= 3.11；
#   宿主若只有 3.10，MCP 传输不可用，但 CLI / API / 评测仍可运行。
# * 非 root：容器以 uid/gid 10001 的 `app` 用户运行；写入范围只有 /app。
# * 依赖全部来自 pyproject 的可选组（[dev,api,rag,ui]；MCP 镜像再加 [mcp]），
#   每个组缺失时对应功能会优雅降级，但镜像里一次装齐，避免运行期缺包。
# * 构建期预热检索索引（out/index），容器启动即可离线检索，不依赖网络。
# * healthcheck 打 /healthz（不需要 LLM key / 索引，lazy build）。
#
# 构建：
#   docker build -t gameconfig-copilot:local .
#   docker build --build-arg INSTALL_MCP=true -t gameconfig-copilot:mcp .
# 运行：
#   docker run --rm -p 127.0.0.1:8000:8000 -e GCC_LLM_API_KEY=sk-... gameconfig-copilot:local

FROM python:3.11-slim

# 是否安装 MCP 传输后端（fastmcp）。默认 false：只读展示不需要协议层。
ARG INSTALL_MCP=false

# GCC_MCP_MODE 的安全默认是只读；演示写模式由 compose / -e 显式打开。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GCC_INDEX_DIR=/app/out/index \
    GCC_TASKS_DIR=/app/data/tasks \
    GCC_TRACE_PATH=/app/traces/traces.jsonl \
    GCC_MCP_MODE=readonly

# 非 root 运行用户（不装 build-essential：依赖都有 cp311 wheel）。
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --create-home --home-dir /home/app app

WORKDIR /app

# ---- 依赖层：先装打包元数据与源码，命中缓存时不因数据/文档变化重装 ----
COPY --chown=app:app pyproject.toml README.md ./
COPY --chown=app:app src ./src

# 必须用 editable 安装：writer/memory 用 `__file__` 上溯仓库根（REPO_ROOT=/app），
# 非 editable 装进 site-packages 会把 REPO_ROOT 算到 Python 安装目录。
RUN pip install --no-cache-dir -e ".[dev,api,rag,ui]" \
 && if [ "$INSTALL_MCP" = "true" ]; then \
        pip install --no-cache-dir -e ".[mcp]"; \
    fi

# ---- 运行时资产：语料、Prompt、配置、测试、面板 ----
COPY --chown=app:app data ./data
COPY --chown=app:app prompts ./prompts
COPY --chown=app:app config ./config
COPY --chown=app:app tests ./tests
COPY --chown=app:app scripts ./scripts
# 面板“指标”页读取的已入库基线（docs/reports 在 .dockerignore 中单独放行）
COPY --chown=app:app docs/reports ./docs/reports

# 可写目录 + 整体交给 app；/app 由 WORKDIR 创建时属于 root，必须显式 chown。
RUN mkdir -p /app/out /app/traces \
 && chown -R app:app /app

USER app

# 构建期预热索引：容器启动即有 out/index（dense=noop，BM25 + RRF）。
# 语料指纹未变时重复构建是 no-op，因此该层可缓存。
RUN gcc ingest --source data --index out/index

EXPOSE 8000 8501

# 不依赖 curl：slim 镜像里用标准库探活。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).getcode() == 200 else 1)"

CMD ["python", "-m", "uvicorn", "gameconfig_copilot.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
