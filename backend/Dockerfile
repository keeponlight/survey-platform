# 后端镜像（api 与 worker 复用同一镜像，仅启动命令不同，见 architecture.md D1/C2）。
# 构建上下文 = 项目根（见 deploy/compose.yaml：context: ..）。
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 使用与开发一致的 uv 版本。
COPY --from=ghcr.io/astral-sh/uv:0.12.14 /uv /usr/local/bin/uv

# 只装运行时依赖（dev 组不装）。
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev

# 应用源码
COPY backend/ ./

EXPOSE 8000
# 默认以 api 角色启动；worker 由 compose 覆盖 command。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
