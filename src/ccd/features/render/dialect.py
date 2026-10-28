"""The Dockerfile dialect: the instructions that install a feature, as pure functions.

They say how a feature is written down. Where it goes (which stage, copied from where) is for a renderer to decide.
"""

import json
import shlex
from typing import Sequence

from ccd.config import IMAGE_HOME_FOLDER
from ccd.features import Cache, Feature, version_variable

FEATURES_IN_IMAGE = "/usr/local/share/ccd/features"
IMAGE_USER_ID = 1000
INSTALLED_FILE = "/metadata/installed.jsonl"


def expand_home(path: str) -> str:
    return IMAGE_HOME_FOLDER + path[1:] if path == "~" or path.startswith("~/") else path


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def cache_mounts(caches: Sequence[Cache], user: str) -> list[str]:
    uid = 0 if user == "root" else IMAGE_USER_ID
    mounts: dict[str, str] = {}
    for cache in caches:
        mounts.setdefault(cache.id, f"--mount=type=cache,id={cache.id},uid={uid},gid={uid},target={expand_home(cache.target)}")
    return list(mounts.values())


def run(commands: Sequence[str], mounts: Sequence[str] = ()) -> str:
    """One RUN chaining the commands with &&, so the first failure stops the build step (as the hand-written Dockerfile did)."""
    prefix = "RUN " + "".join(f"{mount} " for mount in mounts)
    body = " && \\\n    ".join(commands)
    return f"{prefix}\\\n    {body}" if mounts else f"RUN {body}"


def heading(feature: Feature) -> str:
    return f"# feature: {feature.name}" + (f" - {feature.description}" if feature.description else "")


def declare(feature: Feature, declared: set[str], versions: bool = True) -> list[str]:
    """ARG lines (with defaults) for the feature's args and versions that this stage has not declared yet."""
    lines = []
    wanted = list(feature.args.items()) + ([(version_variable(key), default) for key, default in feature.versions.items()] if versions else [])
    for name, default in wanted:
        if name not in declared:
            lines.append(f"ARG {name}={default}")
            declared.add(name)
    return lines


def environment(features: Sequence[Feature], declared: set[str]) -> list[str]:
    """The ARGs and ENVs of the given features, for a stage that does not inherit them."""
    lines = []
    for feature in features:
        lines.extend(declare(feature, declared))
        lines.extend(f"ENV {key}={quote(value)}" for key, value in feature.env.items())
    return lines


def install_lines(feature: Feature, dependencies: Sequence[Feature], declared: set[str]) -> list[str]:
    """ARGs, ENVs, assets and the RUN installing one feature; `declared` holds the ARGs the stage already has."""
    # Args and versions are declared right before the RUN that uses them, so changing one only rebuilds from here on.
    lines = declare(feature, declared)
    exported = [*feature.args, *(version_variable(key) for key in feature.versions)]
    # ARGs do not carry over from another stage, so the args of what this feature builds on are declared where they are used
    for dependency in dependencies:
        lines.extend(declare(dependency, declared, versions=False))
        exported.extend(a for a in dependency.args if a not in exported)

    lines.extend(f"ENV {key}={quote(value)}" for key, value in feature.env.items())

    commands = []
    if feature.assets is not None:
        directory = f"{FEATURES_IN_IMAGE}/{feature.name}"
        lines.append(f"COPY --chown=user:user features/{feature.name}/ {directory}/")
        commands.append(f"export CCD_FEATURE_DIR={quote(directory)}")
    # Referencing every arg in the RUN is what makes BuildKit rebuild the layer when one changes.
    commands.extend(f'export {name}="${{{name}}}"' for name in exported)
    commands.extend(feature.install)

    caches = [c for dependency in dependencies for c in dependency.caches] + list(feature.caches)
    if feature.user == "root":
        lines.append("USER root")
    lines.append(run(commands, cache_mounts(caches, feature.user)))
    if feature.user == "root":
        lines.append("USER user")
    return lines


def record(feature: Feature) -> str:
    """Shell command appending one JSON line about the feature to the installed list.

    What the manifest says is written as is; the first line of the check output (stdout, so warnings do not end up as the version) is only known at build time, so the
    shell fills it in. It is the resolved version, for example "go version go1.24.0 linux/arm64".
    """
    known = {"name": feature.name, "description": feature.description, "source": feature.source, "install": list(feature.install)}
    head = json.dumps(known)[:-1] + ',"resolved":"'
    escape = r"sed 's/[\\\"]/\\&/g'"
    resolved = f"$({feature.verify[0]} 2>/dev/null | head -n1 | {escape})" if feature.verify else ""
    return f"printf '%s%s\"}}\\n' {shlex.quote(head)} \"{resolved}\" >> {INSTALLED_FILE}"
