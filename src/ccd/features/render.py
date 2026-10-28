"""Render a selection of features as a Dockerfile plus the build context it needs."""

import json
import shlex
import shutil
from pathlib import Path
from typing import Sequence

from ccd.config import IMAGE_HOME_FOLDER
from ccd.features import Cache, Feature, version_variable

CORE_DIR = Path(__file__).parent / "core"
FEATURES_IN_IMAGE = "/usr/local/share/ccd/features"
IMAGE_USER_ID = 1000
INSTALLED_FILE = "/metadata/installed.jsonl"


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


def _declare(feature: Feature, declared: set[str], versions: bool = True) -> list[str]:
    """ARG lines (with defaults) for the feature's args and versions that this stage has not declared yet."""
    lines = []
    wanted = list(feature.args.items()) + ([(version_variable(key), default) for key, default in feature.versions.items()] if versions else [])
    for name, default in wanted:
        if name not in declared:
            lines.append(f"ARG {name}={default}")
            declared.add(name)
    return lines


def render_feature(feature: Feature, selected: Sequence[Feature], declared: set[str]) -> str:
    """The lines installing one feature in the current stage; `declared` holds the ARGs the stage already has."""
    dependencies = _dependencies(feature, selected)
    lines = [f"# feature: {feature.name}" + (f" - {feature.description}" if feature.description else "")]
    lines.extend(_feature_lines(feature, dependencies, declared))
    return "\n".join(lines)


def _feature_lines(feature: Feature, dependencies: Sequence[Feature], declared: set[str]) -> list[str]:
    # Args and versions are declared right before the RUN that uses them, so changing one only rebuilds from here on.
    lines = _declare(feature, declared)
    exported = [*feature.args, *(version_variable(key) for key in feature.versions)]
    # ARGs do not carry over from another stage, so the args of what this feature builds on are declared where they are used
    for dependency in dependencies:
        lines.extend(_declare(dependency, declared, versions=False))
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
    return lines


def _stage(feature: Feature) -> str:
    return f"f-{feature.name}"


def _copies(features: Sequence[Feature], selected: Sequence[Feature]) -> list[str]:
    """COPY lines bringing the paths of the given features (in install order) over from their stages.

    A path is skipped when a later feature that builds on this one lists it too: that stage holds this feature's files
    plus its own changes, so one copy from there is enough. Unrelated features that list the same path are all copied.
    """
    lines = []
    for index, feature in enumerate(features):
        for path in feature.paths:
            if any(path in later.paths and feature.name in {d.name for d in _dependencies(later, selected)} for later in features[index + 1 :]):
                continue
            target = _expand(path)
            lines.append(f"COPY --chown=user:user --from={_stage(feature)} {target} {target}")
    return lines


def _environment(features: Sequence[Feature], declared: set[str]) -> list[str]:
    """The ARGs and ENVs of the given features, for a stage that does not inherit them."""
    lines = []
    for feature in features:
        lines.extend(_declare(feature, declared))
        lines.extend(f"ENV {key}={_quote(value)}" for key, value in feature.env.items())
    return lines


def render_stage(feature: Feature, selected: Sequence[Feature]) -> str:
    """The stage of a feature that has paths: built on the stage of the one feature it needs, or on `pre` plus copies."""
    staged = [d for d in _dependencies(feature, selected) if d.paths]
    direct = [d for d in staged if d.name in feature.requires]
    # Of the direct requirements, only those no other direct requirement already builds on
    leaves = [d for d in direct if not any(d.name in {x.name for x in _dependencies(other, selected)} for other in direct if other is not d)]
    lines = [f"# feature: {feature.name}" + (f" - {feature.description}" if feature.description else "")]
    declared: set[str] = set()
    if len(leaves) == 1:
        lines.append(f"FROM {_stage(leaves[0])} AS {_stage(feature)}")
    else:
        lines.append(f"FROM pre AS {_stage(feature)}")
        lines.extend(_environment(staged, declared))
        lines.extend(_copies(staged, selected))
    lines.extend(_feature_lines(feature, _dependencies(feature, selected), declared))
    return "\n".join(lines)


def _record(feature: Feature) -> str:
    """Shell command appending one JSON line about the feature to the installed list.

    What the manifest says is written as is; the first line of the check output (stdout, so warnings do not end up as the version) is only known at build time, so the
    shell fills it in. It is the resolved version, for example "go version go1.24.0 linux/arm64".
    """
    known = {"name": feature.name, "description": feature.description, "source": feature.source, "install": list(feature.install)}
    head = json.dumps(known)[:-1] + ',"resolved":"'
    escape = r"sed 's/[\\\"]/\\&/g'"
    resolved = f"$({feature.verify[0]} 2>/dev/null | head -n1 | {escape})" if feature.verify else ""
    return f"printf '%s%s\"}}\\n' {shlex.quote(head)} \"{resolved}\" >> {INSTALLED_FILE}"


def render_dockerfile(selected: Sequence[Feature], parallel: bool = True) -> str:
    """Core head, `pre` (features without paths, in order), one stage per feature with paths, `final` assembling them, core tail.

    With parallel=False the paths are ignored: every feature installs in order in `pre`, as one stage.

    The final stage also runs the check of everything and records what is installed, so a path a feature forgot to list
    shows up as a failing check.
    """
    staged = [f for f in selected if f.paths] if parallel else []
    in_pre = [f for f in selected if f not in staged]
    declared: set[str] = set()
    parts = [(CORE_DIR / "head.Dockerfile").read_text().rstrip(), "FROM base AS pre"]
    parts.extend(render_feature(feature, selected, declared) for feature in in_pre)
    parts.extend(render_stage(feature, selected) for feature in staged)

    final = ["# Assemble the features", "FROM pre AS final"]
    final.extend(_environment(staged, set()))
    final.extend(_copies(staged, selected))
    parts.append("\n".join(final))
    verify = [command for feature in selected for command in feature.verify]
    if verify:
        parts.append("# Verify installations\n" + _run(verify))
    if selected:
        parts.append("# Record what is installed\n" + _run([f": > {INSTALLED_FILE}", *(_record(f) for f in selected)]))
    parts.append((CORE_DIR / "tail.Dockerfile").read_text().rstrip())
    return "\n\n".join(parts) + "\n"


def stage_context(selected: Sequence[Feature], destination: Path, parallel: bool = True) -> Path:
    """Write the Dockerfile and everything it COPYs into destination, and return the Dockerfile path."""
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "core").mkdir(exist_ok=True)
    shutil.copy2(CORE_DIR / "ccd-entrypoint", destination / "core" / "ccd-entrypoint")
    for feature in selected:
        if feature.assets is not None:
            shutil.copytree(feature.assets, destination / "features" / feature.name, dirs_exist_ok=True)
    dockerfile = destination / "Dockerfile"
    dockerfile.write_text(render_dockerfile(selected, parallel))
    return dockerfile
