"""The skeleton every Dockerfile renderer shares; a subclass only decides how the features are laid out in stages."""

import shutil
from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from ccd.features import Feature, dependencies
from ccd.features.render.base import Renderer
from ccd.features.render.dialect import INSTALLED_FILE, heading, install_lines, record, run

CORE_DIR = Path(__file__).parent.parent / "core"


@dataclass
class Plan:
    """How a renderer lays the selection out."""

    pre: list[Feature]  # installed one after the other in the `pre` stage
    stages: list[str] = field(default_factory=list)  # further stages, already rendered
    assembly: list[str] = field(default_factory=list)  # what `final` does before the checks, for example copying from the stages


class DockerfileRenderer(Renderer):
    """Core head, `pre`, the planned stages, `final`, core tail.

    The final stage also runs the check of everything and records what is installed, so whatever a layout loses on the
    way into the image shows up as a failing check.
    """

    @abstractmethod
    def plan(self, selected: Sequence[Feature]) -> Plan: ...

    def render(self, selected: Sequence[Feature]) -> str:
        plan = self.plan(selected)
        declared: set[str] = set()
        parts = [(CORE_DIR / "head.Dockerfile").read_text().rstrip(), "FROM base AS pre"]
        for feature in plan.pre:
            parts.append("\n".join([heading(feature), *install_lines(feature, dependencies(feature, selected), declared)]))
        parts.extend(plan.stages)
        parts.append("\n".join(["# Assemble the features", "FROM pre AS final", *plan.assembly]))
        verify = [command for feature in selected for command in feature.verify]
        if verify:
            parts.append("# Verify installations\n" + run(verify))
        if selected:
            parts.append("# Record what is installed\n" + run([f": > {INSTALLED_FILE}", *(record(f) for f in selected)]))
        parts.append((CORE_DIR / "tail.Dockerfile").read_text().rstrip())
        return "\n\n".join(parts) + "\n"

    def stage_context(self, selected: Sequence[Feature], destination: Path) -> Path:
        """Write the Dockerfile and everything it COPYs into destination, and return the Dockerfile path."""
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "core").mkdir(exist_ok=True)
        shutil.copy2(CORE_DIR / "ccd-entrypoint", destination / "core" / "ccd-entrypoint")
        for feature in selected:
            if feature.assets is not None:
                shutil.copytree(feature.assets, destination / "features" / feature.name, dirs_exist_ok=True)
        dockerfile = destination / "Dockerfile"
        dockerfile.write_text(self.render(selected))
        return dockerfile
