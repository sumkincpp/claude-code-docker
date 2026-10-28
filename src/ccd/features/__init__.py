"""Features: installable parts of the image, described by a backend-neutral manifest (feature.toml).

The kernel here only loads manifests, resolves dependencies and selects features. Turning a selection into something
concrete (a Dockerfile today) is the job of a renderer, see ccd.features.render.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ccd.config import MountConfig, _load_toml

logger = logging.getLogger(__name__)

BUNDLED_DIR = Path(__file__).parent / "bundled"
MANIFEST_NAME = "feature.toml"
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
TOP_LEVEL_KEYS = {"name", "description", "default", "order", "requires", "provides", "version", "versions", "install", "verify", "env", "args", "cache", "mounts"}
INSTALL_KEYS = {"command", "user"}


class FeatureError(ValueError):
    """A manifest is invalid, or a selection cannot be satisfied."""


@dataclass(frozen=True)
class Cache:
    id: str
    target: str


@dataclass
class Feature:
    name: str
    description: str = ""
    default: bool = True
    order: int = 50  # tie-break among features that are ready at the same time, lower goes first
    requires: tuple[str, ...] = ()  # feature names, normalized from provided capabilities when loading
    provides: tuple[str, ...] = ()
    versions: dict[str, str] = field(default_factory=dict)  # name -> default, each gives $NAME_VERSION and --name-version
    install: tuple[str, ...] = ()
    user: str = "user"
    verify: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    args: dict[str, str] = field(default_factory=dict)
    caches: tuple[Cache, ...] = ()
    mounts: tuple[MountConfig, ...] = ()  # config the feature keeps on the host, see {home} and ~/ in MountConfig.to_path_spec
    assets: Path | None = None
    source: str = "bundled"


def version_variable(name: str) -> str:
    """Environment variable (and build arg) carrying a version: claude -> CLAUDE_VERSION."""
    return name.upper().replace("-", "_") + "_VERSION"


def known_versions(features: dict[str, Feature]) -> dict[str, str]:
    """Every selectable version across the features: name -> default."""
    return {key: default for feature in features.values() for key, default in feature.versions.items()}


def _commands(value: object, where: str) -> tuple[str, ...]:
    commands = [value] if isinstance(value, str) else value
    if not isinstance(commands, list) or not all(isinstance(c, str) and c.strip() for c in commands):
        raise FeatureError(f"{where} must be a command or a list of commands")
    for command in commands:
        if "\n" in command.strip():
            raise FeatureError(f"{where}: commands must be a single line (use a TOML line-ending backslash, or a list)")
    return tuple(c.strip() for c in commands)


def _string_table(value: object, where: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise FeatureError(f"{where} must be a table")
    return {str(k): str(v) for k, v in value.items()}


def parse_feature(data: dict, directory: Path, source: str) -> Feature:
    where = f"feature '{directory.name}' ({directory / MANIFEST_NAME})"
    unknown = set(data) - TOP_LEVEL_KEYS
    if unknown:
        raise FeatureError(f"{where}: unknown key(s): {', '.join(sorted(unknown))}")

    name = data.get("name", directory.name)
    if not isinstance(name, str) or not NAME_PATTERN.match(name):
        raise FeatureError(f"{where}: invalid name {name!r} (lower case letters, digits and '-')")

    install = data.get("install")
    if not isinstance(install, dict) or "command" not in install:
        raise FeatureError(f"{where}: [install] with a command is required")
    unknown = set(install) - INSTALL_KEYS
    if unknown:
        raise FeatureError(f"{where}: unknown [install] key(s): {', '.join(sorted(unknown))}")
    user = install.get("user", "user")
    if user not in ("user", "root"):
        raise FeatureError(f"{where}: [install] user must be 'user' or 'root'")

    versions: dict[str, str] = {}
    main = data.get("version", "latest")
    if main is not False:
        versions[name] = str(main)
    for key, default in _string_table(data.get("versions", {}), f"{where}: [versions]").items():
        if not NAME_PATTERN.match(key):
            raise FeatureError(f"{where}: invalid version name {key!r}")
        versions[key] = default

    caches = []
    for entry in data.get("cache", []):
        if not isinstance(entry, dict) or set(entry) != {"id", "target"}:
            raise FeatureError(f"{where}: each [[cache]] needs exactly 'id' and 'target'")
        caches.append(Cache(id=str(entry["id"]), target=str(entry["target"])))

    try:
        mounts = tuple(MountConfig.parse(m, f"{where}: [[mounts]]") for m in data.get("mounts", []))
    except ValueError as e:
        raise FeatureError(str(e)) from e

    assets = directory / "assets"
    return Feature(
        name=name,
        description=str(data.get("description", "")),
        default=bool(data.get("default", True)),
        order=int(data.get("order", 50)),
        requires=tuple(data.get("requires", ())),
        provides=tuple(dict.fromkeys([name, *data.get("provides", ())])),
        versions=versions,
        install=_commands(install["command"], f"{where}: [install] command"),
        user=user,
        verify=_commands(data["verify"]["command"], f"{where}: [verify] command") if "verify" in data else (),
        env=_string_table(data.get("env", {}), f"{where}: [env]"),
        args=_string_table(data.get("args", {}), f"{where}: [args]"),
        caches=tuple(caches),
        mounts=mounts,
        assets=assets if assets.is_dir() else None,
        source=source,
    )


def feature_dirs() -> list[tuple[Path, str]]:
    """Where features are looked for, later entries override earlier ones by name."""
    from ccd.config import GLOBAL_CONFIG_PATH

    return [(BUNDLED_DIR, "bundled"), (GLOBAL_CONFIG_PATH.parent / "features", "global"), (Path(".ccd") / "features", "local")]


def load_features(dirs: Iterable[tuple[Path, str]] | None = None) -> dict[str, Feature]:
    """Load every feature under the given directories (default: feature_dirs()), validated as a whole."""
    found: dict[str, Feature] = {}
    for root, source in dirs if dirs is not None else feature_dirs():
        if not root.is_dir():
            continue
        for directory in sorted(p for p in root.iterdir() if (p / MANIFEST_NAME).is_file()):
            feature = parse_feature(_load_toml(directory / MANIFEST_NAME), directory, source)
            if feature.name in found:
                logger.debug("Feature %s from %s overrides the one from %s", feature.name, source, found[feature.name].source)
            found[feature.name] = feature
    return _link(found)


def _link(features: dict[str, Feature]) -> dict[str, Feature]:
    """Check cross-feature rules and turn required capabilities into feature names."""
    provider: dict[str, str] = {}
    versions: dict[str, str] = {}
    for feature in features.values():
        for capability in feature.provides:
            if capability in provider:
                raise FeatureError(f"'{capability}' is provided by both '{provider[capability]}' and '{feature.name}'")
            provider[capability] = feature.name
        for key in feature.versions:
            if key in versions:
                raise FeatureError(f"version '{key}' is declared by both '{versions[key]}' and '{feature.name}'")
            versions[key] = feature.name

    for feature in features.values():
        missing = [r for r in feature.requires if r not in provider]
        if missing:
            raise FeatureError(f"feature '{feature.name}' requires unknown '{missing[0]}'")
        feature.requires = tuple(dict.fromkeys(provider[r] for r in feature.requires))

    _check_acyclic(features)
    return features


def _check_acyclic(features: dict[str, Feature]) -> None:
    state: dict[str, int] = {}

    def visit(name: str, path: list[str]) -> None:
        if state.get(name) == 2:
            return
        if state.get(name) == 1:
            raise FeatureError("dependency cycle: " + " -> ".join([*path[path.index(name):], name]))
        state[name] = 1
        for dependency in features[name].requires:
            visit(dependency, [*path, name])
        state[name] = 2

    for name in features:
        visit(name, [])


def select(features: dict[str, Feature], with_names: set[str] | None = None, without_names: set[str] | None = None) -> list[Feature]:
    """The features to install, in install order.

    With with_names exactly those are requested, otherwise every default feature minus without_names. Dependencies are
    always added. Excluding a feature that a requested one needs is an error.
    """
    for names in (with_names, without_names):
        unknown = sorted((names or set()) - set(features))
        if unknown:
            raise FeatureError(f"unknown feature(s): {', '.join(unknown)} (available: {', '.join(sorted(features))})")

    if with_names is not None:
        requested = set(with_names)
    else:
        requested = {n for n, f in features.items() if f.default} - (without_names or set())

    needed: dict[str, str] = {}  # dependency -> a feature that needs it
    pending = sorted(requested)
    while pending:
        name = pending.pop()
        for dependency in features[name].requires:
            if dependency not in needed:
                needed[dependency] = name
                pending.append(dependency)

    for name in sorted(without_names or ()):
        if name in needed:
            raise FeatureError(f"cannot exclude '{name}': required by '{needed[name]}'")

    return order(requested | set(needed), features)


def order(names: set[str], features: dict[str, Feature]) -> list[Feature]:
    """Dependencies first; among features that are ready, lower order then name goes first, so output is stable."""
    remaining = set(names)
    done: set[str] = set()
    result: list[Feature] = []
    while remaining:
        ready = [features[n] for n in remaining if all(r in done for r in features[n].requires)]
        nxt = min(ready, key=lambda f: (f.order, f.name))
        result.append(nxt)
        done.add(nxt.name)
        remaining.remove(nxt.name)
    return result
