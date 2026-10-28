# syntax=docker/dockerfile:1
FROM alpine:3.24

RUN apk add --no-cache \
    curl \
    git \
    bash \
    python3 \
    python3-dev \
    py3-pip \
    build-base \
    linux-headers \
    ca-certificates \
    gnupg \
    ripgrep \
    bubblewrap \
    vim

# Create app and metadata directories and set permissions
RUN mkdir -p /app /metadata && \
    addgroup -g 1000 user && \
    adduser -D -u 1000 -G user -s /bin/bash user && \
    chown -R user:user /app /metadata && \
    chmod -R 755 /app /metadata

USER user

ENV PATH="/home/user/.local/bin:/home/user/.cargo/bin:$PATH"
ENV UV_PROJECT_ENVIRONMENT="/app/.venv2"

RUN python3 --version
