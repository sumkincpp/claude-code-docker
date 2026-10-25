# Building with buildah

`buildah` builds the project `Dockerfile` without a Docker daemon. ccd uses it as a *build* backend and hands the image to a runtime
backend (today: `firecracker`). Docker is not needed on the host in that setup, but see [Known limits](#known-limits).

```toml
# ccd.toml
[build]
backend = "buildah"

[run]
backend = "firecracker"
```

or, for a single build, `ccd build --backend buildah` (the run backend stays as configured).

## 1. Install

```bash
sudo apt install buildah uidmap        # Debian / Raspberry Pi OS
buildah --version
```

`uidmap` provides `newuidmap`/`newgidmap`, which rootless buildah needs.

## 2. Rootless prerequisites

Your user needs ranges in `/etc/subuid` and `/etc/subgid`:

```bash
grep "$USER" /etc/subuid /etc/subgid
# pi:100000:65536  (one line per file)
```

If a file has no line for you: `sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$USER"`.

## 3. Storage driver: use `overlay`, not `vfs`

This is the setting that matters most for speed.

| driver | speed | notes |
|---|---|---|
| `overlay` | fast | native on kernel 5.11+ rootless; copies nothing per layer |
| `overlay` + `fuse-overlayfs` | medium | when native rootless overlay is refused |
| `vfs` | **extremely slow** | copies the whole filesystem for every layer |

With no `driver` configured, buildah picks the first driver whose test mount works and **falls back to `vfs` silently**.
A store keeps its driver once created. On a Raspberry Pi (SD card) the `vfs` fallback turned a build that should take minutes
into one that had not finished after 110 minutes (step 37 of 77).

Check which driver you have:

```bash
buildah info | grep -iE 'graphdriver|graphroot'
# "GraphDriverName": "overlay"      <- what you want
# "GraphRoot": "/home/<you>/.local/share/containers/storage"
```

### Switching from `vfs` to `overlay`

Changing the config is not enough; the existing `vfs` store must be removed. This deletes buildah's local images and layer cache
(not Docker's):

```bash
buildah rm --all; buildah rmi --all
buildah unshare rm -rf ~/.local/share/containers/storage   # unshare: vfs files are owned by mapped sub-uids
mkdir -p ~/.config/containers
printf '[storage]\ndriver = "overlay"\n' > ~/.config/containers/storage.conf
buildah info | grep -i GraphDriverName
```

If the config file already exists, add the `driver` line under `[storage]` instead of overwriting it.

### Why it fell back, and the `fuse-overlayfs` option

To see the real reason overlay was refused, force it once and read the error:

```bash
buildah --storage-driver overlay info 2>&1 | head
grep overlay /proc/filesystems
findmnt -T ~/.local/share/containers      # the store's filesystem must support overlay upperdirs (ext4 is fine; NFS/FUSE are not)
```

If native rootless overlay is refused, use `fuse-overlayfs`:

```bash
sudo apt install fuse-overlayfs
printf '[storage]\ndriver = "overlay"\n[storage.options.overlay]\nmount_program = "/usr/bin/fuse-overlayfs"\n' > ~/.config/containers/storage.conf
```

(Remove the old store as above whenever the driver changes.)

## 4. Build and run with ccd

```bash
ccd -v build --backend buildah      # -v prints the timings of each phase
ccd .                               # runs on the configured run backend
```

`ccd build` with buildah does three things:

1. `buildah bud --layers -t localhost/ccd …`: builds the Dockerfile (layer cache enabled).
2. `buildah bud … -f Dockerfile.rootfs`: adds the VM layer (`localhost/ccd-vm`, see below).
3. `buildah run --volume <cache>:/out … mksquashfs`: packs the VM image into `~/.cache/ccd/firecracker/rootfs.squashfs`.

Step 3 is skipped when the image id and the ccd VM files are unchanged since the last pack.

The first build has a cold cache (no layers, no npm/pnpm cache mounts) and downloads everything. Later builds reuse it.

## Running the image as a container: podman

`buildah` only builds. To run a buildah-built image as a container, use the `podman` backend; podman reads the same
containers-storage, so nothing is copied:

```bash
sudo apt install podman
```

```toml
[build]
backend = "buildah"

[run]
backend = "podman"
```

```bash
ccd build      # buildah builds localhost/ccd
ccd .          # podman run … localhost/ccd (same mounts, limits and -u root as the docker backend); ccd attach works too
```

`podman` can also build (`[build] backend = "podman"`, same layer cache as buildah) and then run in one backend. Podman as the
builder for Firecracker (`[run] backend = "firecracker"`) still calls the `buildah` command to pack the rootfs, so install
both. Run podman and buildah as the same user, or they will not see each other's images.

**Which user runs inside.** Rootless podman maps the image's `user` (uid 1000) to a sub-uid, so host files you own show up inside
the container as `root`-owned and `user` cannot write to them (for example `~/.claude`, or a mounted `~/.local/share/uv`). In
rootless podman, container root *is* your host user, so ccd runs the shell as root by default, with `HOME=/home/user` so the
mounted tool config is found. Files you create in `/app` belong to you on the host, and nothing is re-owned at start. The Docker
and Firecracker backends do not have this limit and keep using the image's `user`.

To use the image's `user` instead (tools that refuse to run as uid 0; mounts then need a fix such as `--userns=keep-id`, which can
make the first start slow while podman re-owns the image layers once):

```toml
[podman]
user = "image"
```

## 5. Where things are

| what | where |
|---|---|
| buildah images and layers | `~/.local/share/containers/storage` (root: `/var/lib/containers/storage`) |
| buildah config | `~/.config/containers/storage.conf` |
| Firecracker rootfs, kernel, VM state | `~/.cache/ccd/firecracker/` |

Buildah's images are invisible to `docker images`; list them with `buildah images`, or use ccd:

```bash
ccd images      # build data of the build and run backends
ccd status      # configured backends, the built image id, and whether the run backend's copy is current
ccd backends    # which backends are installed and ready
```

`localhost/ccd-vm` is a build helper: the `ccd` image plus the NFS client, Python and the VM init/agent, used only to pack the
rootfs. It is never started.

## Cleaning up

```bash
buildah images
buildah rmi localhost/ccd-vm localhost/ccd     # next ccd build recreates them
buildah rm --all                               # leftover working containers from interrupted builds
du -sh ~/.local/share/containers/storage
```

An interrupted build can leave an incomplete layer; buildah prints `Found incomplete layer … deleting it` and removes it.

## Troubleshooting

| symptom | likely cause | what to do |
|---|---|---|
| build takes hours | `vfs` storage driver | [switch to overlay](#switching-from-vfs-to-overlay) |
| errors about `newuidmap`, `/etc/subuid`, user namespaces | rootless prerequisites missing | section 2 |
| `Unknown instruction: "SET"` (or another shell word) at a step after a `RUN <<EOF` | old buildah without heredoc support parses the heredoc body as Dockerfile lines | the project Dockerfile no longer uses heredocs; avoid them in custom Dockerfiles or use a newer buildah |
| `buildah from ccd-vm` tries a registry | image name without `localhost/` | ccd already uses `localhost/ccd`; check custom commands |
| `ccd` says the rootfs is older than the image | image changed since the last pack | run `ccd build` |

## Known limits

- The buildah route was written from tool documentation and ccd's mocked tests; the only measured result on real hardware is the
  `vfs` slowness above. Report problems with the failing command's output.
- Not every BuildKit feature works on older packaged buildah versions. `RUN --mount=type=cache,…` worked on a Raspberry Pi OS
  buildah (the build got through all npm/pnpm steps), but heredocs (`RUN cat <<'EOF'`) did not, so the Dockerfile copies
  `docker/ccd-entrypoint` instead of writing it with a heredoc.
- A cold build on a Raspberry Pi with the `overlay` driver took about 50 minutes to reach step 56 of 77.
- `buildah` only builds. Running needs another backend: `[run] backend = "firecracker"`.
