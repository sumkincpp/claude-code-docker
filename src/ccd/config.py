import argparse
import logging
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import List, Literal

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
# Exported images handed from a build backend to a different run backend

FEATURE_BUILD_ARGS = {
    "rust": "WITH_RUST",
    "claude": "WITH_CLAUDE",
    "codex": "WITH_CODEX",
    "gemini": "WITH_GEMINI",
    "opencode": "WITH_OPENCODE",
    "copilot": "WITH_COPILOT",
    "jules": "WITH_JULES",
    "pi": "WITH_PI",
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
    "jules": "JULES_VERSION",
    "pi": "PI_VERSION",
}

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
            versions={k: str(v) for k, v in versions_data.items() if k in VERSION_BUILD_ARGS},
        )

    def apply_cli(self, args: argparse.Namespace) -> "BuildConfig":
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

        for component in VERSION_BUILD_ARGS:
            version = getattr(args, f"{component}_version", None)
            if version is not None:
                config.versions[component] = version

        return config

    def validate(self) -> None:
        if self.with_features is not None and self.without_features is not None:
            logger.error("Use only one of --with or --without")
            sys.exit(1)
        for features in (self.with_features, self.without_features):
            if features is None:
                continue
            unknown = features - set(FEATURE_BUILD_ARGS.keys())
            if unknown:
                logger.error("Unknown feature(s): %s", ", ".join(sorted(unknown)))
                logger.error("Available features: %s", ", ".join(sorted(FEATURE_BUILD_ARGS.keys())))
                sys.exit(1)

    def feature_flags(self) -> dict[str, bool] | None:
        """Resolve --with/--without into {feature: enabled}; None means 'use the image defaults'."""
        if self.with_features is not None:
            return {f: f in self.with_features for f in FEATURE_BUILD_ARGS}
        if self.without_features is not None:
            return {f: f not in self.without_features for f in FEATURE_BUILD_ARGS}
        return None


@dataclass
class MountConfig:
    host: str
    container: str
    type: Literal["file", "folder"] = "folder"
    optional: bool = False
    readonly: bool = False

    def to_path_spec(self) -> "PathSpec":
        return PathSpec(
            path=Path(self.host).expanduser(),
            volume_mapping=Path(self.container),
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

        mounts: List[MountConfig] = []
        for m in raw_mounts:
            if "host" not in m or "container" not in m:
                logger.error("Each [[run.mounts]] entry must have 'host' and 'container' keys")
                sys.exit(1)
            mounts.append(MountConfig(
                host=m["host"],
                container=m["container"],
                type=m.get("type", "folder"),
                optional=m.get("optional", False),
                readonly=m.get("readonly", False),
            ))

        reserved = {"build", "versions", "run"}
        backend_options = {k: v for k, v in data.items() if k not in reserved and isinstance(v, dict)}

        return cls(
            backend=run.get("backend"),
            memory=run.get("memory"),
            cpus=run.get("cpus"),
            mounts=mounts,
            backend_options=backend_options,
        )

    def merge(self, local: "RunConfig") -> "RunConfig":
        """Return a new config with local values taking precedence over self (global).

        Scalar fields (backend, memory, cpus): local wins if set, otherwise fall back to global.
        Mounts: global mounts first, then local mounts (both are applied).
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
            backend_options=options,
        )

    @classmethod
    def load_effective(cls, local_path: Path, global_path: Path = GLOBAL_CONFIG_PATH) -> "RunConfig":
        """Load and merge global and local configs. Local file wins for scalars; mounts are combined."""
        global_cfg = cls.load(global_path)
        local_cfg = cls.load(local_path)
        effective = global_cfg.merge(local_cfg)
        if global_cfg.mounts or global_cfg.memory or global_cfg.cpus:
            logger.debug("Global run config loaded from: %s", global_path)
        logger.debug("Effective run config: %s", effective)
        return effective


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
    extra_mounts: List[MountConfig] = field(default_factory=list)

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
            extra_mounts=run_config.mounts if run_config is not None else [],
        )
