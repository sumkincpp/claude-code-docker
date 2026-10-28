import argparse
import logging
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, List, Literal

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

IMAGE_NAME = "ccd"
IMAGE_HOME_FOLDER = "/home/user"
# ccdXX
CONTAINER_NAME_BASE = "ccd"
CONFIG_FILE_NAME = "ccd.toml"
GLOBAL_CONFIG_PATH = Path.home() / ".config" / "ccd" / "ccd.toml"
DEFAULT_BACKEND = "docker"
DEFAULTS_PATH = Path(__file__).with_name("defaults.toml")
# Exported images handed from a build backend to a different run backend


logger = logging.getLogger(__name__)


def _parse_comma_set(value: str) -> set[str]:
    return {v.strip() for v in value.split(",") if v.strip()}


def _load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


@dataclass
class BuildConfig:
    backend: str | None = None
    with_features: set[str] | None = None
    without_features: set[str] | None = None
    force: bool = False
    npm_min_release_age_days: int = 7
    npm_audit_ignore_components: set[str] = field(default_factory=set)
    npm_audit_force_fix_components: set[str] = field(default_factory=set)
    npm_min_release_age_ignore_components: set[str] = field(default_factory=set)
    versions: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "BuildConfig":
        """Load config from a TOML file; returns defaults if file does not exist."""
        if not path.exists():
            return cls()
        if tomllib is None:
            logger.error("Config file found but tomllib is unavailable. Upgrade to Python 3.11+ or install 'tomli'.")
            sys.exit(1)

        data = _load_toml(path)

        build = data.get("build", {})
        versions_data = data.get("versions", {})

        def as_set(v: object) -> set[str]:
            if isinstance(v, list):
                return {str(x).strip() for x in v if str(x).strip()}
            if isinstance(v, str):
                return _parse_comma_set(v)
            return set()

        return cls(
            backend=build.get("backend"),
            with_features=as_set(build["with_features"]) if "with_features" in build else None,
            without_features=as_set(build["without_features"]) if "without_features" in build else None,
            npm_min_release_age_days=build.get("npm_min_release_age_days", 7),
            npm_audit_ignore_components=as_set(build.get("npm_audit_ignore_components", [])),
            npm_audit_force_fix_components=as_set(build.get("npm_audit_force_fix_components", [])),
            npm_min_release_age_ignore_components=as_set(build.get("npm_min_release_age_ignore_components", [])),
            versions={k: str(v) for k, v in versions_data.items()},
        )

    def apply_cli(self, args: argparse.Namespace, version_names: Iterable[str] = ()) -> "BuildConfig":
        """Return a new config with explicitly provided CLI args applied on top."""
        config = replace(self)

        if args.with_features is not None:
            config.with_features = _parse_comma_set(args.with_features)
        if args.without_features is not None:
            config.without_features = _parse_comma_set(args.without_features)
        if args.force:
            config.force = True
        if args.npm_min_release_age_days is not None:
            config.npm_min_release_age_days = args.npm_min_release_age_days
        if args.npm_audit_ignore_components is not None:
            config.npm_audit_ignore_components = _parse_comma_set(args.npm_audit_ignore_components)
        if args.npm_audit_force_fix_components is not None:
            config.npm_audit_force_fix_components = _parse_comma_set(args.npm_audit_force_fix_components)
        if args.npm_min_release_age_ignore_components is not None:
            config.npm_min_release_age_ignore_components = _parse_comma_set(args.npm_min_release_age_ignore_components)

        for component in version_names:
            version = getattr(args, f"{component.replace('-', '_')}_version", None)
            if version is not None:
                config.versions[component] = version

        return config

    def validate(self) -> None:
        if self.with_features is not None and self.without_features is not None:
            logger.error("Use only one of --with or --without")
            sys.exit(1)


@dataclass
class MountConfig:
    host: str
    container: str
    type: Literal["file", "folder"] = "folder"
    optional: bool = False
    readonly: bool = False

    @classmethod
    def parse(cls, raw: dict, where: str) -> "MountConfig":
        if "host" not in raw or "container" not in raw:
            logger.error("Each %s entry must have 'host' and 'container' keys", where)
            sys.exit(1)
        mount_type = raw.get("type", "folder")
        if mount_type not in ("file", "folder"):
            logger.error("Invalid mount type %r in %s (expected 'file' or 'folder')", mount_type, where)
            sys.exit(1)
        return cls(
            host=raw["host"],
            container=raw["container"],
            type=mount_type,
            optional=raw.get("optional", False),
            readonly=raw.get("readonly", False),
        )

    def to_path_spec(self, home: Path) -> "PathSpec":
        """Expand {home} (the host --home folder) in host and a leading ~/ (the image home) in container."""
        container = self.container
        if container == "~" or container.startswith("~/"):
            container = IMAGE_HOME_FOLDER + container[1:]
        return PathSpec(
            path=Path(self.host.replace("{home}", str(home))).expanduser(),
            volume_mapping=Path(container),
            type=self.type,
            optional=self.optional,
            readonly=self.readonly,
        )


