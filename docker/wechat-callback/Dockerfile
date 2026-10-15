FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AI_AUDIT_WECHAT_CALLBACK_DB_PATH=/var/lib/ai-audit-wechat/receipts.sqlite3

WORKDIR /app/backend
COPY backend/requirements-wechat-callback.txt /app/backend/requirements-wechat-callback.txt
RUN python -m pip install --no-cache-dir -r requirements-wechat-callback.txt

COPY backend/wechat_callback_inbox.py backend/wechat_callback_verifier.py backend/wechat_callback_service.py backend/wechat_callback_main.py /app/backend/
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin ai-audit \
  && mkdir -p /var/lib/ai-audit-wechat \
  && chown ai-audit:ai-audit /var/lib/ai-audit-wechat
USER ai-audit

EXPOSE 8001
VOLUME ["/var/lib/ai-audit-wechat"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=3).read()"]
CMD ["uvicorn", "wechat_callback_main:app", "--host", "0.0.0.0", "--port", "8001"]
