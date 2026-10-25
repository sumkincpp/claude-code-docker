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

# https://github.com/nvm-sh/nvm/releases
# API Github can be blocked in some environments, consider setting up a build arg with a specific version
ARG NVM_VERSION=latest
RUN if [ "$NVM_VERSION" = "latest" ]; then \
        echo "Fetching latest nvm version from GitHub"; \
        resp=$(curl --fail-with-body -s https://api.github.com/repos/nvm-sh/nvm/releases/latest); \
        NVM_VERSION=$(printf '%s' "$resp" | grep '"tag_name":' | sed -E 's/.*"v([^"]+)".*/\1/'); \
    fi \
    && echo "Installing nvm version ${NVM_VERSION}"  \
    && curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v${NVM_VERSION}/install.sh | bash \
    && test -f /home/user/.nvm/nvm.sh

ENV NVM_DIR="/home/user/.nvm"
ENV PNPM_CLI_INSTALL_ROOT="/home/user/.local/share/ccd/pnpm-installs"
ENV PNPM_CLI_METADATA_ROOT="/metadata"
# Flat path directly under home so BuildKit cache mount doesn't need to create
# intermediate dirs (which would be owned by root, breaking writes alongside them).
ENV PNPM_STORE_DIR="/home/user/.pnpm-store"

# https://nodejs.org/en/about/previous-releases
# 22 - Maintenance LTS  24 - Active LTS
ARG NVM_NODE_VERSION=24.15.0
ENV PATH="/home/user/.nvm/versions/node/v${NVM_NODE_VERSION}/bin:${PATH}"
RUN . $NVM_DIR/nvm.sh && \
    NVM_NODEJS_ORG_MIRROR=https://unofficial-builds.nodejs.org/download/release \
    nvm install ${NVM_NODE_VERSION}

# Scripts depend on node — copy after node is installed so script changes don't bust node layer
COPY --chown=user:user scripts/. /home/user/.local/bin/.

# https://www.npmjs.com/package/npm?activeTab=versions
ARG NPM_VERSION=latest
# https://github.com/pnpm/pnpm/releases
ARG PNPM_VERSION=latest
RUN --mount=type=cache,id=npm-cache,uid=1000,gid=1000,target=/home/user/.npm \
    . $NVM_DIR/nvm.sh && \
    NPM_RESOLVED=$(node /home/user/.local/bin/resolve-version.mjs npm ${NPM_VERSION} ${NPM_CLI_MIN_RELEASE_AGE_DAYS}) && \
    npm install -g npm@${NPM_RESOLVED} && \
    PNPM_RESOLVED=$(node /home/user/.local/bin/resolve-version.mjs pnpm ${PNPM_VERSION} ${NPM_CLI_MIN_RELEASE_AGE_DAYS}) && \
    corepack enable && \
    corepack prepare "pnpm@${PNPM_RESOLVED}" --activate

# Shared policy args — declared once here, available to all CLI install layers below.
# Changing any of these correctly invalidates all CLI layers (policy change = full CLI rebuild).
ARG NPM_CLI_MIN_RELEASE_AGE_DAYS=7
ARG NPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS=
ARG PNPM_AUDIT_IGNORE_CVES=
ARG NPM_AUDIT_FORCE_FIX_COMPONENTS=

# Each CLI: ARG WITH_* and ARG *_VERSION placed immediately before their RUN so that
# changing one component's args only invalidates that component's layer and later ones.

ARG WITH_CLAUDE=1
ARG CLAUDE_VERSION=latest
RUN --mount=type=cache,id=pnpm-store,uid=1000,gid=1000,target=/home/user/.pnpm-store \
    INSTALL_COMPONENT=claude \
    WITH_CLAUDE="${WITH_CLAUDE}" \
    CLAUDE_VERSION="${CLAUDE_VERSION}" \
    PNPM_CLI_MIN_RELEASE_AGE_DAYS="${NPM_CLI_MIN_RELEASE_AGE_DAYS}" \
    PNPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS="${NPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS}" \
    PNPM_AUDIT_IGNORE_CVES="${PNPM_AUDIT_IGNORE_CVES}" \
    PNPM_AUDIT_FORCE_FIX_COMPONENTS="${NPM_AUDIT_FORCE_FIX_COMPONENTS}" \
    NODE_USE_ENV_PROXY=1 \
    node /home/user/.local/bin/install-clis.mjs

ARG WITH_CODEX=1
ARG CODEX_VERSION=latest
RUN --mount=type=cache,id=pnpm-store,uid=1000,gid=1000,target=/home/user/.pnpm-store \
    INSTALL_COMPONENT=codex \
    WITH_CODEX="${WITH_CODEX}" \
    CODEX_VERSION="${CODEX_VERSION}" \
    PNPM_CLI_MIN_RELEASE_AGE_DAYS="${NPM_CLI_MIN_RELEASE_AGE_DAYS}" \
    PNPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS="${NPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS}" \
    PNPM_AUDIT_IGNORE_CVES="${PNPM_AUDIT_IGNORE_CVES}" \
    PNPM_AUDIT_FORCE_FIX_COMPONENTS="${NPM_AUDIT_FORCE_FIX_COMPONENTS}" \
    NODE_USE_ENV_PROXY=1 \
    node /home/user/.local/bin/install-clis.mjs

ARG WITH_GEMINI=1
ARG GEMINI_VERSION=latest
RUN --mount=type=cache,id=pnpm-store,uid=1000,gid=1000,target=/home/user/.pnpm-store \
    INSTALL_COMPONENT=gemini \
    WITH_GEMINI="${WITH_GEMINI}" \
    GEMINI_VERSION="${GEMINI_VERSION}" \
    PNPM_CLI_MIN_RELEASE_AGE_DAYS="${NPM_CLI_MIN_RELEASE_AGE_DAYS}" \
    PNPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS="${NPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS}" \
    PNPM_AUDIT_IGNORE_CVES="${PNPM_AUDIT_IGNORE_CVES}" \
    PNPM_AUDIT_FORCE_FIX_COMPONENTS="${NPM_AUDIT_FORCE_FIX_COMPONENTS}" \
    NODE_USE_ENV_PROXY=1 \
    node /home/user/.local/bin/install-clis.mjs

ARG WITH_JULES=0
ARG JULES_VERSION=latest
RUN --mount=type=cache,id=pnpm-store,uid=1000,gid=1000,target=/home/user/.pnpm-store \
    INSTALL_COMPONENT=jules \
    WITH_JULES="${WITH_JULES}" \
    JULES_VERSION="${JULES_VERSION}" \
    PNPM_CLI_MIN_RELEASE_AGE_DAYS="${NPM_CLI_MIN_RELEASE_AGE_DAYS}" \
    PNPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS="${NPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS}" \
    PNPM_AUDIT_IGNORE_CVES="${PNPM_AUDIT_IGNORE_CVES}" \
    PNPM_AUDIT_FORCE_FIX_COMPONENTS="${NPM_AUDIT_FORCE_FIX_COMPONENTS}" \
    NODE_USE_ENV_PROXY=1 \
    node /home/user/.local/bin/install-clis.mjs

ARG WITH_OPENCODE=1
ARG OPENCODE_VERSION=latest
RUN --mount=type=cache,id=pnpm-store,uid=1000,gid=1000,target=/home/user/.pnpm-store \
    INSTALL_COMPONENT=opencode \
    WITH_OPENCODE="${WITH_OPENCODE}" \
    OPENCODE_VERSION="${OPENCODE_VERSION}" \
    PNPM_CLI_MIN_RELEASE_AGE_DAYS="${NPM_CLI_MIN_RELEASE_AGE_DAYS}" \
    PNPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS="${NPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS}" \
    PNPM_AUDIT_IGNORE_CVES="${PNPM_AUDIT_IGNORE_CVES}" \
    PNPM_AUDIT_FORCE_FIX_COMPONENTS="${NPM_AUDIT_FORCE_FIX_COMPONENTS}" \
    NODE_USE_ENV_PROXY=1 \
    node /home/user/.local/bin/install-clis.mjs

ARG WITH_COPILOT=1
ARG COPILOT_VERSION=latest
RUN --mount=type=cache,id=pnpm-store,uid=1000,gid=1000,target=/home/user/.pnpm-store \
    INSTALL_COMPONENT=copilot \
    WITH_COPILOT="${WITH_COPILOT}" \
    COPILOT_VERSION="${COPILOT_VERSION}" \
    PNPM_CLI_MIN_RELEASE_AGE_DAYS="${NPM_CLI_MIN_RELEASE_AGE_DAYS}" \
    PNPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS="${NPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS}" \
    PNPM_AUDIT_IGNORE_CVES="${PNPM_AUDIT_IGNORE_CVES}" \
    PNPM_AUDIT_FORCE_FIX_COMPONENTS="${NPM_AUDIT_FORCE_FIX_COMPONENTS}" \
    NODE_USE_ENV_PROXY=1 \
    node /home/user/.local/bin/install-clis.mjs

ARG WITH_PI=1
ARG PI_VERSION=latest
RUN --mount=type=cache,id=pnpm-store,uid=1000,gid=1000,target=/home/user/.pnpm-store \
    INSTALL_COMPONENT=pi \
    WITH_PI="${WITH_PI}" \
    PI_VERSION="${PI_VERSION}" \
    PNPM_CLI_MIN_RELEASE_AGE_DAYS="${NPM_CLI_MIN_RELEASE_AGE_DAYS}" \
    PNPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS="${NPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS}" \
    PNPM_AUDIT_IGNORE_CVES="${PNPM_AUDIT_IGNORE_CVES}" \
    PNPM_AUDIT_FORCE_FIX_COMPONENTS="${NPM_AUDIT_FORCE_FIX_COMPONENTS}" \
    NODE_USE_ENV_PROXY=1 \
    node /home/user/.local/bin/install-clis.mjs

RUN ln -sf /home/user/.nvm/versions/node/v${NVM_NODE_VERSION}/bin/* /home/user/.local/bin/

ARG WITH_RUST=1
ARG RUSTUP_VERSION=latest
RUN if [ "${WITH_RUST}" = "1" ]; then \
    if [ "${RUSTUP_VERSION}" = "latest" ]; then \
      unset RUSTUP_VERSION; \
    fi; \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y; \
  fi

ARG UV_VERSION=latest
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
    if [ "${WITH_COPILOT}" = "1" ]; then copilot --version; fi && \
    if [ "${WITH_PI}" = "1" ]; then pi --version; fi

#####################################################################################
# Install local-claude wrapper for Ollama integration
#####################################################################################
COPY --chown=user:user local-claude/local-claude /home/user/.local/bin/local-claude
RUN chmod +x /home/user/.local/bin/local-claude

WORKDIR /app

USER root
# Entrypoint to initialize /app environment
COPY docker/ccd-entrypoint /usr/local/bin/ccd-entrypoint
RUN chmod +x /usr/local/bin/ccd-entrypoint
USER user

# claude code
ENV DISABLE_AUTOUPDATER=1

ENTRYPOINT ["/usr/local/bin/ccd-entrypoint"]
CMD ["bash"]
