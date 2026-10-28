"""The contract of a renderer: what the kernel asks of anything that turns the selected features into build input."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Sequence

from ccd.features import Feature


class RendererError(ValueError):
    """An unknown renderer was asked for."""


class Renderer(ABC):
    """Turns the selected features (in install order) into something a builder can build.

    The manifests do not depend on a renderer, and a renderer does not know about backends: it gets a selection and
    produces text and files.
    """

    name: str
    description: str

    @abstractmethod
    def render(self, selected: Sequence[Feature]) -> str:
        """The build file for the selection, for example a Dockerfile."""

    @abstractmethod
    def stage_context(self, selected: Sequence[Feature], destination: Path) -> Path:
        """Write the build file and everything it needs into destination, and return the path of the build file."""
