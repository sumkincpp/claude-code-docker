#!/usr/bin/env bash
# Install Firecracker on the host. Usage: misc/install-firecracker.sh [VERSION]  (default: latest)
set -euo pipefail

if [ ! -e /dev/kvm ]; then
  echo "ERROR: /dev/kvm not found; Firecracker needs KVM (check 'dmesg | grep -i kvm', nested virtualization, EL2 boot on Pi)" >&2
  exit 1
fi
if [ ! -r /dev/kvm ] || [ ! -w /dev/kvm ]; then
  echo "ERROR: no access to /dev/kvm; run 'sudo usermod -aG kvm \$USER' and re-login" >&2
  exit 1
fi

REPO="firecracker-microvm/firecracker"
ARCH=$(uname -m)
[ "${ARCH}" = arm64 ] && ARCH=aarch64

VERSION="${1:-$(curl -fsSLI -o /dev/null -w '%{url_effective}' "https://github.com/${REPO}/releases/latest")}"
VERSION="${VERSION##*/}"
TARBALL="firecracker-${VERSION}-${ARCH}.tgz"

cd "$(mktemp -d)"
curl -fsSLO "https://github.com/${REPO}/releases/download/${VERSION}/${TARBALL}"
curl -fsSLO "https://github.com/${REPO}/releases/download/${VERSION}/${TARBALL}.sha256.txt"
sha256sum -c "${TARBALL}.sha256.txt"
tar xzf "${TARBALL}"

sudo install -m 0755 "release-${VERSION}-${ARCH}/firecracker-${VERSION}-${ARCH}" /usr/local/bin/firecracker
firecracker --version
