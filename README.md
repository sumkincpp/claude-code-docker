# Claude Code Docker

**CCD** is python wrapper script to build and run Claude Code in a Docker container.

Docker Container is based on Ubuntu 24.04 with Node.js, python and uv installed.

**Why?**

- It doesn't mess with your local environment (Node.js, Python)
- It allows you to run Claude Code in a isolated and consistent environment
- You can run it for different apps(each app in its own folder) without conflicts

## Requirements

- Docker
- Python 3.10+
- uv - for development

## Usage

Use the `uv` tool to install the `ccd` package from GitHub:

```bash
uv tool install git@github.com:sumkincpp/ccd.git@latest
```

You can use it as follows:

```bash
# Build claude-code docker image
ccd build

# Run claude-code docker container with app in app_folder
ccd run ./app
```

At first run you should call `claude login` inside the container to authenticate with your Claude account.

### Arguments

- `app_folder`: Local directory mounted to `/app` (default: `./app`)
- `--home`: Local directory for Claude config (default: `./home`), only `.claude` and `.claude.json` files are used
- `-v/-vv/-vvv`: Verbosity levels (warning/info/debug/verbose-debug)

### Volume Mounts

- `{app_folder}` → `/app`
- `{home_folder}/.claude` → `/home/ubuntu/.claude`
- `{home_folder}/.claude.json` → `/home/ubuntu/.claude.json`

## Development

Use the `uv`, e.g.

```bash
git clone git@github.com:sumkincpp/ccd.git
cd ccd
uv run ccd --help
```

