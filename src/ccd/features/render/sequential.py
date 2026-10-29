"""Every feature installed in order in a single stage."""

from typing import Sequence

from ccd.features import Feature
from ccd.features.render.dockerfile import DockerfileRenderer, Plan


class SequentialRenderer(DockerfileRenderer):
    """Nothing is copied and nothing runs at the same time: slower on a cold build, but no copy cost on slow storage.

    The paths of the features are ignored.
    """

    name = "sequential"
    description = "every feature in order in a single stage"

    def plan(self, selected: Sequence[Feature]) -> Plan:
        return Plan(pre=list(selected))
