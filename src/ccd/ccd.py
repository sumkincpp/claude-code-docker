#!/usr/bin/env python3
import argparse
import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import List

from ccd.config import (
    BUILD_MODES,
    CONFIG_FILE_NAME,
    CONTAINER_NAME_BASE,
    DEFAULT_BACKEND,
    DEFAULT_BUILD_MODE,
    DEFAULT_TAG,
    IMAGE_NAME,
    BuildConfig,
    PathSpec,
    RunConfig,
    RunParameters,
    image_ref,
    validate_tag,
)
from ccd.features import Feature, FeatureError, known_versions, load_features, select
from ccd.features.render import INSTALLED_FILE, render_dockerfile, stage_context
from ccd.plugin import (
    AttachRequest,
    Backend,
    BackendError,
    BuildRequest,
    ResolvedMount,
    RunRequest,
    available_backends,
    format_age,
    get_backend,
    hand_over,
    human_size,
    short_id,
    timed,
)

# Configure logging
logger = logging.getLogger(__name__)


def setup_logging(verbosity: int) -> None:
    """Configure logging based on verbosity level"""
    # Set up console handler
    console_handler = logging.StreamHandler(sys.stderr)  # logs never mix into a one-off command's stdout

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

    # Configure the package logger; ccd.* module loggers (including backends) propagate to it
    package_logger = logging.getLogger("ccd")
    package_logger.setLevel(log_level)
    package_logger.addHandler(console_handler)

    # Set subprocess logging for very verbose mode
    if verbosity >= 3:
        subprocess_logger = logging.getLogger("subprocess")
        subprocess_logger.setLevel(logging.DEBUG)
        subprocess_logger.addHandler(console_handler)

    logger.debug("Logging configured with verbosity level: %s", verbosity)


class VolumeManager:
    def __init__(self, path_specs: List[PathSpec]):
        self.path_specs = path_specs

    def prepare_paths(self) -> None:
        """Create all directories and files defined in path_specs."""
        logger.debug("Creating necessary directories and files")

        # Create directories first
        for spec in self.path_specs:
            if spec.optional:
                continue
            if spec.type == "folder":
                logger.debug("Creating directory: %s", spec.path)
                spec.path.mkdir(parents=True, exist_ok=True)

        # Create files after directories
        for spec in self.path_specs:
            if spec.optional:
                continue
            if spec.type == "file":
                logger.debug("Creating file: %s", spec.path)
                spec.path.touch(exist_ok=True)

    def resolved_mounts(self) -> List[ResolvedMount]:
        """Runtime-neutral mounts for backends to render in their own way."""
        mounts: List[ResolvedMount] = []
        for spec in self.path_specs:
            if not spec.path.exists():
                if spec.optional:
                    logger.debug("Optional path does not exist, skipping: %s", spec.path)
                    continue
                raise FileNotFoundError(f"Source path does not exist: {spec.path}")
            mounts.append(ResolvedMount(host=spec.path.resolve(), container=spec.volume_mapping, readonly=spec.readonly))
        return mounts


def unique_by_container(specs: List[PathSpec]) -> List[PathSpec]:
    """A later mount of the same guest path replaces the earlier one (keeping its position)."""
    by_container = {spec.volume_mapping: spec for spec in specs}
    return list(by_container.values())


def get_container_base_name():
    """Get the base name for containers"""
    # {base}-{folder}-{number}

    return CONTAINER_NAME_BASE


def get_running_containers(backend: Backend) -> List[str]:
    """Get a list of currently running environments belonging to ccd"""
    running = [name for name in backend.list_running() if name.startswith(CONTAINER_NAME_BASE + "-")]
    logger.debug("Currently running containers: %s", running)
    return list(sorted(running))


def get_next_free_container_name(backend: Backend, folder: str) -> str:
    """Check current running environments and when starting a new one, avoid name conflict by appending a number suffix"""
    running_containers = get_running_containers(backend)

    suffix = 1
    while True:
        candidate_name = f"{CONTAINER_NAME_BASE}-{folder}-{suffix:02d}"
        if candidate_name not in running_containers:
            logger.debug("Selected container name: %s", candidate_name)
            return candidate_name
        suffix += 1


