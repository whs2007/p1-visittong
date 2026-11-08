# 生产镜像：只装锁定依赖，非 root 运行，带健康检查。
# 演示请用 README 的三步本地启动；本镜像面向「以 strict 模式部署」。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AUTH_MODE=strict \
    HOST=0.0.0.0 \
    PORT=8020

WORKDIR /app

# 先装依赖，利用层缓存：代码改动不会触发重装
COPY backend/requirements.lock ./requirements.lock
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.lock

COPY backend ./backend
COPY frontend ./frontend

# 非 root：容器内被攻破时降低影响面
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/backend/data \
    && chown -R appuser:appuser /app
USER appuser

WORKDIR /app/backend
EXPOSE 8020

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8020/api/health', timeout=2).status==200 else 1)"

# 单 worker 默认；横向扩展用 docker compose scale，并给非首个副本设 RECONCILE_ON_START=0
CMD ["python", "run.py"]
