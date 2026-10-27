import os
import shutil
import subprocess

from ccd.backends.docker import DockerBackend
from ccd.backends.podman import PodmanBackend
from ccd.plugin import BackendError, BuildRequest

INSTALL_HINT = "run misc/install-gvisor.sh"
PAGE_SIZE = 4096  # what the released runsc is compiled for; it refuses to boot on a host with another page size


def check_page_size() -> None:
    page_size = os.sysconf("SC_PAGE_SIZE")
    if page_size != PAGE_SIZE:
        raise BackendError(f"gVisor needs a host with {PAGE_SIZE}-byte pages, this one has {page_size} (on a Raspberry Pi 5, boot kernel8.img)")


class GVisorDockerBackend(DockerBackend):
    """Runs containers from the docker daemon's images in gVisor's user-space kernel (runsc), which must be registered with
    docker (misc/install-gvisor.sh does that)."""

    name = "gvisor-docker"
    can_build = False
    runtime_args = ["--runtime", "runsc"]

    def check_available(self) -> None:
        super().check_available()
        check_page_size()
        info = subprocess.run([self.cli, "info", "--format", "{{json .Runtimes}}"], capture_output=True, text=True, check=False)
        if "runsc" not in info.stdout:
            raise BackendError(f"Docker has no 'runsc' runtime registered ({INSTALL_HINT}, or 'sudo runsc install' and restart docker)")

    def accepted_formats(self) -> list[str]:
        return ["docker-daemon"]

    def import_image(self, fmt: str, location: str, request: BuildRequest) -> None:
        """Same daemon as the docker builder: nothing to copy."""


class GVisorPodmanBackend(PodmanBackend):
    """Like gvisor-docker, but with podman and its containers-storage."""

    name = "gvisor-podman"
    can_build = False
    runtime_args = ["--runtime", "runsc"]

    def check_available(self) -> None:
        super().check_available()
        check_page_size()
        if shutil.which("runsc") is None:
            raise BackendError(f"runsc not found in PATH ({INSTALL_HINT})")