def prompt_container_name(backend: Backend):
    """Prompt user for a container name"""

    while True:
        running = get_running_containers(backend)

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


def run_container(backend: Backend, params: RunParameters, backend_options: dict | None = None) -> int:
    """Resolve mounts and hand the run off to the backend; returns the exit code of the shell or command"""
    logger.info("Preparing to run container: %s", image_ref(params.image_name, params.tag))
    require_tag_support(backend, params.tag)

    # Convert to absolute paths
    app_path = Path(params.app_folder).resolve()
    home_path = Path(params.home_folder).resolve()

    logger.debug("App folder resolved to: %s", app_path)
    logger.debug("Home folder resolved to: %s", home_path)

    path_specs = [
        PathSpec(path=app_path, volume_mapping=Path("/app")),
        *unique_by_container([m.to_path_spec(home_path) for m in [*params.feature_mounts, *params.extra_mounts]]),
    ]

    volume_manager = VolumeManager(path_specs)
    volume_manager.prepare_paths()
    mounts = volume_manager.resolved_mounts()

    container_name = get_next_free_container_name(backend, app_path.name)

    # A one-off command's stdout belongs to the command (it may be piped), so the banner goes to stderr
    print(f"Starting container '{container_name}'...", file=sys.stderr if params.command else sys.stdout)

    logger.info("Starting container with:")
    logger.info("  Backend: %s", backend.name)
    logger.info("  App folder: %s", app_path)
    logger.info("  Home folder: %s", home_path)
    logger.info("  Memory limit: %s", params.memory)
    logger.info("  CPU limit: %s", params.cpus)
    logger.info("  User: %s", "root" if params.root else "user")
    logger.info("  Features: %s", ", ".join(params.features) or "none")
    logger.info("  Mounts:")
    for mount in mounts:
        logger.info("    %s -> %s%s", mount.host, mount.container, " (ro)" if mount.readonly else "")

    return (
        backend.run(
            RunRequest(
                image_name=params.image_name,
                tag=params.tag,
                name=container_name,
                memory=params.memory,
                cpus=params.cpus,
                root=params.root,
                mounts=mounts,
                options=backend_options or {},
                command=params.command,
            )
        )
        or 0
    )


def attach_container(backend: Backend, root: bool, backend_options: dict | None = None) -> None:
    """Attach to a running container"""
    container_name = prompt_container_name(backend)

    print(f"Attaching to container '{container_name}'...")

    backend.attach(AttachRequest(name=container_name, root=root, options=backend_options or {}))


def add_backend_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--backend",
        default=None,
        help=f"Runtime backend plugin (default: [run].backend from {CONFIG_FILE_NAME}, else {DEFAULT_BACKEND})",
    )


def add_container_args(parser: argparse.ArgumentParser, home_folder: Path) -> argparse.ArgumentParser:
    """Add common container arguments to a parser"""
    parser.add_argument("--home", default=home_folder, help=f"Home folder path (default: ${home_folder})")
    parser.add_argument("--memory", default="1g", help="Memory limit for the container (default: 1g)")
    parser.add_argument("--cpus", default="2", help="Number of CPUs for the container (default: 2)")
    parser.add_argument("--root", action="store_true", help="Run as root (same as --user root)")
    parser.add_argument("--tag", default=None, help=f"Image tag to run (default: [run] tag from {CONFIG_FILE_NAME}, else {DEFAULT_TAG})")
    add_user_arg(parser)
    add_backend_arg(parser)
    return parser


def add_user_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-u", "--user", dest="run_user", choices=["root", "user"], default=None, help="User to log in as (default: user)")


def load_backend(name: str, run_config: RunConfig) -> tuple[Backend, dict]:
    """Load a backend plugin together with its [<name>] options table."""
    return get_backend(name), run_config.backend_options.get(name, {})


def run_backend_name(args: argparse.Namespace, run_config: RunConfig) -> str:
    """CLI --backend beats [run].backend beats the default."""
    return getattr(args, "backend", None) or run_config.backend or DEFAULT_BACKEND


def require_tag_support(backend: Backend, tag: str) -> None:
    if tag != DEFAULT_TAG and not backend.supports_tags:
        raise BackendError(f"Backend '{backend.name}' keeps a single image, so image tag '{tag}' is not supported (only '{DEFAULT_TAG}')")


