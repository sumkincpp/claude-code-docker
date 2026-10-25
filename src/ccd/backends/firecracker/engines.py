"""How an image becomes the VM rootfs, per image format the Firecracker backend accepts.

For each engine: look up the image id (for the up-to-date stamp), add the VM layer (Dockerfile.rootfs) on top of the image,
and run the pack script inside that image with the cache dir mounted at /out, so mksquashfs writes straight into the cache.
"""

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Engine:
    image_id: Callable[[str], str]  # "" when the image does not exist
    add_vm_layer: Callable[[str, str, Path, Path, bool], None]  # base image, VM image, Dockerfile.rootfs, build context, force
    pack: Callable[[str, Path, str], None]  # VM image, cache dir, shell script
    chown: Callable[[str], str]  # shell command handing the output file to the invoking user


def _docker_image_id(image: str) -> str:
    result = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image], capture_output=True, text=True, check=False)
    return result.stdout.strip()


def _docker_add_vm_layer(image: str, vm_image: str, dockerfile: Path, context: Path, force: bool) -> None:
    cmd = ["docker", "build", "-t", vm_image, "--build-arg", f"BASE_IMAGE={image}", "-f", str(dockerfile), *(["--no-cache"] if force else []), str(context)]
    subprocess.run(cmd, check=True)


def _docker_pack(vm_image: str, cache: Path, script: str) -> None:
    cmd = ["docker", "run", "--rm", "--user", "root", "--network", "none", "--entrypoint", "sh", "-v", f"{cache}:/out", vm_image, "-c", script]
    subprocess.run(cmd, check=True)


def _buildah_image_id(image: str) -> str:
    result = subprocess.run(["buildah", "images", "--noheading", "--format", "{{.ID}}", image], capture_output=True, text=True, check=False)
    return result.stdout.strip()


def _buildah_add_vm_layer(image: str, vm_image: str, dockerfile: Path, context: Path, force: bool) -> None:
    cmd = ["buildah", "bud", "--layers", "-t", vm_image, "--build-arg", f"BASE_IMAGE={image}", "-f", str(dockerfile), *(["--no-cache"] if force else []), str(context)]
    subprocess.run(cmd, check=True)


def _buildah_pack(vm_image: str, cache: Path, script: str) -> None:
    container = subprocess.run(["buildah", "from", vm_image], capture_output=True, text=True, check=True).stdout.strip()
    try:
        cmd = ["buildah", "run", "--network", "none", "--volume", f"{cache}:/out", container, "--", "sh", "-c", script]
        subprocess.run(cmd, check=True)
    finally:
        subprocess.run(["buildah", "rm", container], capture_output=True, check=False)


ENGINES = {
    "docker-daemon": Engine(_docker_image_id, _docker_add_vm_layer, _docker_pack, lambda path: f"chown {os.getuid()}:{os.getgid()} {path}"),
    # Rootless buildah maps the container's root to the invoking user, so the file is already theirs (and their uid is not mapped inside)
    "containers-storage": Engine(_buildah_image_id, _buildah_add_vm_layer, _buildah_pack, lambda path: "true"),
}
