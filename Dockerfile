FROM ubuntu:24.04

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
    ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Create app directory and set permissions
RUN mkdir -p /app && \
    chown -R ubuntu:ubuntu /app && \
    chmod -R 755 /app

USER ubuntu

ENV PATH="/home/ubuntu/.local/bin:$PATH"

# https://github.com/nvm-sh/nvm/releases
ARG NVM_VERSION=0.40.3
# https://nodejs.org/en/about/previous-releases
# 22 - Maintenance LTS
# 24 - Active LTS
ARG NVM_NODE_VERSION=24

# Install nvm and Node.js with proper environment
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v${NVM_VERSION}/install.sh | bash

ENV NVM_DIR="/home/ubuntu/.nvm"
# https://github.com/google-gemini/gemini-cli
# https://help.openai.com/en/articles/11096431-openai-codex-cli-getting-started
# https://github.com/github/copilot-cli?locale=en-US
RUN . $NVM_DIR/nvm.sh && \
    nvm install ${NVM_NODE_VERSION} && \
    nvm use ${NVM_NODE_VERSION} && \
    npm install -g npm@latest && \
    npm install -g @anthropic-ai/claude-code && \
    npm install -g @openai/codex  && \
    npm install -g @google/gemini-cli && \
    npm install -g opencode-ai && \
    npm install -g @github/copilot && \
    echo "Let's symlink the nvm directory to the local bin directory" && \
    NODE_VERSION=$(nvm current) && \
    mkdir -p /home/ubuntu/.local/bin && \
    echo "Symlinking Node.js and npm binaries to /home/ubuntu/.local/bin" && \
    ln -sf /home/ubuntu/.nvm/versions/node/$NODE_VERSION/bin/* /home/ubuntu/.local/bin/

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# Verify installations
RUN node -v && \
    npm -v && \
    python3 --version && \
    uv --version && \
    claude --version && \
    gemini --version && \
    codex --version && \
    opencode --version && \
    copilot --version

WORKDIR /app

CMD ["bash"]