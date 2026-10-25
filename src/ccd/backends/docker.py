import logging
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from ccd.config import FEATURE_BUILD_ARGS, VERSION_BUILD_ARGS, BuildConfig
from ccd.plugin import AttachRequest, Backend, BackendError, BuildRequest, ImageSource, RunRequest

logger = logging.getLogger(__name__)


def to_docker_build_args(config: BuildConfig, extra_args: list[str]) -> list[str]:
    """Produce the full list of docker-build flag tokens."""
    result: list[str] = [*extra_args]

    if config.force:
        logger.debug("Force flag set, disabling Docker layer caching")
        result.append("--no-cache")

    flags = config.feature_flags()
    if flags is not None:
        for feature, build_arg in sorted(FEATURE_BUILD_ARGS.items()):
            result.extend(["--build-arg", f"{build_arg}={'1' if flags[feature] else '0'}"])

    logger.debug("Setting NPM_CLI_MIN_RELEASE_AGE_DAYS=%s", config.npm_min_release_age_days)
    result.extend(["--build-arg", f"NPM_CLI_MIN_RELEASE_AGE_DAYS={config.npm_min_release_age_days}"])

    if config.npm_audit_ignore_components:
        logger.debug("Setting NPM_AUDIT_IGNORE_COMPONENTS=%s", config.npm_audit_ignore_components)
        result.extend(["--build-arg", f"NPM_AUDIT_IGNORE_COMPONENTS={','.join(sorted(config.npm_audit_ignore_components))}"])
    if config.npm_audit_force_fix_components:
        logger.debug("Setting NPM_AUDIT_FORCE_FIX_COMPONENTS=%s", config.npm_audit_force_fix_components)
        result.extend(["--build-arg", f"NPM_AUDIT_FORCE_FIX_COMPONENTS={','.join(sorted(config.npm_audit_force_fix_components))}"])
    if config.npm_min_release_age_ignore_components:
        logger.debug("Setting NPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS=%s", config.npm_min_release_age_ignore_components)
        result.extend(["--build-arg", f"NPM_CLI_MIN_RELEASE_AGE_IGNORE_COMPONENTS={','.join(sorted(config.npm_min_release_age_ignore_components))}"])

    for component, build_arg in sorted(VERSION_BUILD_ARGS.items()):
        if component in config.versions:
            logger.debug("Setting %s=%s", build_arg, config.versions[component])
            result.extend(["--build-arg", f"{build_arg}={config.versions[component]}"])

    return result


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

    def export_image(self, request: BuildRequest, dest: Path) -> None:
        self._docker(["docker", "save", "-o", str(dest), request.image_name], "Image export")

    def image_formats(self) -> set[str]:
        return {"docker-daemon"}

    def import_image(self, source: ImageSource, request: BuildRequest) -> None:
        self._docker(["docker", "load", "-i", str(source.archive())], "Image import")

    @staticmethod
    def _docker(cmd: list[str], what: str) -> None:
        logger.debug("Executing: %s", " ".join(cmd))
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            raise BackendError(f"{what} failed with exit code {e.returncode}") from e

    def list_running(self) -> list[str]:
        cmd = ["docker", "ps", "--format", "{{.Names}}"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            raise BackendError(f"Failed to get running containers with exit code {e.returncode}") from e
        return result.stdout.splitlines()
