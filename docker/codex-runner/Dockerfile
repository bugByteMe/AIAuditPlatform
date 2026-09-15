FROM node:22-bookworm-slim

ARG CODEX_VERSION=0.153.4
ARG NPM_REGISTRY=https://registry.npmmirror.com

RUN sed -i 's#http://deb.debian.org/debian#http://mirrors.cloud.aliyuncs.com/debian#g; s#http://deb.debian.org/debian-security#http://mirrors.cloud.aliyuncs.com/debian-security#g' /etc/apt/sources.list.d/debian.sources \
  && apt-get update \
  && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    git \
    python3 \
    python3-pip \
    ripgrep \
  && npm install -g "@openai/codex@${CODEX_VERSION}" --registry="${NPM_REGISTRY}" \
  && useradd --create-home --shell /bin/bash codex \
  && mkdir -p /workspace \
  && chown -R codex:codex /workspace /home/codex \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

USER codex
WORKDIR /workspace

ENV CODEX_HOME=/home/codex/.codex

CMD ["codex", "--help"]
