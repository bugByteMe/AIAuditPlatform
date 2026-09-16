FROM node:22-bookworm-slim AS node-runtime

FROM python:3.12-slim-bookworm

ARG CODEX_VERSION=0.153.4
ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/bin/npm /usr/local/bin/npm
COPY --from=node-runtime /usr/local/bin/npx /usr/local/bin/npx
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN sed -i 's#http://deb.debian.org/debian#http://mirrors.cloud.aliyuncs.com/debian#g; s#http://deb.debian.org/debian-security#http://mirrors.cloud.aliyuncs.com/debian-security#g' /etc/apt/sources.list.d/debian.sources \
  && apt-get update \
  && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    git \
    libreoffice \
    pandoc \
    poppler-utils \
    ripgrep \
    tesseract-ocr \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

RUN python3.12 -m pip install --no-cache-dir --upgrade pip \
  && python3.12 -m pip install --no-cache-dir \
    --index-url "${PIP_INDEX_URL}" \
    --trusted-host "${PIP_TRUSTED_HOST}" \
    defusedxml \
    docx2txt \
    lxml \
    markitdown \
    numpy \
    openpyxl \
    pandas \
    pdf2image \
    pdfplumber \
    pillow \
    pypdf \
    pypdfium2 \
    pytesseract \
    python-docx \
    reportlab

RUN npm install -g "@openai/codex@${CODEX_VERSION}" --registry="${NPM_REGISTRY}" \
  && useradd --create-home --shell /bin/bash codex \
  && mkdir -p /workspace /home/codex/.codex \
  && chown -R codex:codex /workspace /home/codex

USER codex
WORKDIR /workspace

ENV CODEX_HOME=/home/codex/.codex

CMD ["codex", "--help"]