@dataclass
class RunConfig:
    backend: str | None = None
    memory: str | None = None
    cpus: str | None = None
    mounts: List[MountConfig] = field(default_factory=list)
    # Built-in and user-defined agent config mounts ([agents.<name>] mounts = [...]) and the [run] agents selection.
    agent_mounts: dict[str, List[MountConfig]] = field(default_factory=dict)
    agents: list[str] | None = None
    # Raw per-backend tables (e.g. [firecracker]) handed to the matching plugin untouched.
    backend_options: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "RunConfig":
        """Load [run] section from a ccd.toml file; returns defaults if absent."""
        if not path.exists():
            return cls()
        if tomllib is None:
            logger.error("Config file found but tomllib is unavailable. Upgrade to Python 3.11+ or install 'tomli'.")
            sys.exit(1)

        data = _load_toml(path)

        run = data.get("run", {})
        raw_mounts = run.get("mounts", [])

        mounts = [MountConfig.parse(m, "[[run.mounts]]") for m in raw_mounts]
        agent_mounts = {
            name: [MountConfig.parse(m, f"[agents.{name}] mounts") for m in table.get("mounts", [])]
            for name, table in data.get("agents", {}).items()
        }

        reserved = {"build", "versions", "run", "agents"}
        backend_options = {k: v for k, v in data.items() if k not in reserved and isinstance(v, dict)}

        return cls(
            backend=run.get("backend"),
            memory=run.get("memory"),
            cpus=run.get("cpus"),
            mounts=mounts,
            agent_mounts=agent_mounts,
            agents=list(run["agents"]) if "agents" in run else None,
            backend_options=backend_options,
        )

    def merge(self, local: "RunConfig") -> "RunConfig":
        """Return a new config with local values taking precedence over self (global).

        Scalar fields (backend, memory, cpus): local wins if set, otherwise fall back to global.
        Mounts: global mounts first, then local mounts (both are applied).
        Agent mounts: merged per agent, the local list replaces the global one; agents follows the scalar rule.
        Backend option tables: merged per backend, local keys win.
        """
        options = {name: {**opts} for name, opts in self.backend_options.items()}
        for name, opts in local.backend_options.items():
            options.setdefault(name, {}).update(opts)

        return RunConfig(
            backend=local.backend if local.backend is not None else self.backend,
            memory=local.memory if local.memory is not None else self.memory,
            cpus=local.cpus if local.cpus is not None else self.cpus,
            mounts=self.mounts + local.mounts,
            agent_mounts={**self.agent_mounts, **local.agent_mounts},
            agents=local.agents if local.agents is not None else self.agents,
            backend_options=options,
        )

    @classmethod
    def load_effective(cls, local_path: Path, global_path: Path = GLOBAL_CONFIG_PATH) -> "RunConfig":
        """Load and merge bundled defaults, global and local configs. Local file wins for scalars; mounts are combined."""
        global_cfg = cls.load(DEFAULTS_PATH).merge(cls.load(global_path))
        local_cfg = cls.load(local_path)
        effective = global_cfg.merge(local_cfg)
        if global_cfg.mounts or global_cfg.memory or global_cfg.cpus:
            logger.debug("Global run config loaded from: %s", global_path)
        logger.debug("Effective run config: %s", effective)
        return effective


    def selected_agents(self) -> List[str]:
        """Selected agents: all declared ones unless [run] agents says otherwise."""
        names = list(self.agent_mounts) if self.agents is None else self.agents
        unknown = [n for n in names if n not in self.agent_mounts]
        if unknown:
            logger.error("Unknown agent(s) in [run] agents: %s", ", ".join(unknown))
            logger.error("Declared agents: %s", ", ".join(self.agent_mounts))
            sys.exit(1)
        return names

    def selected_agent_mounts(self) -> List[MountConfig]:
        """Mounts of the selected agents, in declaration order."""
        return [m for name in self.selected_agents() for m in self.agent_mounts[name]]


@dataclass
class PathSpec:
    path: Path
    volume_mapping: Path
    type: Literal["file", "folder"] = "folder"
    optional: bool = False
    readonly: bool = False


@dataclass
class RunParameters:
    image_name: str
    app_folder: str
    home_folder: str
    memory: str = "1g"
    cpus: str = "2"
    root: bool = False
    agents: List[str] = field(default_factory=list)
    agent_mounts: List[MountConfig] = field(default_factory=list)
    extra_mounts: List[MountConfig] = field(default_factory=list)
    command: List[str] = field(default_factory=list)

    @classmethod
    def from_args(cls, image_name: str, args: argparse.Namespace, run_config: "RunConfig | None" = None, app_folder: str | None = None) -> "RunParameters":
        """Create RunParameters from CLI args, optionally layering a RunConfig on top.

        Args:
            image_name: Name of the image to use
            args: Parsed command line arguments
            run_config: Optional config loaded from ccd.toml (config values are
                        overridden by any explicitly supplied CLI args)
            app_folder: Override for app_folder (defaults to args.app_folder or ".")
        """
        if app_folder is None:
            app_folder = args.app_folder if hasattr(args, "app_folder") else "."

        memory = args.memory
        cpus = args.cpus
        if run_config is not None:
            # CLI args take precedence; fall back to config values when CLI uses defaults
            if run_config.memory is not None and args.memory == "1g":
                memory = run_config.memory
            if run_config.cpus is not None and args.cpus == "2":
                cpus = run_config.cpus

        return cls(
            image_name=image_name,
            app_folder=app_folder,
            home_folder=args.home,
            memory=memory,
            cpus=cpus,
            root=args.root,
            agents=run_config.selected_agents() if run_config is not None else [],
            agent_mounts=run_config.selected_agent_mounts() if run_config is not None else [],
            extra_mounts=run_config.mounts if run_config is not None else [],
            command=list(getattr(args, "run_command", [])),
        )
