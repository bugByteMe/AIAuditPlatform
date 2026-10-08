FROM python:3.12-slim-bookworm

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AI_AUDIT_CONFIG=/app/config/ai_audit.json

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    ca-certificates \
    fonts-noto-cjk \
    libreoffice-calc \
    libreoffice-core \
    libreoffice-writer \
    tini \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN python -m pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r /app/backend/requirements.txt

COPY backend /app/backend
COPY frontend /app/frontend
COPY config /app/config

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin ai-audit \
  && mkdir -p /mnt/workspace_storage /etc/ai-audit \
  && chown -R ai-audit:ai-audit /app /mnt/workspace_storage /etc/ai-audit

USER ai-audit

EXPOSE 8000
VOLUME ["/mnt/workspace_storage"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).read()"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "backend/server.py"]
