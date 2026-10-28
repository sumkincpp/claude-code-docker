"""One build stage per feature that lists paths, copied together in the final stage."""

from typing import Sequence

from ccd.features import Feature, dependencies
from ccd.features.render.dialect import environment, expand_home, heading, install_lines
from ccd.features.render.dockerfile import DockerfileRenderer, Plan


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


def stage(feature: Feature, selected: Sequence[Feature]) -> str:
    """The stage of a feature that has paths: built on the stage of the one feature it needs, or on `pre` plus copies."""
    staged = [d for d in dependencies(feature, selected) if d.paths]
    direct = [d for d in staged if d.name in feature.requires]
    # Of the direct requirements, only those no other direct requirement already builds on
    leaves = [d for d in direct if not any(d.name in {x.name for x in dependencies(other, selected)} for other in direct if other is not d)]
    lines = [heading(feature)]
    declared: set[str] = set()
    if len(leaves) == 1:
        lines.append(f"FROM {stage_name(leaves[0])} AS {stage_name(feature)}")
    else:
        lines.append(f"FROM pre AS {stage_name(feature)}")
        lines.extend(environment(staged, declared))
        lines.extend(copies(staged, selected))
    lines.extend(install_lines(feature, dependencies(feature, selected), declared))
    return "\n".join(lines)


class ParallelRenderer(DockerfileRenderer):
    """Features with paths get a stage of their own, so independent ones build at the same time and an unchanged one keeps
    its cached install. Features without paths install first, in order, in `pre`.
    """

    name = "parallel"
    description = "one build stage per feature, independent ones built at the same time, copied together at the end"

    def plan(self, selected: Sequence[Feature]) -> Plan:
        staged = [f for f in selected if f.paths]
        return Plan(
            pre=[f for f in selected if not f.paths],
            stages=[stage(f, selected) for f in staged],
            assembly=[*environment(staged, set()), *copies(staged, selected)],
        )
