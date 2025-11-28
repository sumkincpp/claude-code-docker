#!/usr/bin/env python3
import argparse
import logging
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Literal

IMAGE_NAME = "claude-code"
IMAGE_HOME_FOLDER = "/home/ubuntu"
CONTAINER_NAME = "claude-code-container"

# Configure logging
logger = logging.getLogger(__name__)


def setup_logging(verbosity):
    """Configure logging based on verbosity level"""
    # Set up console handler
    console_handler = logging.StreamHandler(sys.stdout)

    if verbosity == 0:
        log_level = logging.WARNING
        log_format = "%(message)s"
    elif verbosity == 1:
        log_level = logging.INFO
        log_format = "%(levelname)s: %(message)s"
    elif verbosity == 2:
        log_level = logging.DEBUG
        log_format = "%(levelname)s: %(funcName)s: %(message)s"
    else:  # verbosity >= 3
        log_level = logging.DEBUG
        log_format = "%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s"

    # Configure formatter
    formatter = logging.Formatter(log_format)
    console_handler.setFormatter(formatter)

    # Configure logger
    logger.setLevel(log_level)
    logger.addHandler(console_handler)

    # Set subprocess logging for very verbose mode
    if verbosity >= 3:
        subprocess_logger = logging.getLogger("subprocess")
        subprocess_logger.setLevel(logging.DEBUG)
        subprocess_logger.addHandler(console_handler)

    logger.debug(f"Logging configured with verbosity level: {verbosity}")


def build_image(image_name, docker_args=None):
    """Build the Docker image"""
    logger.info(f"Building Docker image: {image_name}")

    build_command = f"docker build {docker_args} -t '{image_name}' - < Dockerfile"
    logger.debug(f"Build command: {build_command}")

    try:
        logger.debug("Starting Docker build process")
        # Stream output directly to console in real-time
        result = subprocess.run(build_command, shell=True, check=True)

        logger.info("Build completed successfully!")

    except subprocess.CalledProcessError as e:
        logger.error(f"Build failed with exit code {e.returncode}")
        sys.exit(1)
    except FileNotFoundError:
        logger.error("Docker not found. Please ensure Docker is installed and in PATH")
        sys.exit(1)


@dataclass
class PathSpec:
    path: Path
    volume_mapping: Path
    type: Literal["file", "folder"] = "folder"


class VolumeManager:
    def __init__(self, path_specs: List[PathSpec]):
        self.path_specs = path_specs

    def prepare_paths(self) -> None:
        """Create all directories and files defined in path_specs."""
        logger.debug("Creating necessary directories and files")

        # Create directories first
        for spec in self.path_specs:
            if spec.type == "folder":
                logger.debug(f"Creating directory: {spec.path}")
                spec.path.mkdir(parents=True, exist_ok=True)

        # Create files after directories
        for spec in self.path_specs:
            if spec.type == "file":
                logger.debug(f"Creating file: {spec.path}")
                spec.path.touch(exist_ok=True)

    def get_volume_commands(self) -> List[str]:
        """Generate volume mount commands for docker."""
        volume_cmds = []
        for spec in self.path_specs:
            if not spec.path.exists():
                raise FileNotFoundError(f"Source path does not exist: {spec.path}")
            volume_cmds.extend(["-v", f"{spec.path}:{spec.volume_mapping}"])
        return volume_cmds