def resolve_backends(run_config: RunConfig, build_config: BuildConfig, build_override: str | None = None) -> tuple[str, str]:
    """(build backend, run backend): CLI override beats [build].backend beats the run backend."""
    run_name = run_config.backend or DEFAULT_BACKEND
    return build_override or build_config.backend or run_name, run_name


def try_backend(name: str, run_config: RunConfig) -> tuple[Backend | None, dict, str]:
    """Load a backend for reporting commands: (backend, options, "") or (None, {}, why it is unavailable)."""
    try:
        backend, options = load_backend(name, run_config)
    except BackendError as e:
        return None, {}, str(e)
    return backend, options, ""


def print_table(rows: list[tuple[str, ...]]) -> None:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def import_into_runner(run_config: RunConfig, builder: Backend, builder_options: dict, build_name: str, run_name: str, config: BuildConfig, tag: str) -> None:
    """Hand the image tagged `tag` from its builder to the (different) run backend."""
    runner, runner_options = load_backend(run_name, run_config)
    logger.info("Handing image from '%s' to '%s'", build_name, run_name)
    with timed(f"Import ({run_name})", logger):
        fmt = hand_over(
            builder,
            runner,
            BuildRequest(image_name=IMAGE_NAME, config=config, options=builder_options, tag=tag),
            BuildRequest(image_name=IMAGE_NAME, config=config, options=runner_options, tag=tag),
        )
    logger.info("Handed over as '%s'", fmt)


def tag_image(run_config: RunConfig, source: str, target: str, build_override: str | None = None) -> None:
    """Add the tag `target` to the image tagged `source` on the build backend (promote a build, or keep a backup)."""
    for tag in (source, target):
        try:
            validate_tag(tag)
        except ValueError as e:
            raise BackendError(str(e)) from e
    config = BuildConfig.load(Path(CONFIG_FILE_NAME))
    build_name, run_name = resolve_backends(run_config, config, build_override)
    backend, options = load_backend(build_name, run_config)
    require_tag_support(backend, source)
    require_tag_support(backend, target)
    backend.tag_image(IMAGE_NAME, source, target, options)
    print(f"Tagged {image_ref(IMAGE_NAME, source)} as {image_ref(IMAGE_NAME, target)} in {build_name}")
    # The run backend only ever sees the default tag, so moving it is what hands the image over
    if target == DEFAULT_TAG and build_name != run_name:
        import_into_runner(run_config, backend, options, build_name, run_name, config, DEFAULT_TAG)


def show_features(features: dict[str, Feature]) -> None:
    rows = [("FEATURE", "DEFAULT", "REQUIRES", "VERSIONS", "SOURCE")]
    for feature in features.values():
        versions = ", ".join(f"{name}={default}" for name, default in feature.versions.items())
        rows.append((feature.name, "yes" if feature.default else "no", ", ".join(feature.requires) or "-", versions or "-", feature.source))
    print_table(rows)


def show_metadata(run_config: RunConfig, build_override: str | None = None, tag: str | None = None, as_json: bool = False) -> None:
    """What the built image contains: the list ccd wrote to the image at build time."""
    build_config = BuildConfig.load(Path(CONFIG_FILE_NAME))
    tag = tag or build_config.tag or DEFAULT_TAG
    build_name, _ = resolve_backends(run_config, build_config, build_override)
    backend, options = load_backend(build_name, run_config)
    require_tag_support(backend, tag)
    text = backend.read_image_file(IMAGE_NAME, tag, INSTALLED_FILE, options)
    if as_json:
        print(text, end="")
        return
    try:
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    except ValueError as e:
        raise BackendError(f"{INSTALLED_FILE} in {image_ref(IMAGE_NAME, tag)} is not valid: {e}") from e
    rows = [("FEATURE", "RESOLVED", "SOURCE")]
    rows.extend((r["name"], r.get("resolved") or "-", r.get("source", "-")) for r in records)
    print_table(rows)


def show_backends(run_config: RunConfig) -> None:
    rows = [("BACKEND", "CAN", "STATE")]
    for name, entry in sorted(available_backends().items()):
        plugin = entry.load()
        can = "+".join(w for w, ok in (("build", plugin.can_build), ("run", plugin.can_run)) if ok)
        backend, _, why = try_backend(name, run_config)
        rows.append((name, can, "ready" if backend else f"not ready: {why}"))
    print_table(rows)


