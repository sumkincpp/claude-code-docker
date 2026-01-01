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

# Entrypoint to initialize /app environment
RUN cat <<'EOF' >/usr/local/bin/ccd-entrypoint
#!/usr/bin/env bash
set -e

export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/app/.venv2}"
export CCD_APP_DIR="${CCD_APP_DIR:-/app}"

init_file=""
if [ -n "${CCD_INIT_FILE:-}" ]; then
  init_file="${CCD_INIT_FILE}"
elif [ -f /app/.ccd_env ]; then
  init_file="/app/.ccd_env"
elif [ -f /app/.ccd-init.sh ]; then
  init_file="/app/.ccd-init.sh"
fi

if [ -n "${init_file}" ]; then
  # shellcheck source=/dev/null
  . "${init_file}"
fi

exec "$@"
EOF
RUN chmod +x /usr/local/bin/ccd-entrypoint

USER ubuntu

ENV PATH="/home/ubuntu/.local/bin:/home/ubuntu/.cargo/bin:$PATH"
ENV UV_PROJECT_ENVIRONMENT="/app/.venv2"

# https://github.com/nvm-sh/nvm/releases
ARG NVM_VERSION=0.40.3
# https://nodejs.org/en/about/previous-releases
# 22 - Maintenance LTS
# 24 - Active LTS
ARG NVM_NODE_VERSION=24

# Optional build features (set --build-arg WITH_*=0 to disable)
ARG WITH_RUST=1
ARG WITH_CLAUDE=1
ARG WITH_CODEX=1
ARG WITH_GEMINI=1
ARG WITH_OPENCODE=1
ARG WITH_COPILOT=1

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
    if [ "${WITH_CLAUDE}" = "1" ]; then npm install -g @anthropic-ai/claude-code; fi && \
    if [ "${WITH_CODEX}" = "1" ]; then npm install -g @openai/codex; fi && \
    if [ "${WITH_GEMINI}" = "1" ]; then npm install -g @google/gemini-cli; fi && \
    if [ "${WITH_OPENCODE}" = "1" ]; then npm install -g opencode-ai; fi && \
    if [ "${WITH_COPILOT}" = "1" ]; then npm install -g @github/copilot; fi && \
    echo "Let's symlink the nvm directory to the local bin directory" && \
    NODE_VERSION=$(nvm current) && \
    mkdir -p /home/ubuntu/.local/bin && \
    echo "Symlinking Node.js and npm binaries to /home/ubuntu/.local/bin" && \
    ln -sf /home/ubuntu/.nvm/versions/node/$NODE_VERSION/bin/* /home/ubuntu/.local/bin/

# Install Rust toolchain only when requested
RUN if [ "${WITH_RUST}" = "1" ]; then \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y; \
  fi

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# Verify installations
RUN node -v && \
    npm -v && \
    python3 --version && \
    uv --version && \
    if [ "${WITH_CLAUDE}" = "1" ]; then claude --version; fi && \
    if [ "${WITH_GEMINI}" = "1" ]; then gemini --version; fi && \
    if [ "${WITH_CODEX}" = "1" ]; then codex --version; fi && \
    if [ "${WITH_OPENCODE}" = "1" ]; then opencode --version; fi && \
    if [ "${WITH_COPILOT}" = "1" ]; then copilot --version; fi

WORKDIR /app

ENTRYPOINT ["/usr/local/bin/ccd-entrypoint"]
CMD ["bash"]
