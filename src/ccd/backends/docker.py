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

    def check_available(self) -> None:
        if shutil.which("docker") is None:
            raise BackendError("Docker not found. Please ensure Docker is installed and in PATH")

    def build(self, request: BuildRequest) -> None:
        """Build the Docker image"""
        logger.info("Building Docker image: %s", request.image_name)

        docker_args = shlex.join(to_docker_build_args(request.config, request.extra_args))
        build_command = f"docker build {docker_args} -f Dockerfile -t '{request.image_name}' ."
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
            "docker",
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

        if request.root:
            cmd.extend(["--user", "root"])

        cmd.extend([*volume_args(request), request.image_name])

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
        cmd = ["docker", "exec", "-it"]

        if request.root:
            cmd.extend(["--user", "root"])

        cmd.extend([request.name, "/bin/bash"])

        try:
            logger.debug("Executing docker exec command")
            subprocess.run(cmd)
        except subprocess.CalledProcessError as e:
            raise BackendError(f"Attach failed with exit code {e.returncode}") from e

    def image_formats(self) -> list[str]:
        return ["docker-daemon"]

    def built_image_id(self, request: BuildRequest) -> str:
        result = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", request.image_name], capture_output=True, text=True, check=False)
        return result.stdout.strip()

    def images(self, options: dict) -> list[ImageInfo]:
        # Unknown images make the exit code non-zero, but the ones that exist are still printed
        out = subprocess.run(["docker", "image", "inspect", "ccd", "ccd-vm"], capture_output=True, text=True, check=False).stdout
        try:
            entries = json.loads(out)
        except ValueError:
            return []
        return [
            ImageInfo(self.name, "image", (entry.get("RepoTags") or ["?"])[0], entry.get("Id", ""), entry.get("Size"), parse_time(entry.get("Created", "")))
            for entry in entries
        ]

    def list_running(self) -> list[str]:
        cmd = ["docker", "ps", "--format", "{{.Names}}"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            raise BackendError(f"Failed to get running containers with exit code {e.returncode}") from e
        return result.stdout.splitlines()
