#!/usr/bin/env python3
import argparse
import logging
import subprocess
import sys
from pathlib import Path

IMAGE_NAME = "claude-code"

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


def build_image(image_name):
    """Build the Docker image"""
    logger.info(f"Building Docker image: {image_name}")

    build_command = f"docker build -t '{image_name}' - < Dockerfile"
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


def run_container(image_name, app_folder, home_folder):
    """Run the Docker container with specified volumes"""
    logger.info(f"Preparing to run container: {image_name}")

    # Convert to absolute paths
    app_path = Path(app_folder).resolve()
    home_path = Path(home_folder).resolve()

    logger.debug(f"App folder resolved to: {app_path}")
    logger.debug(f"Home folder resolved to: {home_path}")

    # Create necessary directories and files
    logger.debug("Creating necessary directories and files")

    logger.debug(f"Creating app directory: {app_path}")
    app_path.mkdir(parents=True, exist_ok=True)

    claude_dir = home_path / ".claude"
    logger.debug(f"Creating claude directory: {claude_dir}")
    claude_dir.mkdir(parents=True, exist_ok=True)

    claude_json = home_path / ".claude.json"
    logger.debug(f"Creating claude.json file: {claude_json}")
    claude_json.touch(exist_ok=True)

    # Build Docker run command
    cmd = [
        "docker",
        "run",
        "-it",
        "--rm",
        "-v",
        f"{app_path}:/app",
        "-v",
        f"{claude_dir}:/home/ubuntu/.claude",
        "-v",
        f"{claude_json}:/home/ubuntu/.claude.json",
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


def main():
    parser = argparse.ArgumentParser(description="Run claude-code Docker container")

    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity level (can be used multiple times: -v, -vv, -vvv)")

    command_parser = parser.add_subparsers(dest="command", required=True, help="Command to execute")
    command_parser.required = True

    command_parser.add_parser("build", help="Build the Docker image")
    run_parser = command_parser.add_parser("run", help="Run the Docker container")

    run_parser.add_argument("app_folder", nargs="?", default="./app", help="App folder path (default: ./app)")
    run_parser.add_argument("--home", default="./home", help="Home folder path (default: ./home)")

    args = parser.parse_args()

    # Setup logging based on verbosity
    setup_logging(args.verbose)

    logger.debug(f"Arguments parsed: {args}")
    logger.debug(f"Image name: {IMAGE_NAME}")

    if args.command == "build":
        logger.debug("Executing build command")
        build_image(IMAGE_NAME)
    elif args.command == "run":
        logger.debug("Executing run command")
        run_container(IMAGE_NAME, args.app_folder, args.home)


if __name__ == "__main__":
    main()
