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

ENV PATH="/home/ubuntu/.local/bin:/home/ubuntu/.cargo/bin:$PATH"
ENV UV_PROJECT_ENVIRONMENT="/app/.venv2"

# Component versions (set --build-arg <COMPONENT>_VERSION=X.Y.Z to specify version)
# https://github.com/nvm-sh/nvm/releases
# ARG NVM_VERSION=0.40.3
ARG NVM_VERSION=latest
# https://nodejs.org/en/about/previous-releases
# 22 - Maintenance LTS
# 24 - Active LTS
ARG NVM_NODE_VERSION=24
ARG NPM_VERSION=latest
ARG UV_VERSION=latest
ARG RUSTUP_VERSION=latest
#ARG CLAUDE_VERSION=latest
ARG CLAUDE_VERSION=2.0.76
ARG CODEX_VERSION=latest
ARG GEMINI_VERSION=latest
ARG JULES_VERSION=latest
ARG OPENCODE_VERSION=latest
ARG COPILOT_VERSION=latest

# Optional build features (set --build-arg WITH_*=0 to disable)
ARG WITH_RUST=1
ARG WITH_CLAUDE=1
ARG WITH_CODEX=1
ARG WITH_GEMINI=1
ARG WITH_JULES=1
ARG WITH_OPENCODE=1
ARG WITH_COPILOT=1

# Install nvm - fetch latest if NVM_VERSION=latest, otherwise use provided version
RUN if [ "$NVM_VERSION" = "latest" ]; then \
        NVM_VERSION=$(curl -s https://api.github.com/repos/nvm-sh/nvm/releases/latest | grep '"tag_name":' | sed -E 's/.*"v([^"]+)".*/\1/'); \
    fi \
    && echo "Installing nvm version ${NVM_VERSION}"  \
    && curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v${NVM_VERSION}/install.sh | bash

ENV NVM_DIR="/home/ubuntu/.nvm"
# https://github.com/google-gemini/gemini-cli
# https://help.openai.com/en/articles/11096431-openai-codex-cli-getting-started
# https://github.com/github/copilot-cli?locale=en-US
RUN . $NVM_DIR/nvm.sh && \
    nvm install ${NVM_NODE_VERSION} && \
    nvm use ${NVM_NODE_VERSION} && \
    npm install -g npm@${NPM_VERSION} && \
    if [ "${WITH_CLAUDE}" = "1" ]; then npm install -g @anthropic-ai/claude-code@${CLAUDE_VERSION}; fi && \
    if [ "${WITH_CODEX}" = "1" ]; then npm install -g @openai/codex@${CODEX_VERSION}; fi && \
    if [ "${WITH_GEMINI}" = "1" ]; then npm install -g @google/gemini-cli@${GEMINI_VERSION}; fi && \
    if [ "${WITH_JULES}" = "1" ]; then npm install -g @google/jules@${JULES_VERSION}; fi && \
    if [ "${WITH_OPENCODE}" = "1" ]; then npm install -g opencode-ai@${OPENCODE_VERSION}; fi && \
    if [ "${WITH_COPILOT}" = "1" ]; then npm install -g @github/copilot@${COPILOT_VERSION}; fi && \
    echo "Let's symlink the nvm directory to the local bin directory" && \
    NODE_VERSION=$(nvm current) && \
    mkdir -p /home/ubuntu/.local/bin && \
    echo "Symlinking Node.js and npm binaries to /home/ubuntu/.local/bin" && \
    ln -sf /home/ubuntu/.nvm/versions/node/$NODE_VERSION/bin/* /home/ubuntu/.local/bin/

# Install Rust toolchain only when requested
RUN if [ "${WITH_RUST}" = "1" ]; then \
    if [ "${RUSTUP_VERSION}" = "latest" ]; then \
      unset RUSTUP_VERSION; \
    fi; \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y; \
  fi

# Install uv
RUN if [ "${UV_VERSION}" = "latest" ]; then \
    curl -LsSf https://astral.sh/uv/install.sh | sh; \
  else \
    curl -LsSf https://astral.sh/uv/${UV_VERSION}/install.sh | sh; \
  fi

# Verify installations
RUN node -v && \
    npm -v && \
    python3 --version && \
    uv --version && \
    if [ "${WITH_CLAUDE}" = "1" ]; then claude --version; fi && \
    if [ "${WITH_GEMINI}" = "1" ]; then gemini --version; fi && \
    if [ "${WITH_JULES}" = "1" ]; then jules --version; fi && \
    if [ "${WITH_CODEX}" = "1" ]; then codex --version; fi && \
    if [ "${WITH_OPENCODE}" = "1" ]; then opencode --version; fi && \
    if [ "${WITH_COPILOT}" = "1" ]; then copilot --version; fi

WORKDIR /app

USER root
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

# claude code
ENV DISABLE_AUTOUPDATER=1

ENTRYPOINT ["/usr/local/bin/ccd-entrypoint"]
CMD ["bash"]
