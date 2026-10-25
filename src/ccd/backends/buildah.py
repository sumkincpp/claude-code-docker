import json
import logging
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime

from ccd.backends.common import to_docker_build_args
from ccd.plugin import AttachRequest, Backend, BackendError, BuildRequest, ImageInfo, RunRequest, parse_size, parse_time

logger = logging.getLogger(__name__)

NOT_A_RUNTIME = "The buildah backend only builds images; set [run] backend to a runtime (e.g. firecracker)"


class BuildahBackend(Backend):
    """Builds the Dockerfile without a Docker daemon; the image stays in buildah's containers-storage."""

    name = "buildah"
    can_run = False

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

    def built_image_id(self, request: BuildRequest) -> str:
        result = subprocess.run(["buildah", "images", "--noheading", "--format", "{{.ID}}", f"localhost/{request.image_name}"], capture_output=True, text=True, check=False)
        return result.stdout.strip()

    def images(self, options: dict) -> list[ImageInfo]:
        out = subprocess.run(["buildah", "images", "--json"], capture_output=True, text=True, check=False).stdout
        try:
            entries = json.loads(out)
        except ValueError:
            return []
        rows = []
        for entry in entries:
            size = entry.get("size", "")
            for name in entry.get("names") or []:
                if name.startswith(("localhost/ccd:", "localhost/ccd-vm:")):
                    rows.append(ImageInfo(self.name, "image", name, entry.get("id", ""), size if isinstance(size, int) else parse_size(str(size)), self._created(name)))
        return rows

    @staticmethod
    def _created(image: str) -> datetime | None:
        """Creation time from `buildah inspect`; the listing only has a relative '2 hours ago'."""
        out = subprocess.run(["buildah", "inspect", "--type", "image", image], capture_output=True, text=True, check=False).stdout
        try:
            data = json.loads(out)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        created = (data.get("OCIv1") or {}).get("created") or (data.get("Docker") or {}).get("created") or ""
        return parse_time(created)

    def run(self, request: RunRequest) -> None:
        raise BackendError(NOT_A_RUNTIME)

    def attach(self, request: AttachRequest) -> None:
        raise BackendError(NOT_A_RUNTIME)

    def list_running(self) -> list[str]:
        return []
