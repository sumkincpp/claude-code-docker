import json
import logging
import shlex
import shutil
import subprocess
import sys

from ccd.backends.common import to_docker_build_args
from ccd.plugin import AttachRequest, Backend, BackendError, BuildRequest, ImageInfo, RunRequest, parse_time

logger = logging.getLogger(__name__)


def volume_args(request: RunRequest) -> list[str]:
    args: list[str] = []
    for mount in request.mounts:
        mount_str = f"{mount.host}:{mount.container}"
        if mount.readonly:
            mount_str += ":ro"
        args.extend(["-v", mount_str])
    return args


class DockerBackend(Backend):
    name = "docker"
    cli = "docker"  # the command line tool; the podman backend reuses everything with its own
    image_prefix = ""  # podman needs "localhost/" to address images that are not from a registry

    def image_ref(self, image_name: str) -> str:
        return f"{self.image_prefix}{image_name}"

    def user_args(self, root: bool, options: dict) -> list[str]:
        """Flags choosing the user for `run` and `exec`: the image's own user, or root when asked for."""
        return ["--user", "root"] if root else []

    def check_available(self) -> None:
        if shutil.which(self.cli) is None:
            raise BackendError("Docker not found. Please ensure Docker is installed and in PATH")

    def build(self, request: BuildRequest) -> None:
        """Build the Docker image"""
        logger.info("Building Docker image: %s", request.image_name)

        docker_args = shlex.join(to_docker_build_args(request.config, request.extra_args))
        build_command = f"{self.cli} build {docker_args} -f Dockerfile -t {shlex.quote(self.image_ref(request.image_name))} ."
        logger.debug("Build command: %s", build_command)

        try:
            logger.debug("Starting Docker build process")
            # Stream output directly to console in real-time
            _ = subprocess.run(build_command, shell=True, check=True)

            logger.info("Build completed successfully!")

        except subprocess.CalledProcessError as e:
            raise BackendError(f"Build failed with exit code {e.returncode}") from e

    def run(self, request: RunRequest) -> None:
        """Run the Docker container with specified volumes and resource limits"""
        cmd = [
            self.cli,
            "run",
            "-it",
            "--rm",
            "--memory",
            request.memory,
            "--cpus",
            request.cpus,
            "--hostname",
            request.name,
            "--name",
            request.name,
            # Allow bwrap (bubblewrap) to create user namespaces inside the container.
            # Docker's default seccomp profile blocks the clone syscall flags required
            # by bwrap even when kernel.unprivileged_userns_clone=1 is set on the host.
            "--security-opt",
            "seccomp=unconfined",
        ]

        cmd.extend(self.user_args(request.root, request.options))
        cmd.extend([*volume_args(request), self.image_ref(request.image_name)])

        logger.info("  Full command: %s", " ".join(cmd))

        try:
            logger.debug("Executing docker run command")
            subprocess.run(cmd, check=True)
            logger.info("Container execution completed successfully")

        except subprocess.CalledProcessError as e:
            raise BackendError(f"Container run failed with exit code {e.returncode}") from e
        except KeyboardInterrupt:
            logger.info("Container stopped by user")
            sys.exit(0)

    def attach(self, request: AttachRequest) -> None:
        """Attach to a running Docker container"""
        cmd = [self.cli, "exec", "-it"]

        cmd.extend(self.user_args(request.root, request.options))
        cmd.extend([request.name, "/bin/bash"])

        try:
            logger.debug("Executing docker exec command")
            subprocess.run(cmd)
        except subprocess.CalledProcessError as e:
            raise BackendError(f"Attach failed with exit code {e.returncode}") from e

    def image_formats(self) -> list[str]:
        return ["docker-daemon"]

    def built_image_id(self, request: BuildRequest) -> str:
        result = subprocess.run([self.cli, "image", "inspect", "--format", "{{.Id}}", self.image_ref(request.image_name)], capture_output=True, text=True, check=False)
        return result.stdout.strip()

    def images(self, options: dict) -> list[ImageInfo]:
        # Unknown images make the exit code non-zero, but the ones that exist are still printed
        out = subprocess.run([self.cli, "image", "inspect", self.image_ref("ccd"), self.image_ref("ccd-vm")], capture_output=True, text=True, check=False).stdout
        try:
            entries = json.loads(out)
        except ValueError:
            return []
        return [
            ImageInfo(self.name, "image", (entry.get("RepoTags") or ["?"])[0], entry.get("Id", ""), entry.get("Size"), parse_time(entry.get("Created", "")))
            for entry in entries
        ]

    def list_running(self) -> list[str]:
        cmd = [self.cli, "ps", "--format", "{{.Names}}"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            raise BackendError(f"Failed to get running containers with exit code {e.returncode}") from e
        return result.stdout.splitlines()
