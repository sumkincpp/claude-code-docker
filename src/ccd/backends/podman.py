import logging
import shutil
import subprocess

from ccd.backends.docker import DockerBackend
from ccd.config import IMAGE_HOME_FOLDER
from ccd.plugin import BackendError, BuildRequest

logger = logging.getLogger(__name__)


class PodmanBackend(DockerBackend):
    """Builds and runs containers with podman. Podman shares containers-storage with buildah, so an image built by the
    buildah backend runs here without any copy."""

    name = "podman"
    cli = "podman"
    image_prefix = "localhost/"

    def check_available(self) -> None:
        if shutil.which(self.cli) is None:
            raise BackendError("podman not found in PATH")

    def user_args(self, root: bool, options: dict) -> list[str]:
        # Rootless podman maps the image's `user` (uid 1000) to a sub-uid, so your bind-mounted host files look root-owned and
        # are not writable for it. Container root is your own host user, so by default the shell is root with the image user's
        # home (the mounted config lives there). [podman] user = "image" keeps the image's user instead.
        if root or options.get("user", "root") == "root":
            logger.info("  Container user: root (your host user in rootless podman), HOME=%s", IMAGE_HOME_FOLDER)
            return ["--user", "root", "-e", f"HOME={IMAGE_HOME_FOLDER}"]
        return []

    def image_formats(self) -> list[str]:
        return ["containers-storage"]

    def accepted_formats(self) -> list[str]:
        return ["containers-storage"]

    def import_image(self, fmt: str, location: str, request: BuildRequest) -> None:
        # Same store as the builder: nothing to copy, but fail early when the image is not there
        if subprocess.run([self.cli, "image", "exists", str(location)], check=False).returncode != 0:
            raise BackendError(f"Image '{location}' is not in podman's storage (is buildah/podman using a different user or storage root?)")