def show_images(run_config: RunConfig, only: str | None = None) -> None:
    """Build data of every backend that is ready (build data can sit in several places), or of just `only`."""
    rows = [("BACKEND", "KIND", "NAME", "ID", "SIZE", "CREATED")]
    seen: set[tuple[str, str]] = set()  # buildah and podman share one store: list each image once
    for name in [only] if only else sorted(available_backends()):
        backend, options, why = try_backend(name, run_config)
        if backend is None:
            if only:
                print(f"{name}: not ready: {why}")
            continue
        for i in backend.images(options):
            if i.id and (i.name, short_id(i.id)) in seen:  # ids come short or full depending on the tool
                continue
            seen.add((i.name, short_id(i.id)))
            rows.append((i.backend, i.kind, i.name, short_id(i.id), human_size(i.size) if i.size is not None else "", format_age(i.created) if i.created else ""))
    if len(rows) == 1:
        print("No build data yet (run 'ccd build')")
    else:
        print_table(rows)


def show_status(run_config: RunConfig, build_override: str | None = None, tag: str | None = None) -> None:
    build_config = BuildConfig.load(Path(CONFIG_FILE_NAME))
    tag = tag or build_config.tag or DEFAULT_TAG
    build_name, run_name = resolve_backends(run_config, build_config, build_override)
    builder, builder_options, build_why = try_backend(build_name, run_config)
    runner, runner_options, run_why = try_backend(run_name, run_config)

    print(f"Build backend: {build_name} ({'ready' if builder else 'not ready: ' + build_why})")
    print(f"Run backend:   {run_name} ({'ready' if runner else 'not ready: ' + run_why})")
    built_id = ""
    if builder is not None:
        built_id = builder.built_image_id(BuildRequest(image_name=IMAGE_NAME, config=build_config, options=builder_options, tag=tag))
        print(f"Image '{image_ref(IMAGE_NAME, tag)}' in {build_name}: {built_id or 'not built (run ccd build)'}")
    if runner is not None and build_name != run_name:
        verdict = runner.describe_image(built_id, runner_options)
        if verdict:
            print(f"Image for {run_name}: {verdict}")
    if runner is not None:
        running = get_running_containers(runner)
        print(f"Running: {', '.join(running) if running else 'nothing'}")


def build_image(args: argparse.Namespace, run_config: RunConfig, extra_args: list[str], features: dict[str, Feature]) -> None:
    """Build with the build backend; when it differs from the run backend, hand the image over."""
    config = BuildConfig.load(Path(CONFIG_FILE_NAME)).apply_cli(args, known_versions(features))
    config.validate()
    logger.debug("Effective build config: %s", config)

    unknown = sorted(set(config.versions) - set(known_versions(features)))
    if unknown:
        raise BackendError(f"Unknown version(s) in [versions]: {', '.join(unknown)} (available: {', '.join(sorted(known_versions(features)))})")
    try:
        selected = select(features, config.with_features, config.without_features)
    except FeatureError as e:
        raise BackendError(str(e)) from e
    logger.info("Features: %s", ", ".join(f.name for f in selected))

    if args.print_dockerfile:
        print(render_dockerfile(selected, config.mode != "sequential"), end="")
        return

    build_name, run_name = resolve_backends(run_config, config, args.backend)

    tag = config.tag or DEFAULT_TAG
    builder, builder_options = load_backend(build_name, run_config)
    require_tag_support(builder, tag)
    hand_over_after = build_name != run_name
    if hand_over_after and tag != DEFAULT_TAG:
        # The run backend only runs the default tag: a tagged build stays with the builder until it is promoted
        runner, _, why = try_backend(run_name, run_config)
        if runner is None or not runner.supports_tags:
            reason = why if runner is None else f"it only runs '{DEFAULT_TAG}'"
            logger.warning("Not importing %s into %s (%s); 'ccd tag %s %s' imports it later", image_ref(IMAGE_NAME, tag), run_name, reason, tag, DEFAULT_TAG)
            hand_over_after = False

    with timed(f"Build ({build_name})", logger), tempfile.TemporaryDirectory(prefix="ccd-build-") as context:
        dockerfile = stage_context(selected, Path(context), config.mode != "sequential")
        request = BuildRequest(image_name=IMAGE_NAME, config=config, extra_args=extra_args, options=builder_options, dockerfile=dockerfile, context=Path(context), tag=tag)
        builder.build(request)

    if hand_over_after:
        import_into_runner(run_config, builder, builder_options, build_name, run_name, config, tag)


