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
| paths | What the install leaves in the image (absolute or `~/` paths, files or folders). A feature with paths gets a build stage of its own, see How features become a Dockerfile. |
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

## Paths and stages

A feature that lists `paths` is built in a stage of its own and only those paths are copied into the final image. Stages that do not depend on each other are built at the same time (BuildKit does that by itself; buildah needs `--jobs`, ccd passes 4, see `[buildah] jobs` in ccd.toml), and a feature that did not change keeps its cached install when another one does.

```toml
paths = ["~/.local/go"]
```

Rules:
- List exactly what the install writes. A path that is missing from the list is missing from the image, and the build's final check (every feature's verify command) fails. A listed path that does not exist stops the build at the copy.
- Be precise in shared folders: `~/.local/bin/uv`, not `~/.local/bin`, when other features write there too. Two features may list the same folder, then both are copied and the folders merge.
- List a folder, not a symlink, when you want links kept: a link named as the source is copied as the file it points to, a link inside a copied folder stays a link.
- A feature that changes files of what it requires lists them too (pnpm updates npm inside `~/.nvm`).
- A feature that installs as root cannot have paths.
- A feature without paths needs none: it installs in `pre`, first and in order, unless it requires a feature that has paths (every bundled one has). Then it installs in `post`, after the image is assembled, on top of it. It is not built in parallel, but it works as it always did.
- A feature with paths cannot require one that installs in `post`, because it is built before the image is assembled. ccd says so and names both: give the second one paths.

`ccd build --print-plan` shows where each selected feature goes (its stage and what it is built on) without building anything:

```
FEATURE  STAGE   BUILT ON
nvm      f-nvm   pre
node     f-node  f-nvm
pnpm     f-pnpm  f-node
mytool   post    final
```
- Files keep their owner when copied. ccd copies with `--chown=user:user` so the folders it has to create are the user's too.

## How features become a Dockerfile

How the selection is laid out is up to a renderer, chosen with `ccd build --mode` or `mode` under `[build]`. This section is the `parallel` renderer, the default. `ccd build --mode sequential` (or `mode = "sequential"` under `[build]` in ccd.toml) ignores the paths and installs every feature in order in the single `pre` stage, with the same final checks: slower, but nothing is copied, which can win on slow storage or with a builder that cannot run stages at the same time.

1. Select: the default features, or exactly the ones you ask for, plus whatever they require. Excluding something a selected feature needs is an error that names both.
2. Order: requirements first, then by `order`, then by name, so the output is the same on every run.
3. Render:
   - `base`: the base image part.
   - `pre`: the features without paths, one block each in order.
   - `f-<name>`: one stage per feature with paths. With one requirement it starts from that requirement's stage (nvm, node, pnpm and the CLIs form a chain), otherwise from `pre` with the paths of everything it needs copied in.
   - `final`: starts from `pre` and copies every feature's paths over from its stage.
   - `post`: only if a feature without paths needs one that has them. Starts from `final` and installs those features in order.
   - Then, in the last of these stages: one step that runs every verify command, the record of what is installed (`/metadata/installed.jsonl`), and the entrypoint part.

Each block declares its build args right before its RUN and the RUN references each of them. A stage does not inherit build args, so each stage declares the ones it uses. Docker then reuses a layer as long as its text, its args and its assets are unchanged and everything before it in the same stage is too, so changing one version rebuilds that feature's stage and the final stage.

Copying a big tree is not free (a few hundred MB of small files take minutes on slow storage), and a changed feature is copied again together with every copy after it. That is why `order` matters: give large trees that rarely change (node, rust, go) a lower number than the tools you update often.

## Renderers

The manifests, the selection and the order (`ccd.features`) know nothing about Dockerfiles. A renderer turns the selected features into build input, and `ccd build --mode` picks one by name:

| Mode | Renderer | What it does |
| --- | --- | --- |
| parallel | `ParallelRenderer` | The default, see Paths and stages. |
| sequential | `SequentialRenderer` | Every feature in order in one stage. |

Both are small subclasses of `DockerfileRenderer` (`ccd/features/render/dockerfile.py`), which writes the head, `pre`, the stages, `final` and the tail, and only ask the subclass for a `Plan`: which features install in `pre`, which further stages there are, and what `final` copies. The instructions that install one feature are shared pure functions (`ccd/features/render/dialect.py`).

### One layer

`ccd build --flatten` (or `flatten = true` under `[build]`) appends a last stage to the Dockerfile of either layout:

```dockerfile
FROM scratch AS flat
COPY --from=final / /
ENV ... WORKDIR ... USER ... ENTRYPOINT ... CMD ...   # the image config, replayed
RUN <every feature's verify command>
```

The image then has one layer and nothing of the stages. A copy keeps files but not the image config, so ccd replays it from the same sources the other stages use (the head, the features' `env`, the tail), and runs the checks once more to show the replay is right. The base image's `PATH` is written out in `BASE_PATH` (`ccd/features/render/dockerfile.py`) because a stage from scratch has none; change it with the base image.

It costs a second full copy of the whole filesystem and the stage is rebuilt after any change, which is why it is off by default. It only helps when the layers themselves matter, for example when the image is pushed to a registry; the Firecracker backend flattens the image into a rootfs anyway.

Another renderer, which does not have to produce a Dockerfile, is a subclass of `Renderer` (`render` and `stage_context`; it receives `RenderOptions`, for example `flatten`) published in the `ccd.renderers` entry-point group of a package:

```toml
[project.entry-points."ccd.renderers"]
mine = "mypackage.render:MyRenderer"
```

Then `ccd build --mode mine` uses it. Renderers do not know about backends.

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
ccd features                           # list features, what they require, their versions and where they come from
```

The same selection works in `ccd.toml` with `with_features` or `without_features` under `[build]`. Excluding a feature that a selected one requires stops the build and names both.
