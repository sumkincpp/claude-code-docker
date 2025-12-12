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
# ccdXX
CONTAINER_NAME_BASE = "ccd"

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


def get_container_base_name():
    """Get the base name for containers"""
    # {base}-{folder}-{number}

    return CONTAINER_NAME_BASE


def get_running_containers():
    """Get a list of currently running docker containers"""
    cmd = ["docker", "ps", "--format", "{{.Names}}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        running_containers = result.stdout.splitlines()
        # filter ones matching our pattern
        running_containers = [name for name in running_containers if name.startswith(CONTAINER_NAME_BASE + "-")]
        logger.debug(f"Currently running containers: {running_containers}")

        return list(sorted(running_containers))

    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to get running containers with exit code {e.returncode}")
        sys.exit(1)
    except FileNotFoundError:
        logger.error("Docker not found. Please ensure Docker is installed and in PATH")
        sys.exit(1)


def get_next_free_container_name(folder: str):
    """Check current running docker containers and when starting a new one, avoid name conflict by appending a number suffix"""
    running_containers = get_running_containers()

    suffix = 1
    while True:
        candidate_name = f"{CONTAINER_NAME_BASE}-{folder}-{suffix:02d}"
        if candidate_name not in running_containers:
            logger.debug(f"Selected container name: {candidate_name}")
            return candidate_name
        suffix += 1


def prompt_container_name():
    """Prompt user for a container name"""

    while True:
        running = get_running_containers()

        if not running:
            raise ValueError("No running containers found!")

        if len(running) == 1:
            return running[0]

        default_name = running[0]

        print("Running containers:")
        for index, name in enumerate(running):
            print(f"  [{index}] {name}")

        name = input(f"Enter container name, number or press Enter for default ({default_name}): ").strip()
        if name == "":
            return default_name
        elif name in running:
            return name
        elif name.isdigit():
            index = int(name)
            if 0 <= index < len(running):
                return running[index]
            else:
                print(f"Invalid number. Please enter a number between 0 and {len(running) - 1}.")
        else:
            print("Invalid input. Please enter a valid container name or number.")


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

    container_name = get_next_free_container_name(app_path.name)

    print(f"Starting container '{container_name}'...")

    # Build Docker run command
    cmd = [
        "docker",
        "run",
        "-it",
        "--rm",
        "--hostname",
        container_name,
        "--name",
        container_name,
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

    container_name = prompt_container_name()

    print(f"Attaching to container '{container_name}'...")

    cmd = ["docker", "exec", "-it", container_name, "/bin/bash"]

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

    try:
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
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
