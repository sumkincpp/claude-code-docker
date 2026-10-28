import argparse
import logging
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, List, Literal

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
DEFAULT_TAG = "latest"
TAG_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
# Exported images handed from a build backend to a different run backend


if TYPE_CHECKING:
    from ccd.features import Feature

logger = logging.getLogger(__name__)


def validate_tag(tag: str) -> str:
    """The tag itself, or ValueError when container tools would not accept it."""
    if not TAG_PATTERN.match(tag):
        raise ValueError(f"Invalid image tag {tag!r} (letters, digits, '_', '.' and '-', at most 128 characters, not starting with '.' or '-')")
    return tag


def image_ref(name: str, tag: str = DEFAULT_TAG) -> str:
    """name for the default tag (which container tools resolve to :latest anyway), name:tag otherwise."""
    return name if tag == DEFAULT_TAG else f"{name}:{tag}"


def _parse_comma_set(value: str) -> set[str]:
    return {v.strip() for v in value.split(",") if v.strip()}


def _load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


@dataclass
class BuildConfig:
    backend: str | None = None
    tag: str | None = None
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
            tag=build.get("tag"),
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

        if getattr(args, "tag", None) is not None:
            config.tag = args.tag
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
        if self.tag is not None:
            try:
                validate_tag(str(self.tag))
            except ValueError as e:
                logger.error("%s", e)
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
        """Raises ValueError for an invalid entry."""
        if "host" not in raw or "container" not in raw:
            raise ValueError(f"Each {where} entry must have 'host' and 'container' keys")
        mount_type = raw.get("type", "folder")
        if mount_type not in ("file", "folder"):
            raise ValueError(f"Invalid mount type {mount_type!r} in {where} (expected 'file' or 'folder')")
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
    tag: str | None = None
    memory: str | None = None
    cpus: str | None = None
    mounts: List[MountConfig] = field(default_factory=list)
    # Per-feature mount overrides ([features.<name>] mounts = [...]) and the [run] features selection.
    feature_mounts: dict[str, List[MountConfig]] = field(default_factory=dict)
    features: list[str] | None = None
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

        try:
            mounts = [MountConfig.parse(m, "[[run.mounts]]") for m in raw_mounts]
            feature_mounts = {
                name: [MountConfig.parse(m, f"[features.{name}] mounts") for m in table.get("mounts", [])]
                for name, table in data.get("features", {}).items()
            }
        except ValueError as e:
            logger.error("%s", e)
            sys.exit(1)

        reserved = {"build", "versions", "run", "features"}
        backend_options = {k: v for k, v in data.items() if k not in reserved and isinstance(v, dict)}

        return cls(
            backend=run.get("backend"),
            tag=run.get("tag"),
            memory=run.get("memory"),
            cpus=run.get("cpus"),
            mounts=mounts,
            feature_mounts=feature_mounts,
            features=list(run["features"]) if "features" in run else None,
            backend_options=backend_options,
        )

    def merge(self, local: "RunConfig") -> "RunConfig":
        """Return a new config with local values taking precedence over self (global).

        Scalar fields (backend, tag, memory, cpus): local wins if set, otherwise fall back to global.
        Mounts: global mounts first, then local mounts (both are applied).
        Feature mounts: merged per feature, the local list replaces the global one; features follows the scalar rule.
        Backend option tables: merged per backend, local keys win.
        """
        options = {name: {**opts} for name, opts in self.backend_options.items()}
        for name, opts in local.backend_options.items():
            options.setdefault(name, {}).update(opts)

        return RunConfig(
            backend=local.backend if local.backend is not None else self.backend,
            tag=local.tag if local.tag is not None else self.tag,
            memory=local.memory if local.memory is not None else self.memory,
            cpus=local.cpus if local.cpus is not None else self.cpus,
            mounts=self.mounts + local.mounts,
            feature_mounts={**self.feature_mounts, **local.feature_mounts},
            features=local.features if local.features is not None else self.features,
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


    def selected_features(self, features: "dict[str, Feature]") -> List[str]:
        """Features whose mounts apply: those with mounts unless [run] features says otherwise."""
        names = [n for n, f in features.items() if f.mounts] if self.features is None else self.features
        unknown = [n for n in [*names, *self.feature_mounts] if n not in features]
        if unknown:
            logger.error("Unknown feature(s) in the config: %s", ", ".join(dict.fromkeys(unknown)))
            logger.error("Available features: %s", ", ".join(features))
            sys.exit(1)
        return names

    def selected_mounts(self, features: "dict[str, Feature]") -> List[MountConfig]:
        """Mounts of the selected features: the feature's own, unless [features.NAME] replaces them."""
        return [m for name in self.selected_features(features) for m in self.feature_mounts.get(name, features[name].mounts)]


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
    tag: str = DEFAULT_TAG
    memory: str = "1g"
    cpus: str = "2"
    root: bool = False
    features: List[str] = field(default_factory=list)
    feature_mounts: List[MountConfig] = field(default_factory=list)
    extra_mounts: List[MountConfig] = field(default_factory=list)
    command: List[str] = field(default_factory=list)

    @classmethod
    def from_args(
        cls,
        image_name: str,
        args: argparse.Namespace,
        run_config: "RunConfig | None" = None,
        app_folder: str | None = None,
        features: "dict[str, Feature] | None" = None,
    ) -> "RunParameters":
        """Create RunParameters from CLI args, optionally layering a RunConfig on top.

        Args:
            image_name: Name of the image to use
            args: Parsed command line arguments
            run_config: Optional config loaded from ccd.toml (config values are
                        overridden by any explicitly supplied CLI args)
            app_folder: Override for app_folder (defaults to args.app_folder or ".")
            features: The loaded features, whose mounts are added to the run
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

        tag = getattr(args, "tag", None) or (run_config.tag if run_config is not None else None) or DEFAULT_TAG
        try:
            validate_tag(str(tag))
        except ValueError as e:
            logger.error("%s", e)
            sys.exit(1)

        return cls(
            image_name=image_name,
            app_folder=app_folder,
            home_folder=args.home,
            tag=str(tag),
            memory=memory,
            cpus=cpus,
            root=args.root,
            features=run_config.selected_features(features) if run_config is not None and features else [],
            feature_mounts=run_config.selected_mounts(features) if run_config is not None and features else [],
            extra_mounts=run_config.mounts if run_config is not None else [],
            command=list(getattr(args, "run_command", [])),
        )
