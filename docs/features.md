# Features guide

A feature is one installable part of the ccd image: an agent such as Claude Code or Codex, a runtime such as Node.js, a tool such as uv. Everything that is not the bare base image (Alpine, the user, the entrypoint) is a feature.

A feature is described by a manifest, not by Dockerfile text. The manifest says what the feature is, what it needs, the command that installs it and how to check it. ccd reads the manifests, works out which features are selected and in what order, and a renderer turns that into a Dockerfile. The Dockerfile is only the first renderer, the manifests do not depend on it.

## Layout

```
src/ccd/features/bundled/claude/
  feature.toml
  assets/            optional: files the install command needs
```

Bundled features live in `src/ccd/features/bundled`. You can add your own, or replace a bundled one by using the same name, in:

1. `~/.config/ccd/features/<name>/` for all your projects
2. `.ccd/features/<name>/` for one project

Later locations win over earlier ones. A manifest is shell, so it is trusted code like a Dockerfile: only put features in places you control.

## A manifest

```toml
description = "Claude Code CLI"
requires = ["pnpm"]

[env]
DISABLE_AUTOUPDATER = "1"

[install]
command = "ccd-npm-install claude @anthropic-ai/claude-code --binary claude --version \"$CLAUDE_VERSION\" --built-dependency"

[verify]
command = "claude --version"
```

Keys, all optional except the install command:

| Key | Meaning |
| --- | --- |
| name | Defaults to the directory name. Lower case letters, digits and dashes. |
| description | One line, shown in the generated Dockerfile. |
| default | Installed unless excluded. Default true, set false for opt-in features such as jules. |
| order | Tie-break among features that are ready at the same time, lower first. Default 50. |
| requires | Other features (or capabilities, see provides) that must be installed first. Resolved transitively. |
| provides | Extra capability names this feature satisfies. Its own name is always provided. |
| version, versions | See Versions. |
| install.command | A shell command or a list of commands, chained with &&. Each must be a single line. |
| install.user | user (default) or root, for example to run apk add. |
| verify.command | A command or a list of them. All selected features' checks run in one step at the end of the build. |
| env | Environment variables set in the image. Values may use build args, for example `${NODE_VERSION}`. |
| args | Build args with their defaults, see Build args. |
| cache | Build cache mounts, see Caches. |
| mounts | Host folders the feature keeps config in, see Mounts. |

Unknown keys are an error, so typos do not pass silently.

## Install commands

The install is just a command. Use the real CLI directly, for example `npm install -g @openai/codex@$CODEX_VERSION`, or a helper. Bundled features install npm packages with `ccd-npm-install`, shipped by the pnpm feature, which applies the release-age cooldown, the audit and the build script rules of ccd.

Commands run with:
- the feature's versions and args as environment variables (`$CLAUDE_VERSION`)
- `CCD_FEATURE_DIR` pointing at the feature's assets, when it has any
- the cache mounts and args of everything it requires

## Versions

Every feature X automatically has:
- an environment variable `X_VERSION` (dashes become underscores, `local-claude` gives `LOCAL_CLAUDE_VERSION`)
- the flag `ccd build --x-version` and the setting `x` under `[versions]` in `ccd.toml`
- the default `latest`

So a manifest only says something when it differs:

```toml
version = "24.15.0"      # a different default (node)
version = false          # no version at all (local-claude)

[versions]               # extra versions on top of the main one
npm = "latest"           # $NPM_VERSION and --npm-version, declared by pnpm
```

Version names are unique across all features, which is checked when loading.

## Build args

Build args are settings shared with other features, such as the npm release-age policy declared by pnpm:

```toml
[args]
NPM_CLI_MIN_RELEASE_AGE_DAYS = "7"
```

A feature that requires the declaring feature, directly or not, sees these as environment variables too. Changing one rebuilds the declaring feature and everything after it.

## Caches

A feature offers build caches with `[[cache]]`:

```toml
[[cache]]
id = "pnpm-store"
target = "~/.pnpm-store"
```

The install step of every feature that requires it gets the mount automatically, so an agent does not repeat it. A feature can also declare a cache only for itself. `~` means the image user's home.

## Assets

Files in `assets/` are copied into the image at `/usr/local/share/ccd/features/<name>/` before the install runs, and `$CCD_FEATURE_DIR` points there. The install command decides where they end up, for example copying a script into `~/.local/bin`. Changing an asset rebuilds that feature's layer.

## Mounts

A feature that keeps config on the host, such as an agent, declares it with `[[mounts]]`, in the same form as `[[run.mounts]]` in ccd.toml:

```toml
[[mounts]]
host = "{home}/.claude"
container = "~/.claude"

[[mounts]]
host = "{home}/.claude.json"
container = "~/.claude.json"
type = "file"
```

`{home}` is the `--home` folder on the host and a leading `~/` in `container` is the image user's home. `type` is `folder` (default) or `file`, and `optional` and `readonly` work as in ccd.toml. These folders are created under `--home` on first run.

At run time the mounts of every feature that declares some are used. `ccd.toml` can narrow that and change it:

```toml
[run]
features = ["claude", "codex"]     # only these

[features.claude]                  # replace this feature's mounts
mounts = [{ host = "~/.claude", container = "~/.claude" }]
```

Mounts follow the manifests, not what happens to be installed in the image, so build with the same selection you run with. A mount whose container path equals an earlier one replaces it, so a `[[run.mounts]]` entry can take over a feature's mount. `ccd -v` lists the features and every mount when a container starts.

## How features become a Dockerfile

1. Select: the default features, or exactly the ones you ask for, plus whatever they require. Excluding something a selected feature needs is an error that names both.
2. Order: requirements first, then by `order`, then by name, so the output is the same on every run.
3. Render: the base image part, then one block per feature, then one step that runs every verify command, then the entrypoint part.

Each block declares its build args right before its RUN and the RUN references each of them. Docker then reuses a layer as long as its text, its args and its assets are unchanged and everything before it is too, so changing one version rebuilds only that feature and the ones after it.

## Writing a feature

`.ccd/features/hello/feature.toml`:

```toml
description = "Say hello at build time"
version = false

[install]
command = "echo hello"
```

Then `ccd build --with hello --print-dockerfile` shows the generated Dockerfile with your feature in it, and `ccd build --with hello` builds it. A broken manifest stops ccd with an error that names the file.

## Using features

```bash
ccd build                              # every default feature
ccd build --with claude,uv             # only these, plus what they require (nvm, node and pnpm for claude)
ccd build --without rust,gemini        # all defaults except these
ccd build --claude-version 2.0.1       # a version, see Versions
ccd build --print-dockerfile           # show the Dockerfile, build nothing
```

The same selection works in `ccd.toml` with `with_features` or `without_features` under `[build]`. Excluding a feature that a selected one requires stops the build and names both.
