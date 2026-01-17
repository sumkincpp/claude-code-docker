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
FEATURE_BUILD_ARGS = {
    "rust": "WITH_RUST",
    "claude": "WITH_CLAUDE",
    "codex": "WITH_CODEX",
    "gemini": "WITH_GEMINI",
    "opencode": "WITH_OPENCODE",
    "copilot": "WITH_COPILOT",
}
VERSION_BUILD_ARGS = {
    "nvm": "NVM_VERSION",
    "node": "NVM_NODE_VERSION",
    "npm": "NPM_VERSION",
    "uv": "UV_VERSION",
    "rustup": "RUSTUP_VERSION",
    "claude": "CLAUDE_VERSION",
    "codex": "CODEX_VERSION",
    "gemini": "GEMINI_VERSION",
    "opencode": "OPENCODE_VERSION",
    "copilot": "COPILOT_VERSION",
}

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

    logger.debug("Logging configured with verbosity level: %s", verbosity)


def build_image(image_name, docker_args=None):
    """Build the Docker image"""
    logger.info("Building Docker image: %s", image_name)

    build_command = f"docker build {docker_args} -t '{image_name}' - < Dockerfile"
    logger.debug("Build command: %s", build_command)

    try:
        logger.debug("Starting Docker build process")
        # Stream output directly to console in real-time
        result = subprocess.run(build_command, shell=True, check=True)

        logger.info("Build completed successfully!")

    except subprocess.CalledProcessError as e:
        logger.error("Build failed with exit code %s", e.returncode)
        sys.exit(1)
    except FileNotFoundError:
        logger.error("Docker not found. Please ensure Docker is installed and in PATH")
        sys.exit(1)


@dataclass
class PathSpec:
    path: Path
    volume_mapping: Path
    type: Literal["file", "folder"] = "folder"


@dataclass
class RunParameters:
    image_name: str
    app_folder: str
    home_folder: str
    memory: str = "1g"
    cpus: str = "2"
    root: bool = False

    @classmethod
    def from_args(cls, image_name: str, args, app_folder: str = None) -> "RunParameters":
        """Create RunParameters from command line arguments

        Args:
            image_name: Name of the Docker image to use
            args: Parsed command line arguments
            app_folder: Override for app_folder (defaults to args.app_folder or ".")
        """
        if app_folder is None:
            app_folder = args.app_folder if hasattr(args, 'app_folder') else "."

        return cls(
            image_name=image_name,
            app_folder=app_folder,
            home_folder=args.home,
            memory=args.memory,
            cpus=args.cpus,
            root=args.root,
        )


class VolumeManager:
    def __init__(self, path_specs: List[PathSpec]):
        self.path_specs = path_specs

    def prepare_paths(self) -> None:
        """Create all directories and files defined in path_specs."""
        logger.debug("Creating necessary directories and files")

        # Create directories first
        for spec in self.path_specs:
            if spec.type == "folder":
                logger.debug("Creating directory: %s", spec.path)
                spec.path.mkdir(parents=True, exist_ok=True)

        # Create files after directories
        for spec in self.path_specs:
            if spec.type == "file":
                logger.debug("Creating file: %s", spec.path)
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
        logger.debug("Currently running containers: %s", running_containers)

        return list(sorted(running_containers))

    except subprocess.CalledProcessError as e:
        logger.error("Failed to get running containers with exit code %s", e.returncode)
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
            logger.debug("Selected container name: %s", candidate_name)
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


def run_container(params: RunParameters):
    """Run the Docker container with specified volumes and resource limits"""
    logger.info("Preparing to run container: %s", params.image_name)

    # Convert to absolute paths
    app_path = Path(params.app_folder).resolve()
    home_path = Path(params.home_folder).resolve()

    logger.debug("App folder resolved to: %s", app_path)
    logger.debug("Home folder resolved to: %s", home_path)

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
        "--memory",
        params.memory,
        "--cpus",
        params.cpus,
        "--hostname",
        container_name,
        "--name",
        container_name,
    ]

    if params.root:
        cmd.extend(["--user", "root"])

    cmd.extend([*volume_cmds, params.image_name])

    logger.info("Starting container with:")
    logger.info("  App folder: %s", app_path)
    logger.info("  Home folder: %s", home_path)
    logger.info("  Memory limit: %s", params.memory)
    logger.info("  CPU limit: %s", params.cpus)
    logger.info("  User: %s", "root" if params.root else "ubuntu")
    logger.info("  Full command: %s", " ".join(cmd))

    try:
        logger.debug("Executing docker run command")
        subprocess.run(cmd, check=True)
        logger.info("Container execution completed successfully")

    except subprocess.CalledProcessError as e:
        logger.error("Container run failed with exit code %s", e.returncode)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Container stopped by user")
        sys.exit(0)
    except FileNotFoundError:
        logger.error("Docker not found. Please ensure Docker is installed and in PATH")
        sys.exit(1)


