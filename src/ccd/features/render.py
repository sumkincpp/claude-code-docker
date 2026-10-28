"""Render a selection of features as a Dockerfile plus the build context it needs."""

import shutil
from pathlib import Path
from typing import Sequence

from ccd.config import IMAGE_HOME_FOLDER
from ccd.features import Cache, Feature, version_variable

CORE_DIR = Path(__file__).parent / "core"
FEATURES_IN_IMAGE = "/usr/local/share/ccd/features"
IMAGE_USER_ID = 1000


def _expand(path: str) -> str:
    return IMAGE_HOME_FOLDER + path[1:] if path == "~" or path.startswith("~/") else path


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _dependencies(feature: Feature, selected: Sequence[Feature]) -> list[Feature]:
    """Everything the feature requires, directly or not, in install order."""
    by_name = {f.name: f for f in selected}
    seen: set[str] = set()
    pending = list(feature.requires)
    while pending:
        name = pending.pop()
        if name not in seen:
            seen.add(name)
            pending.extend(by_name[name].requires)
    return [f for f in selected if f.name in seen]


def _cache_mounts(caches: Sequence[Cache], user: str) -> list[str]:
    uid = 0 if user == "root" else IMAGE_USER_ID
    mounts: dict[str, str] = {}
    for cache in caches:
        mounts.setdefault(cache.id, f"--mount=type=cache,id={cache.id},uid={uid},gid={uid},target={_expand(cache.target)}")
    return list(mounts.values())


def _run(commands: Sequence[str], mounts: Sequence[str] = ()) -> str:
    """One RUN chaining the commands with &&, so the first failure stops the build step (as the hand-written Dockerfile did)."""
    prefix = "RUN " + "".join(f"{mount} " for mount in mounts)
    body = " && \\\n    ".join(commands)
    return f"{prefix}\\\n    {body}" if mounts else f"RUN {body}"


def render_feature(feature: Feature, selected: Sequence[Feature], declared: set[str]) -> str:
    dependencies = _dependencies(feature, selected)
    lines = [f"# feature: {feature.name}" + (f" - {feature.description}" if feature.description else "")]

    # Args and versions are declared right before the RUN that uses them, so changing one only rebuilds from here on.
    exported = []
    for name, default in feature.args.items():
        if name not in declared:
            lines.append(f"ARG {name}={default}")
            declared.add(name)
        exported.append(name)
    for key, default in feature.versions.items():
        lines.append(f"ARG {version_variable(key)}={default}")
        exported.append(version_variable(key))
    for dependency in dependencies:
        exported.extend(a for a in dependency.args if a not in exported)

    lines.extend(f"ENV {key}={_quote(value)}" for key, value in feature.env.items())

    commands = []
    if feature.assets is not None:
        directory = f"{FEATURES_IN_IMAGE}/{feature.name}"
        lines.append(f"COPY --chown=user:user features/{feature.name}/ {directory}/")
        commands.append(f"export CCD_FEATURE_DIR={_quote(directory)}")
    # Referencing every arg in the RUN is what makes BuildKit rebuild the layer when one changes.
    commands.extend(f'export {name}="${{{name}}}"' for name in exported)
    commands.extend(feature.install)

    caches = [c for dependency in dependencies for c in dependency.caches] + list(feature.caches)
    if feature.user == "root":
        lines.append("USER root")
    lines.append(_run(commands, _cache_mounts(caches, feature.user)))
    if feature.user == "root":
        lines.append("USER user")
    return "\n".join(lines)


def render_dockerfile(selected: Sequence[Feature]) -> str:
    """Core head, one block per feature in the given (install) order, a final check of everything, core tail."""
    declared: set[str] = set()
    parts = [(CORE_DIR / "head.Dockerfile").read_text().rstrip()]
    parts.extend(render_feature(feature, selected, declared) for feature in selected)
    verify = [command for feature in selected for command in feature.verify]
    if verify:
        parts.append("# Verify installations\n" + _run(verify))
    parts.append((CORE_DIR / "tail.Dockerfile").read_text().rstrip())
    return "\n\n".join(parts) + "\n"


def stage_context(selected: Sequence[Feature], destination: Path) -> Path:
    """Write the Dockerfile and everything it COPYs into destination, and return the Dockerfile path."""
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "core").mkdir(exist_ok=True)
    shutil.copy2(CORE_DIR / "ccd-entrypoint", destination / "core" / "ccd-entrypoint")
    for feature in selected:
        if feature.assets is not None:
            shutil.copytree(feature.assets, destination / "features" / feature.name, dirs_exist_ok=True)
    dockerfile = destination / "Dockerfile"
    dockerfile.write_text(render_dockerfile(selected))
    return dockerfile