def run_container(image_name, app_folder, home_folder):
    """Run the Docker container with specified volumes"""
    logger.info(f"Preparing to run container: {image_name}")

    # Convert to absolute paths
    app_path = Path(app_folder).resolve()
    home_path = Path(home_folder).resolve()

    logger.debug(f"App folder resolved to: {app_path}")
    logger.debug(f"Home folder resolved to: {home_path}")

    docker_home = Path(IMAGE_HOME_FOLDER)

    path_specs = [
        PathSpec(path=app_path, volume_mapping=Path("/app")),
        # Claude-Code config
        PathSpec(path=home_path / ".claude", volume_mapping=docker_home / ".claude"),
        PathSpec(path=home_path / ".claude.json", volume_mapping=docker_home / ".claude.json", type="file"),
        # Gemini config
        PathSpec(path=home_path / ".gemini", volume_mapping=docker_home / ".gemini"),
        # Codex config
        PathSpec(path=home_path / ".codex", volume_mapping=docker_home / ".codex"),
        # Copilot config
        PathSpec(path=home_path / ".copilot", volume_mapping=docker_home / ".copilot"),
    ]

    volume_manager = VolumeManager(path_specs)
    volume_manager.prepare_paths()
    volume_cmds = volume_manager.get_volume_commands()

    # Build Docker run command
    cmd = [
        "docker",
        "run",
        "-it",
        "--rm",
        "--name",
        CONTAINER_NAME,
        *volume_cmds,
        image_name,
    ]

    logger.info("Starting container with:")
    logger.info(f"  App folder: {app_path}")
    logger.info(f"  Home folder: {home_path}")
    logger.info(f"  Full command: {' '.join(cmd)}")

    try:
        logger.debug("Executing docker run command")
        subprocess.run(cmd, check=True)
        logger.info("Container execution completed successfully")

    except subprocess.CalledProcessError as e:
        logger.error(f"Container run failed with exit code {e.returncode}")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Container stopped by user")
        sys.exit(0)
    except FileNotFoundError:
        logger.error("Docker not found. Please ensure Docker is installed and in PATH")
        sys.exit(1)


def attach_container():
    """Attach to a running Docker container"""
    logger.info("Attaching to running Docker container: claude-code-container")

    cmd = ["docker", "exec", "-it", CONTAINER_NAME, "/bin/bash"]

    # ubuntu@7c3c7131cb15:/app$ exit
    # exit
    # Attach failed with exit code 130
    try:
        logger.debug("Executing docker exec command")
        subprocess.run(cmd)
    except subprocess.CalledProcessError as e:
        logger.error(f"Attach failed with exit code {e.returncode}")
        sys.exit(1)
    except FileNotFoundError:
        logger.error("Docker not found. Please ensure Docker is installed and in PATH")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Run claude-code Docker container")

    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity level (can be used multiple times: -v, -vv, -vvv)")

    command_parser = parser.add_subparsers(dest="command", required=True, help="Command to execute")
    command_parser.required = True

    build_parser = command_parser.add_parser("build", help="Build the Docker image")

    home_folder = Path.home() / ".claude-code-docker"

    dot_parser = command_parser.add_parser(".", help="Run the Docker container")
    dot_parser.add_argument("--home", default=home_folder, help=f"Home folder path (default: ${home_folder})")

    run_parser = command_parser.add_parser("run", help="Run the Docker container")
    run_parser.add_argument("app_folder", nargs="?", default="./app", help="App folder path (default: ./app)")
    run_parser.add_argument("--home", default=home_folder, help=f"Home folder path (default: ${home_folder})")

    attach_parser = command_parser.add_parser("attach", help="Attach to a running Docker container")

    args, unknown = parser.parse_known_args()

    # Setup logging based on verbosity
    setup_logging(args.verbose)

    logger.debug(f"Arguments parsed: {args}")
    logger.debug(f"Image name: {IMAGE_NAME}")

    if args.command == "build":
        logger.debug("Executing build command")
        docker_args = shlex.join(unknown) if unknown else ""
        build_image(IMAGE_NAME, docker_args)
    elif args.command == "run":
        logger.debug("Executing run command")
        run_container(IMAGE_NAME, args.app_folder, args.home)
    elif args.command == ".":
        logger.debug("Executing run command with default . folder")
        run_container(IMAGE_NAME, ".", args.home)
    elif args.command == "attach":
        logger.debug("Executing attach command")
        attach_container()


if __name__ == "__main__":
    main()
