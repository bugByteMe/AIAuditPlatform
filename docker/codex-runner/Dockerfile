FROM node:22-bookworm-slim

ARG CODEX_VERSION=0.153.4

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    git \
    python3 \
    python3-pip \
    ripgrep \
  && npm install -g "@openai/codex@${CODEX_VERSION}" \
  && useradd --create-home --shell /bin/bash codex \
  && mkdir -p /workspace \
  && chown -R codex:codex /workspace /home/codex \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

USER codex
WORKDIR /workspace

ENV CODEX_HOME=/home/codex/.codex

CMD ["codex", "--help"]
