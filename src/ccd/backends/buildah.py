import logging
import shutil
import subprocess
from dataclasses import replace

from ccd.backends.common import to_docker_build_args
from ccd.plugin import AttachRequest, Backend, BackendError, BuildRequest, RunRequest

logger = logging.getLogger(__name__)

NOT_A_RUNTIME = "The buildah backend only builds images; set [run] backend to a runtime (e.g. firecracker)"


class BuildahBackend(Backend):
    """Builds the Dockerfile without a Docker daemon; the image stays in buildah's containers-storage."""

    name = "buildah"

    def check_available(self) -> None:
        if shutil.which("buildah") is None:
            raise BackendError("buildah not found in PATH")

    def build(self, request: BuildRequest) -> None:
        # `localhost/` keeps later `buildah from` lookups from trying a registry
        cmd = ["buildah", "bud", "--layers", "-t", f"localhost/{request.image_name}", "-f", "Dockerfile", *to_docker_build_args(request.config, request.extra_args), "."]
        logger.debug("Build command: %s", cmd)
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            raise BackendError(f"Build failed with exit code {e.returncode}") from e

    def image_formats(self) -> list[str]:
        return ["containers-storage"]

    def provide_image(self, fmt: str, request: BuildRequest):
        return super().provide_image(fmt, replace(request, image_name=f"localhost/{request.image_name}"))

    def run(self, request: RunRequest) -> None:
        raise BackendError(NOT_A_RUNTIME)

    def attach(self, request: AttachRequest) -> None:
        raise BackendError(NOT_A_RUNTIME)

    def list_running(self) -> list[str]:
        return []