def attach_container(root=False):
    """Attach to a running Docker container"""
    logger.info("Attaching to running Docker container: claude-code-container")

    container_name = prompt_container_name()

    print(f"Attaching to container '{container_name}'...")

    cmd = ["docker", "exec", "-it"]

    if root:
        cmd.extend(["--user", "root"])

    cmd.extend([container_name, "/bin/bash"])

    # ubuntu@7c3c7131cb15:/app$ exit
    # exit
    # Attach failed with exit code 130
    try:
        logger.debug("Executing docker exec command")
        subprocess.run(cmd)
    except subprocess.CalledProcessError as e:
        logger.error("Attach failed with exit code %s", e.returncode)
        sys.exit(1)
    except FileNotFoundError:
        logger.error("Docker not found. Please ensure Docker is installed and in PATH")
        sys.exit(1)


def add_container_args(parser, home_folder):
    """Add common container arguments to a parser"""
    parser.add_argument("--home", default=home_folder, help=f"Home folder path (default: ${home_folder})")
    parser.add_argument("--memory", default="1g", help="Memory limit for the container (default: 1g)")
    parser.add_argument("--cpus", default="2", help="Number of CPUs for the container (default: 2)")
    parser.add_argument("--root", action="store_true", help="Run container as root user instead of default ubuntu user")
    return parser


def main():
    parser = argparse.ArgumentParser(description="Run claude-code Docker container")

    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity level (can be used multiple times: -v, -vv, -vvv)")

    command_parser = parser.add_subparsers(dest="command", required=True, help="Command to execute")
    command_parser.required = True

    build_parser = command_parser.add_parser("build", help="Build the Docker image")
    build_parser.add_argument(
        "--with",
        dest="with_features",
        help="Comma-separated feature list to include in the image (default: all features)",
    )
    build_parser.add_argument(
        "--without",
        dest="without_features",
        help="Comma-separated feature list to exclude from the image",
    )
    # Add version arguments
    for component in VERSION_BUILD_ARGS.keys():
        build_parser.add_argument(
            f"--{component}-version",
            dest=f"{component}_version",
            help=f"Version of {component} to install (default: latest or predefined)",
        )

    home_folder = Path.home() / ".claude-code-docker"

    dot_parser = command_parser.add_parser(".", help="Run the Docker container in current directory")
    add_container_args(dot_parser, home_folder)

    run_parser = command_parser.add_parser("run", help="Run the Docker container")
    run_parser.add_argument("app_folder", nargs="?", default="./app", help="App folder path (default: ./app)")
    add_container_args(run_parser, home_folder)

    attach_parser = command_parser.add_parser("attach", help="Attach to a running Docker container")
    attach_parser.add_argument("--root", action="store_true", help="Attach as root user instead of default ubuntu user")

    args, unknown = parser.parse_known_args()

    # Setup logging based on verbosity
    setup_logging(args.verbose)

    logger.debug("Arguments parsed: %s", args)
    logger.debug("Image name: %s", IMAGE_NAME)

    try:
        if args.command == "build":
            logger.debug("Executing build command")
            docker_arg_list = []
            if unknown:
                docker_arg_list.extend(unknown)
            if args.with_features and args.without_features:
                logger.error("Use only one of --with or --without")
                sys.exit(1)
            if args.with_features:
                requested = set()
                for item in args.with_features.split(","):
                    feature = item.strip()
                    if feature:
                        requested.add(feature)

                unknown_features = requested - set(FEATURE_BUILD_ARGS.keys())
                if unknown_features:
                    logger.error("Unknown feature(s): %s", ", ".join(sorted(unknown_features)))
                    logger.error("Available features: %s", ", ".join(sorted(FEATURE_BUILD_ARGS.keys())))
                    sys.exit(1)

                for feature, build_arg in sorted(FEATURE_BUILD_ARGS.items()):
                    value = "1" if feature in requested else "0"
                    docker_arg_list.extend(["--build-arg", f"{build_arg}={value}"])
            elif args.without_features:
                excluded = set()
                for item in args.without_features.split(","):
                    feature = item.strip()
                    if feature:
                        excluded.add(feature)

                unknown_features = excluded - set(FEATURE_BUILD_ARGS.keys())
                if unknown_features:
                    logger.error("Unknown feature(s): %s", ", ".join(sorted(unknown_features)))
                    logger.error("Available features: %s", ", ".join(sorted(FEATURE_BUILD_ARGS.keys())))
                    sys.exit(1)

                for feature, build_arg in sorted(FEATURE_BUILD_ARGS.items()):
                    value = "0" if feature in excluded else "1"
                    docker_arg_list.extend(["--build-arg", f"{build_arg}={value}"])

            # Process version arguments
            for component, build_arg in sorted(VERSION_BUILD_ARGS.items()):
                version_attr = f"{component}_version"
                if hasattr(args, version_attr):
                    version = getattr(args, version_attr)
                    if version:
                        logger.debug("Setting %s=%s", build_arg, version)
                        docker_arg_list.extend(["--build-arg", f"{build_arg}={version}"])

            docker_args = shlex.join(docker_arg_list) if docker_arg_list else ""
            build_image(IMAGE_NAME, docker_args)
        elif args.command == "run":
            logger.debug("Executing run command")
            params = RunParameters.from_args(IMAGE_NAME, args)
            run_container(params)
        elif args.command == ".":
            logger.debug("Executing run command with default . folder")
            params = RunParameters.from_args(IMAGE_NAME, args, app_folder=".")
            run_container(params)
        elif args.command == "attach":
            logger.debug("Executing attach command")
            attach_container(args.root)
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
