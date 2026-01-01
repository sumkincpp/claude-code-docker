# Claude Code Docker

**CCD** is python wrapper script to build and run Claude Code and other AI coding assistants inside Docker container.

Docker Container is based on Ubuntu 24.04 with Node.js, python and uv installed.

**Why?**

- It doesn't mess with your local environment (Node.js, Python)
- It allows you to run Claude Code in a isolated and consistent environment
- You can run it for different apps(each app in its own folder) without conflicts

## Features

By default all features are included, but you can customize the build to include or exclude specific features.

- Following CLI clients can be installed inside the container:

  - `claude` - Anthropic Claude Code CLI
  - `codex` - OpenAI Codex CLI
  - `gemini` - Google Gemini CLI
  - `opencode` - OpenCode CLI
  - `copilot` - GitHub Copilot CLI

- Optionally following runtimes can be installed:

  - `rust` - Rust toolchain via rustup

## Requirements

- Docker
- Python 3.12+
- uv - for development

## Usage

You can use it as follows:

```bash
# Build claude-code docker image
$ git clone git@github.com:sumkincpp/claude-code-docker.git
$ cd claude-code-docker
# Install tool globally as editable package pointing to current folder
$ uv tool install . -e
$ ccd build

# Build options
$ ccd build --with rust,gemini
$ ccd build --without opencode,copilot

# Run claude-code docker container with app folder mounted
$ ccd run /home/user/my-code/my-app

# Or run claude-code docker container within current folder
$ cd /home/user/my-code/my-app
$ ccd .
Starting container 'ccd-ltm-agent-01'...
ubuntu@ccd-ltm-agent-01:/app$ exit
exit
```

At first run you should call `claude login` inside the container to authenticate with your Claude account.

Normally you would run `ccd` from the root of your app folder, which will be mounted to `/app` inside the container:

```bash
(ltm-agent) vagrant@vagrant:~/Code/ltm-agent$ ccd -v run .
INFO: Preparing to run container: claude-code
Starting container 'ccd-ltm-agent-01'...
INFO: Starting container with:
INFO:   App folder: /home/vagrant/Code/ltm-agent
INFO:   Home folder: /home/vagrant/.claude-code-docker
INFO:   Full command: docker run -it --rm --hostname ccd-ltm-agent-01 --name ccd-ltm-agent-01 -v /home/vagrant/Code/ltm-agent:/app -v /home/vagrant/.claude-code-docker/.claude:/home/ubuntu/.claude -v /home/vagrant/.claude-code-docker/.claude.json:/home/ubuntu/.claude.json -v /home/vagrant/.claude-code-docker/.gemini:/home/ubuntu/.gemini -v /home/vagrant/.claude-code-docker/.codex:/home/ubuntu/.codex -v /home/vagrant/.claude-code-docker/.copilot:/home/ubuntu/.copilot claude-code
ubuntu@ccd-ltm-agent-01:/app$

...continue with claude commands...
```

### Builing image (ccd build)

To build the Docker image, use:

```bash
ccd build [--with feature1,feature2,...] [--without feature3,feature
```

- `--with`: Comma-separated feature list to include in the image (default: all features)
- `--without`: Comma-separated feature list to exclude from the image

Available build features:
- `rust`
- `claude`
- `codex`
- `gemini`
- `opencode`
- `copilot`

### Running container (ccd run / ccd .)

```bash
ccd run [app_folder] [--home home_folder] [-v|-vv|-vvv
```

- `app_folder`: Local directory mounted to `/app` (default: `./app`)
- `--home`: Local directory for Claude config (default: `$HOME/.claude-code-docker`), only `.claude` and `.claude.json` files are used
- `-v/-vv/-vvv`: Verbosity levels (warning/info/debug/verbose-debug)

#### Volume Mounts

The following host paths are mounted into the container when it is run:

| Host Path                    | Container Path              |
| ---------------------------- | --------------------------- |
| `{app_folder}`               | `/app`                      |
| `{home_folder}/.claude`      | `/home/ubuntu/.claude`      |
| `{home_folder}/.claude.json` | `/home/ubuntu/.claude.json` |
| `{home_folder}/.gemini`      | `/home/ubuntu/.gemini`      |
| `{home_folder}/.codex`       | `/home/ubuntu/.codex`       |
| `{home_folder}/.copilot`     | `/home/ubuntu/.copilot`     |

#### Environment Initialization

On container start, the entrypoint sets defaults and optionally sources an init file:

- Default variables: `UV_PROJECT_ENVIRONMENT=/app/.venv2`, `CCD_APP_DIR=/app`.
- Init file resolution order: `$CCD_INIT_FILE` (if set), `/app/.ccd_env`, `/app/.ccd-init.sh`.
- If present, the init file is sourced, and you can override any defaults there.

It is also possible to attach to a running container via `ccd attach` command.

#### Attach to Running Container (ccd attach)

When you have a running container started with `ccd run`, you can attach to it using:

```bash
ccd attach
```

## Development

Use the `uv`, e.g.

```bash
git clone git@github.com:sumkincpp/claude-code-docker.git
cd ccd
uv run ccd --help
```
