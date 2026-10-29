"""The contract of a renderer: what the kernel asks of anything that turns the selected features into build input."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ccd.features import Feature


class RendererError(ValueError):
    """An unknown renderer was asked for."""


@dataclass(frozen=True)
class RenderOptions:
    """What the user asked for independently of the layout a renderer chooses."""

    flatten: bool = False  # the image ends up as a single layer


class Renderer(ABC):
    """Turns the selected features (in install order) into something a builder can build.

    The manifests do not depend on a renderer, and a renderer does not know about backends: it gets a selection and
    produces text and files.
    """

    name: str
    description: str

    def __init__(self, options: RenderOptions | None = None) -> None:
        self.options = options or RenderOptions()

    @abstractmethod
    def render(self, selected: Sequence[Feature]) -> str:
        """The build file for the selection, for example a Dockerfile."""

    def explain(self, selected: Sequence[Feature]) -> list[tuple[str, str, str]]:
        """Where each selected feature goes, as (feature, stage, built on) rows in install order; empty when the renderer has no stages.

        Raises RendererError for a selection it cannot lay out.
        """
        return []

    @abstractmethod
    def stage_context(self, selected: Sequence[Feature], destination: Path) -> Path:
        """Write the build file and everything it needs into destination, and return the path of the build file."""
