"""One build stage per feature that lists paths, copied together in the final stage; the others before or after."""

from dataclasses import dataclass
from typing import Sequence

from ccd.features import Feature, dependencies
from ccd.features.render.base import RendererError
from ccd.features.render.dialect import environment, expand_home, heading, install_lines
from ccd.features.render.dockerfile import DockerfileRenderer, Plan, Stage


@dataclass
class Placement:
    """Where each selected feature is installed, decided from its paths and what it requires."""

    pre: list[Feature]  # no paths, and nothing it requires has any: installed first, in order
    staged: list[Feature]  # has paths: a stage of its own
    post: list[Feature]  # no paths, but it requires something that has: installed last, on top of the assembled image


def place(selected: Sequence[Feature]) -> Placement:
    """Sort the selection into the three places. Raises RendererError for a layout that cannot be built.

    A feature with paths is built before the image is assembled, so it cannot require one that is installed after.
    """
    staged = [f for f in selected if f.paths]
    post = [f for f in selected if not f.paths and any(d.paths for d in dependencies(f, selected))]
    late = {f.name for f in post}
    for feature in staged:
        for dependency in dependencies(feature, selected):
            if dependency.name in late:
                raise RendererError(
                    f"feature '{feature.name}' has paths, so it is built before the image is assembled, but it requires '{dependency.name}', "
                    "which has no paths and needs features that have them, so it is installed after: give it paths too"
                )
    return Placement(pre=[f for f in selected if not f.paths and f.name not in late], staged=staged, post=post)


def stage_name(feature: Feature) -> str:
    return f"f-{feature.name}"


def copies(features: Sequence[Feature], selected: Sequence[Feature]) -> list[str]:
    """COPY lines bringing the paths of the given features (in install order) over from their stages.

    A path is skipped when a later feature that builds on this one lists it too: that stage holds this feature's files
    plus its own changes, so one copy from there is enough. Unrelated features that list the same path are all copied.
    """
    lines = []
    for index, feature in enumerate(features):
        for path in feature.paths:
            if any(path in later.paths and feature.name in {d.name for d in dependencies(later, selected)} for later in features[index + 1 :]):
                continue
            target = expand_home(path)
            lines.append(f"COPY --chown=user:user --from={stage_name(feature)} {target} {target}")
    return lines


def stage(feature: Feature, selected: Sequence[Feature]) -> Stage:
    """The stage of a feature that has paths: built on the stage of the one feature it needs, or on `pre` plus copies."""
    staged = [d for d in dependencies(feature, selected) if d.paths]
    direct = [d for d in staged if d.name in feature.requires]
    # Of the direct requirements, only those no other direct requirement already builds on
    leaves = [d for d in direct if not any(d.name in {x.name for x in dependencies(other, selected)} for other in direct if other is not d)]
    lines = [heading(feature)]
    declared: set[str] = set()
    parent = stage_name(leaves[0]) if len(leaves) == 1 else "pre"
    lines.append(f"FROM {parent} AS {stage_name(feature)}")
    if parent == "pre":
        lines.extend(environment(staged, declared))
        lines.extend(copies(staged, selected))
    lines.extend(install_lines(feature, dependencies(feature, selected), declared))
    return Stage(feature=feature.name, name=stage_name(feature), parent=parent, text="\n".join(lines))


class ParallelRenderer(DockerfileRenderer):
    """Features with paths get a stage of their own, so independent ones build at the same time and an unchanged one keeps
    its cached install. Features without paths install in `pre`, or in `post` when they need a feature that has paths.
    """

    name = "parallel"
    description = "one build stage per feature, independent ones built at the same time, copied together at the end"

    def plan(self, selected: Sequence[Feature]) -> Plan:
        placement = place(selected)
        return Plan(
            pre=placement.pre,
            stages=[stage(f, selected) for f in placement.staged],
            assembly=[*environment(placement.staged, set()), *copies(placement.staged, selected)],
            post=placement.post,
        )
