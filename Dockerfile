FROM ubuntu:24.04

WORKDIR /app

RUN mkdir -p /app && chown -R ubuntu:ubuntu /app

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get upgrade -y && \
    apt-get install -y \
    curl \
    git \
    bash \
    python3 \
    python3-dev \
    python3-pip \
    build-essential \
    ca-certificates \
    gnupg \
    lsb-release \
    && rm -rf /var/lib/apt/lists/*

USER ubuntu

RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash


# Add nvm to PATH for future shell sessions
ENV NVM_DIR="/home/ubuntu/.nvm"
ENV PATH="$NVM_DIR/versions/node/v22.16.0/bin:/home/ubuntu/.nvm/:/home/ubuntu/.local/bin:$PATH"
RUN mkdir -p /home/ubuntu/.local/bin && \
    echo '#!/bin/bash\nsource ~/.nvm/nvm.sh\nnvm "$@"' > /home/ubuntu/.local/bin/nvm && \
    chmod +x /home/ubuntu/.local/bin/nvm

RUN nvm install 22

# Install the Anthropic Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# Add uv to PATH
ENV PATH="/home/ubuntu/.local/bin:$PATH"

# Verify installations
RUN node -v && \
    nvm current && \
    npm -v && \
    python3 --version && \
    uv --version && \
    claude --version

CMD ["bash"]