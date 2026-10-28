import json
import logging
import shlex
import shutil
import subprocess
import sys

from ccd.backends.common import build_inputs, to_docker_build_args
from ccd.config import DEFAULT_TAG, image_ref
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
    runtime_args: list[str] = []  # extra `run` flags selecting the OCI runtime, e.g. gVisor's --runtime runsc

    def image_ref(self, image_name: str, tag: str = DEFAULT_TAG) -> str:
        return f"{self.image_prefix}{image_ref(image_name, tag)}"

    def user_args(self, root: bool, options: dict) -> list[str]:
        """Flags choosing the user for `run` and `exec`: the image's own user, or root when asked for."""
        return ["--user", "root"] if root else []

    def check_available(self) -> None:
        if shutil.which(self.cli) is None:
            raise BackendError("Docker not found. Please ensure Docker is installed and in PATH")

    def build(self, request: BuildRequest) -> None:
        """Build the Docker image"""
        logger.info("Building Docker image: %s", request.image_name)

        dockerfile, context = build_inputs(request)
        docker_args = shlex.join(to_docker_build_args(request.config, request.extra_args))
        build_command = f"{self.cli} build {docker_args} -f {shlex.quote(str(dockerfile))} -t {shlex.quote(self.image_ref(request.image_name, request.tag))} {shlex.quote(str(context))}"
        logger.debug("Build command: %s", build_command)

        try:
            logger.debug("Starting Docker build process")
            # Stream output directly to console in real-time
            _ = subprocess.run(build_command, shell=True, check=True)

            logger.info("Build completed successfully!")

        except subprocess.CalledProcessError as e:
            raise BackendError(f"Build failed with exit code {e.returncode}") from e

    @staticmethod
    def terminal_flags(command: list[str]) -> list[str]:
        """An interactive shell always gets a terminal. A one-off command only does when ccd itself is attached to one, so
        piping its output or input (and running it from scripts) works."""
        if not command:
            return ["-it"]
        return ["-i", "-t"] if sys.stdin.isatty() and sys.stdout.isatty() else ["-i"]

    def run(self, request: RunRequest) -> int:
        """Run the Docker container with specified volumes and resource limits; returns its exit code"""
        cmd = [
            self.cli,
            "run",
            *self.terminal_flags(request.command),
            "--rm",
            *self.runtime_args,
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
        cmd.extend([*volume_args(request), self.image_ref(request.image_name, request.tag), *request.command])

        logger.info("  Full command: %s", " ".join(cmd))

        logger.debug("Executing %s run command", self.cli)
        try:
            return subprocess.run(cmd, check=False).returncode
        except KeyboardInterrupt:
            logger.info("Container stopped by user")
            return 130

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
        result = subprocess.run([self.cli, "image", "inspect", "--format", "{{.Id}}", self.image_ref(request.image_name, request.tag)], capture_output=True, text=True, check=False)
        return result.stdout.strip()

    def images(self, options: dict) -> list[ImageInfo]:
        # Every tag of the image and of its VM layer; names come from the listing, then one inspect gives the details
        listing = subprocess.run(
            [self.cli, "image", "ls", "--filter", f"reference={self.image_ref('ccd')}", "--filter", f"reference={self.image_ref('ccd-vm')}", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        names = sorted({line.strip() for line in listing.splitlines() if line.strip() and "<none>" not in line})
        if not names:
            return []
        # Unknown images make the exit code non-zero, but the ones that exist are still printed
        out = subprocess.run([self.cli, "image", "inspect", *names], capture_output=True, text=True, check=False).stdout
        try:
            entries = json.loads(out)
        except ValueError:
            return []
        return [
            ImageInfo(self.name, "image", (entry.get("RepoTags") or ["?"])[0], entry.get("Id", ""), entry.get("Size"), parse_time(entry.get("Created", "")))
            for entry in entries
        ]

    def tag_image(self, name: str, source: str, target: str, options: dict) -> None:
        try:
            subprocess.run([self.cli, "tag", self.image_ref(name, source), self.image_ref(name, target)], capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            raise BackendError(f"Tagging failed: {(e.stderr or '').strip() or f'exit code {e.returncode}'}") from e

    def list_running(self) -> list[str]:
        cmd = [self.cli, "ps", "--format", "{{.Names}}"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            raise BackendError(f"Failed to get running containers with exit code {e.returncode}") from e
        return result.stdout.splitlines()
