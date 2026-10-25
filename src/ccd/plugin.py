"""Backend plugin contract and discovery.

The ccd core only talks to backends through the types in this module. Backends are
discovered through the ``ccd.backends`` entry-point group, so third-party packages
can add runtimes without touching the core.
"""

import argparse
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from ccd.config import BuildConfig

ENTRY_POINT_GROUP = "ccd.backends"

logger = logging.getLogger(__name__)


@contextmanager
def timed(label: str, log: logging.Logger = logger) -> Iterator[None]:
    """Log how long the wrapped block took (visible with -v)."""
    start = time.monotonic()
    try:
        yield
    finally:
        log.info("%s took %.1fs", label, time.monotonic() - start)


class BackendError(Exception):
    """Raised by backends for expected failures; the core prints it and exits non-zero."""


@dataclass
class ResolvedMount:
    host: Path
    container: Path
    readonly: bool = False


@dataclass
class BuildRequest:
    image_name: str
    config: BuildConfig
    extra_args: list[str] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunRequest:
    image_name: str
    name: str
    memory: str
    cpus: str
    root: bool
    mounts: list[ResolvedMount]
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class AttachRequest:
    name: str
    root: bool
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImageSource:
    """An image handed from the build backend to a different run backend.

    `formats` lists the native forms the image is already available in (e.g. "docker-daemon").
    """

    name: str
    formats: set[str]


class Backend(ABC):
    """A runtime that can build, run and attach to ccd environments."""

    name: str

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Hook for contributing backend-specific CLI flags to build/run/attach."""

    def check_available(self) -> None:
        """Raise BackendError if the runtime is unusable on this host."""

    @abstractmethod
    def build(self, request: BuildRequest) -> None: ...

    @abstractmethod
    def run(self, request: RunRequest) -> None: ...

    @abstractmethod
    def attach(self, request: AttachRequest) -> None: ...

    @abstractmethod
    def list_running(self) -> list[str]:
        """Names of running environments, unfiltered."""

    def image_formats(self) -> set[str]:
        """Native image forms this backend's builds are available in without exporting (e.g. {"docker-daemon"})."""
        return set()

    def import_image(self, source: ImageSource, request: BuildRequest) -> None:
        """Make an image built by another backend runnable by this backend (according to source.formats)."""
        raise BackendError(f"Backend '{self.name}' cannot import images")


@dataclass
class _BuiltinBackend:
    """Stand-in for an entry point so shipped backends work even when the installed
    package metadata predates them (e.g. a stale `uv tool install`)."""

    name: str
    value: str

    def load(self) -> Any:
        module, _, attr = self.value.partition(":")
        return getattr(import_module(module), attr)


BUILTIN_BACKENDS = {
    "docker": "ccd.backends.docker:DockerBackend",
    "firecracker": "ccd.backends.firecracker:FirecrackerBackend",
}


def available_backends() -> dict[str, Any]:
    """Map backend name -> entry point (not yet loaded). Installed entry points win over built-ins."""
    backends: dict[str, Any] = {name: _BuiltinBackend(name, value) for name, value in BUILTIN_BACKENDS.items()}
    backends.update({ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)})
    return backends


def get_backend(name: str) -> Backend:
    backends = available_backends()
    if name not in backends:
        installed = ", ".join(sorted(backends)) or "none"
        raise BackendError(f"Unknown backend '{name}'. Installed backends: {installed}")
    logger.debug("Loading backend plugin: %s", name)
    backend = backends[name].load()()
    backend.check_available()
    return backend