def split_command(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split `ccd . -- pytest -q` into ccd's own arguments and the one-off command. Only `.` and `run` take a command;
    for the other subcommands `--` keeps its meaning (extra build arguments)."""
    if "--" not in argv:
        return argv, []
    head, command = argv[: argv.index("--")], argv[argv.index("--") + 1 :]
    return (head, command) if any(arg in (".", "run") for arg in head) else (argv, [])


def main() -> None:
    """Main entry point for the ccd command line tool"""
    try:
        features = load_features()
    except FeatureError as e:
        logger.error("%s", e)
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Run ccd containers on pluggable backends")

    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity level (can be used multiple times: -v, -vv, -vvv)")

    command_parser = parser.add_subparsers(dest="command", required=True, help="Command to execute")
    command_parser.required = True

    build_parser = command_parser.add_parser("build", help="Build the image")
    build_parser.add_argument(
        "--backend",
        default=None,
        help=f"Backend that builds the image (default: [build].backend from {CONFIG_FILE_NAME}, else the run backend)",
    )
    build_parser.add_argument(
        "--tag",
        default=None,
        help=f"Tag the image with this instead of {DEFAULT_TAG}; {DEFAULT_TAG} is left alone (default: [build] tag from {CONFIG_FILE_NAME})",
    )
    build_parser.add_argument(
        "--mode",
        choices=BUILD_MODES,
        default=None,
        help=f"parallel: one build stage per feature, independent ones are built at the same time; sequential: every feature in order in a single stage (default: [build] mode from {CONFIG_FILE_NAME}, else {DEFAULT_BUILD_MODE})",
    )
    build_parser.add_argument(
        "--with",
        dest="with_features",
        help="Comma-separated features to install, plus what they require (default: all default features)",
    )
    build_parser.add_argument(
        "--without",
        dest="without_features",
        help="Comma-separated features to leave out of the image",
    )
    build_parser.add_argument(
        "--force",
        dest="force",
        action="store_true",
        help="Build without cache (disable Docker layer caching)",
    )
    build_parser.add_argument(
        "--npm-min-release-age-days",
        dest="npm_min_release_age_days",
        default=None,
        type=int,
        help="Minimum age in days for npm CLI package resolution when using tags such as latest (default: 7)",
    )
    build_parser.add_argument(
        "--no-cooldown",
        dest="npm_min_release_age_ignore_components",
        metavar="COMPONENTS",
        help="Comma-separated CLI components to exempt from the release-age cooldown (e.g. pi,gemini)",
    )
    build_parser.add_argument(
        "--npm-audit-ignore-components",
        dest="npm_audit_ignore_components",
        help="Comma-separated npm CLI components whose final npm audit failure should be ignored",
    )
    build_parser.add_argument(
        "--npm-audit-force-fix-components",
        dest="npm_audit_force_fix_components",
        help="Comma-separated npm CLI components that should run npm audit fix --force before final audit checks",
    )
    build_parser.add_argument(
        "--print-dockerfile",
        dest="print_dockerfile",
        action="store_true",
        help="Print the Dockerfile generated for the selected features and exit",
    )
    # Every feature has a version, plus any extra ones it declares
    for component, default in known_versions(features).items():
        build_parser.add_argument(
            f"--{component}-version",
            dest=f"{component.replace('-', '_')}_version",
            help=f"Version of {component} to install (default: {default})",
        )

    home_folder = Path.home() / ".claude-code-docker"

    dot_parser = command_parser.add_parser(".", help="Run the container in current directory")
    add_container_args(dot_parser, home_folder)

    run_parser = command_parser.add_parser("run", help="Run the container")
    run_parser.add_argument("app_folder", nargs="?", default="./app", help="App folder path (default: ./app)")
    add_container_args(run_parser, home_folder)

    attach_parser = command_parser.add_parser("attach", help="Attach to a running container")
    attach_parser.add_argument("--root", action="store_true", help="Attach as root (same as --user root)")
    add_user_arg(attach_parser)
    add_backend_arg(attach_parser)

    command_parser.add_parser("backends", help="List installed backend plugins and whether they are ready")
    command_parser.add_parser("features", help="List the features the image can be built from")
    metadata_parser = command_parser.add_parser("metadata", help="Show what the built image has installed (features and their resolved versions)")
    metadata_parser.add_argument("--backend", default=None, help="Build backend holding the image (default: as configured, like ccd build --backend)")
    metadata_parser.add_argument("--tag", default=None, help=f"Image tag to look at (default: [build] tag from {CONFIG_FILE_NAME}, else {DEFAULT_TAG})")
    metadata_parser.add_argument("--json", action="store_true", help="Print the raw list, one JSON object per line, with the install commands")
    tag_parser = command_parser.add_parser("tag", help="Give an image tag another tag, for example ccd tag next latest to promote a build")
    tag_parser.add_argument("source", help="Existing tag")
    tag_parser.add_argument("target", help="Tag to create or replace")
    tag_parser.add_argument("--backend", default=None, help="Build backend holding the image (default: as configured, like ccd build --backend)")
    images_parser = command_parser.add_parser("images", help="List the build data (images, rootfs, kernel) of all ready backends")
    images_parser.add_argument("--backend", default=None, help="Only list this backend")
    status_parser = command_parser.add_parser("status", help="Show the configured backends, the built image and whether the run backend's copy is current")
    status_parser.add_argument("--backend", default=None, help="Build backend to compare with (default: as configured, like ccd build --backend)")
    status_parser.add_argument("--tag", default=None, help=f"Image tag to look at (default: [build] tag from {CONFIG_FILE_NAME}, else {DEFAULT_TAG})")

    prune_parser = command_parser.add_parser("prune", help="Remove leftovers of dead runs (VM state, taps, exports, firewall rules)")
    prune_parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True, help="Only list what would be removed (default); --no-dry-run removes it")
    add_backend_arg(prune_parser)

    argv, command = split_command(sys.argv[1:])
    args, unknown = parser.parse_known_args(argv)
    args.run_command = command
    if getattr(args, "run_user", None) is not None:
        args.root = args.run_user == "root"  # -u wins over --root when both are given

    # Setup logging based on verbosity
    setup_logging(args.verbose)

    logger.debug("Arguments parsed: %s", args)
    logger.debug("Image name: %s", IMAGE_NAME)

    try:
        run_config = RunConfig.load_effective(Path(CONFIG_FILE_NAME))

        if args.command == "backends":
            show_backends(run_config)
        elif args.command == "features":
            show_features(features)
        elif args.command == "metadata":
            show_metadata(run_config, args.backend, args.tag, args.json)
        elif args.command == "tag":
            tag_image(run_config, args.source, args.target, args.backend)
        elif args.command == "images":
            show_images(run_config, args.backend)
        elif args.command == "status":
            show_status(run_config, args.backend, args.tag)
        elif args.command == "build":
            logger.debug("Executing build command")
            build_image(args, run_config, unknown, features)
        elif args.command in ("run", "."):
            logger.debug("Executing run command")
            backend, backend_options = load_backend(run_backend_name(args, run_config), run_config)
            app_folder = "." if args.command == "." else None
            params = RunParameters.from_args(IMAGE_NAME, args, run_config=run_config, app_folder=app_folder, features=features)
            code = run_container(backend, params, backend_options)
            if code:
                sys.exit(code)
        elif args.command == "prune":
            backend, backend_options = load_backend(run_backend_name(args, run_config), run_config)
            done = backend.prune(backend_options, args.dry_run)
            for line in done:
                print(f"{'Would: ' if args.dry_run else ''}{line}")
            if not done:
                print("Nothing to prune")
            elif args.dry_run:
                print("Dry run: nothing was removed; use --no-dry-run to remove it")
        elif args.command == "attach":
            logger.debug("Executing attach command")
            backend, backend_options = load_backend(run_backend_name(args, run_config), run_config)
            attach_container(backend, args.root, backend_options)
    except BackendError as e:
        logger.error("%s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
