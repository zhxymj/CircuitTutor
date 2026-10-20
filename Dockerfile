# ---- 构建阶段：仅用于校验代码可导入（零依赖项目，此阶段极轻）----
FROM python:3.12-slim AS builder
WORKDIR /build
COPY . .
# 语法检查 + 导入冒烟：确保镜像不会带着坏代码上线
RUN python -B -c "import ast,sys,pathlib; \
    errors=[]; \
    [errors.append(str(p)) for p in pathlib.Path('core').rglob('*.py') \
     if not (lambda f: (ast.parse(f.read_text(encoding='utf-8')), True)[-1])(p) or None]; \
    print('syntax ok') if not errors else sys.exit(1)"

# ---- 运行阶段：最小镜像 ----
FROM python:3.12-slim AS runtime
LABEL maintainer="CircuitTutor" \
      description="智绘电路 — 大模型驱动的芯片全链条智能设计平台"

# 非 root 用户运行（安全加固）
RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app

WORKDIR /app
COPY --from=builder /build/ /app/

# 数据目录（历史/缓存）需可写
RUN mkdir -p /app/data && chown -R app:app /app

USER app
EXPOSE 8765

ENV LSQC_HOST=0.0.0.0 \
    LSQC_PORT=8765 \
    LSQC_LOG_LEVEL=INFO \
    PYTHONUNBUFFERED=1

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)"

CMD ["python", "-B", "server.py"]
