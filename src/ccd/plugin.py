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
from contextlib import AbstractContextManager, contextmanager, nullcontext
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

    def prune(self, options: dict[str, Any], dry_run: bool) -> list[str]:
        """Remove leftovers of dead runs (or only list them when dry_run); returns what was or would be done."""
        return []

    def image_formats(self) -> list[str]:
        """Builder side: formats this backend's builds can be provided in, cheapest first (e.g. ["docker-daemon"])."""
        return []

    def provide_image(self, fmt: str, request: BuildRequest) -> AbstractContextManager[str | Path]:
        """Builder side: make the built image available in `fmt` and yield where it is (an image name or a path).
        Whatever was set up is released when the block exits. In-place formats need no work: the image is addressed by name."""
        return nullcontext(request.image_name)

    def accepted_formats(self) -> list[str]:
        """Runner side: formats this backend can import, best first."""
        return []

    def import_image(self, fmt: str, location: str | Path, request: BuildRequest) -> None:
        """Runner side: make an image built by another backend, available as `fmt` at `location`, runnable by this backend."""
        raise BackendError(f"Backend '{self.name}' cannot import images")


def hand_over(builder: Backend, runner: Backend, builder_request: BuildRequest, runner_request: BuildRequest) -> str:
    """Hand an image from its builder to a different run backend in the first format the runner accepts and the builder offers."""
    offered, accepted = builder.image_formats(), runner.accepted_formats()
    fmt = next((f for f in accepted if f in offered), None)
    if fmt is None:
        raise BackendError(
            f"Backend '{runner.name}' cannot run images built by '{builder.name}' "
            f"({builder.name} offers: {', '.join(offered) or 'nothing'}; {runner.name} accepts: {', '.join(accepted) or 'nothing'})"
        )
    with builder.provide_image(fmt, builder_request) as location:
        runner.import_image(fmt, location, runner_request)
    return fmt


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
