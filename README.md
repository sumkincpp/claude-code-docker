# Claude Code Docker

**CCD** is a Python wrapper to build and run AI coding assistants inside a Docker container.

It builds a Docker image with one or more assistant CLIs and runs them against your local app folder.
Toolchains stay isolated while your project files remain local and editable.

Base image: Ubuntu 24.04 with Node.js, Python, and uv installed.

**Why?**

- Avoid installing assistant toolchains on your host.
- Keep a consistent, reproducible runtime across projects.
- Use separate app folders without cross-project conflicts.

## Quickstart

```bash
git clone git@github.com:sumkincpp/claude-code-docker.git
cd claude-code-docker
uv tool install . -e
ccd build
ccd run .
```

## Features (Build-Time)

Default build includes all clients and rust; you can include or exclude features.
Exclude `rust` with `--without rust` to keep the image smaller.

### CLI Clients

- `claude` - Anthropic Claude Code CLI
- `codex` - OpenAI Codex CLI
- `gemini` - Google Gemini CLI
- `opencode` - OpenCode CLI
- `copilot` - GitHub Copilot CLI
- `jules` - Jules CLI (disabled by default)

### Runtimes

- `rust` - Rust toolchain via rustup

## Requirements

- Docker
- Python 3.12+
- uv (required for Quickstart and development)
- Host OS: Linux or macOS (Windows via WSL2).

## CLI Reference

### Build Image (ccd build)

```bash
ccd build [--with feature1,feature2,...] [--without feature3,feature4,...]
```

- `--with`: Comma-separated feature list to include (default: all features).
- `--without`: Comma-separated feature list to exclude.

Available build features:

- `rust`
- `claude`
- `codex`
- `gemini`
- `jules`
- `opencode`
- `copilot`

Examples:

```bash
ccd build --with rust,gemini
ccd build --without opencode,copilot
#
# building image without cache
ccd -vv build --no-cache
```

With that an image named `claude-code:latest` is built.

### Run Container (ccd run / ccd .)

```bash
ccd run [app_folder] [--home home_folder] [-v|-vv|-vvv]
```

- `app_folder`: Local directory mounted to `/app` (default: `.`).
- `--home`: Local directory for assistant config (default: `$HOME/.claude-code-docker`).
- `-v/-vv/-vvv`: Verbosity levels (warning/info/debug/verbose-debug).
- Alias: `ccd .` is the same as `ccd run .`.

Examples:

```bash
ccd run /path/to/app
ccd .
```

### Attach to Running Container (ccd attach)

If a container started with `ccd run` is already running, attach to it:

```bash
ccd attach
```

## Usage

### Authentication

On the first run, open a shell in the container and authenticate:

```bash
claude login
```

Other CLI login commands (available only if the client is installed; verify with each CLI's `--help` if needed):

- `codex login`
- `gemini auth login`
- `opencode auth login`
- `copilot auth login`

When CLI tools are run, they also inform you if authentication is needed.

## Local Claude: Using Claude Code with Ollama

The image includes `local-claude`, a wrapper that connects Claude Code to Ollama for local model execution.

Learn more about Ollama at [Claude Code with Anthropic API compatibility](https://ollama.com/blog/claude)

### Setup

Create a `.local-claude.env` file in your application directory:

```bash
# Ollama server URL
OLLAMA_BASE_URL=http://localhost:11434

# Default model (must support tools!)
OLLAMA_DEFAULT_MODEL=qwen2.5-coder:7b
```

Start CCD container and use `local-claude` to run Claude Code with Ollama.

The wrapper searches for config in:

1. `~/.local-claude.env`
2. Current directory `.local-claude.env`
3. `/app/.local-claude.env`

### Usage

List available models with tool support detection:

```bash
local-claude list
```

Run Claude Code with Ollama:

```bash
local-claude run                      # Use default model
local-claude run -m qwen2.5-coder:7b  # Use specific model
```

> Note: changing models with "/model <NAME>" is supported in claude-code CLI

The wrapper:

- Verifies Ollama connection
- Checks model availability
- Validates tool support via `/api/show` endpoint
- Sets required environment variables for local model usage
- Launches claude-code CLI

### Model Requirements

Models must support tool/function calling.

Ollama recommends the following models for use with Claude Code:

Local models -

- gpt-oss:20b
- qwen3-coder

Cloud models -

- glm-4.7:cloud
- minimax-m2.1:cloud

Use models with 32K+ context length for best results.

## Configuration

### Volume Mounts

The following host paths are mounted into the container:

- `{app_folder}` -> `/app` (container working directory)
- `{home_folder}/.claude` -> `/home/ubuntu/.claude`
- `{home_folder}/.claude.json` -> `/home/ubuntu/.claude.json`
- `{home_folder}/.gemini` -> `/home/ubuntu/.gemini`
- `{home_folder}/.codex` -> `/home/ubuntu/.codex`
- `{home_folder}/.copilot` -> `/home/ubuntu/.copilot`

The `--home` directory is expected to contain the assistant config folders shown above.

### Init File

On container start, the entrypoint sets defaults and optionally sources an init file:

- Default variables: `UV_PROJECT_ENVIRONMENT=/app/.venv2`, `CCD_APP_DIR=/app`.
- Init file resolution order: `$CCD_INIT_FILE` (if set), `/app/.ccd_env`, `/app/.ccd-init.sh`.
- If present, the init file is sourced, and you can override defaults there.

Example init file:

```bash
export UV_PROJECT_ENVIRONMENT=/app/.venv
export CCD_APP_DIR=/app
```

## Development

```bash
git clone git@github.com:sumkincpp/claude-code-docker.git
cd claude-code-docker
uv run ccd --help
```

## FAQ

- Q: Where do credentials live on the host?
- A: Under the `--home` directory (default: `$HOME/.claude-code-docker`), mounted into `/home/ubuntu`.

- Q: Can I use a custom container name?
- A: The name is auto-generated based on the app folder name as `ccd-{folder_name}`. You can attach via `ccd attach` without knowing it.
