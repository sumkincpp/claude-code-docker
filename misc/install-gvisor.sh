#!/usr/bin/env bash
# Install gVisor (runsc) on the host and register it with docker. Usage: misc/install-gvisor.sh [RELEASE]  (default: latest)
# For podman no registration is needed: runsc only has to be on PATH.
set -euo pipefail

ARCH=$(uname -m)
[ "${ARCH}" = arm64 ] && ARCH=aarch64

RELEASE="${1:-latest}"
URL="https://storage.googleapis.com/gvisor/releases/release/${RELEASE}/${ARCH}"
TARBALL=gvisor.tar.bz2

cd "$(mktemp -d)"
curl -fsSLO "${URL}/${TARBALL}"
curl -fsSLO "${URL}/${TARBALL}.sha512"
sha512sum -c "${TARBALL}.sha512"
tar xjf "${TARBALL}"

# gvisor-bin holds helper binaries that runsc looks up next to itself
sudo install -m 0755 runsc containerd-shim-runsc-v1 /usr/local/bin/
sudo rm -rf /usr/local/bin/gvisor-bin
sudo cp -r gvisor-bin /usr/local/bin/gvisor-bin
runsc --version

if command -v docker >/dev/null; then
  sudo runsc install
  sudo systemctl reload docker 2>/dev/null || sudo systemctl restart docker
fi
