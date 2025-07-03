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

You can use it as follows:

```bash
# Build claude-code docker image
git clone git@github.com:sumkincpp/claude-code-docker.git
cd claude-code-docker
# Install tool globally as editable package pointing to current folder
uv tool install . -e
ccd build

# Run claude-code docker container within current folder
cd /home/user/my-code/my-app
ccd run .
```

At first run you should call `claude login` inside the container to authenticate with your Claude account.

Normally you would run `ccd` from the root of your app folder, which will be mounted to `/app` inside the container:

```bash
vagrant@vagrant:~/Code/glances$ ccd -v run .
INFO: Preparing to run container: claude-code
INFO: Starting container with:
INFO:   App folder: /home/vagrant/Code/glances
INFO:   Home folder: /home/vagrant/.claude-code-docker
INFO:   Full command: docker run -it --rm -v /home/vagrant/Code/glances:/app -v /home/vagrant/.claude-code-docker/.claude:/home/ubuntu/.claude -v /home/vagrant/.claude-code-docker/.claude.json:/home/ubuntu/.claude.json claude-code

ubuntu@4287aebeaada:/app$ ...continue with claude commands...
```

### Arguments

- `app_folder`: Local directory mounted to `/app` (default: `./app`)
- `--home`: Local directory for Claude config (default: `$HOME/.claude-code-docker`), only `.claude` and `.claude.json` files are used
- `-v/-vv/-vvv`: Verbosity levels (warning/info/debug/verbose-debug)

### Volume Mounts

- `{app_folder}` → `/app`
- `{home_folder}/.claude` → `/home/ubuntu/.claude`
- `{home_folder}/.claude.json` → `/home/ubuntu/.claude.json`

## Development

Use the `uv`, e.g.

```bash
git clone git@github.com:sumkincpp/claude-code-docker.git
cd ccd
uv run ccd --help
```

