"""Backend plugin contract and discovery.

The ccd core only talks to backends through the types in this module. Backends are
discovered through the ``ccd.backends`` entry-point group, so third-party packages
can add runtimes without touching the core.
"""

import argparse
import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from ccd.config import DEFAULT_TAG, BuildConfig, image_ref

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
    dockerfile: Path | None = None  # generated from the selected features, set when there is something to build
    context: Path | None = None
    tag: str = DEFAULT_TAG


@dataclass
class RunRequest:
    image_name: str
    name: str
    memory: str
    cpus: str
    root: bool
    mounts: list[ResolvedMount]
    options: dict[str, Any] = field(default_factory=dict)
    command: list[str] = field(default_factory=list)  # one-off command to run instead of an interactive shell
    tag: str = DEFAULT_TAG


@dataclass
class AttachRequest:
    name: str
    root: bool
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImageInfo:
    """One row of `ccd images`: a piece of build data a backend holds (an image, a rootfs, a kernel).

    Backends fill in plain values (bytes, a datetime); `ccd images` formats them the same way for every backend.
    """

    backend: str
    kind: str
    name: str
    id: str = ""
    size: int | None = None  # bytes
    created: datetime | None = None


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1000
    return ""


AGO_LIMIT = timedelta(days=7)  # older than this, `ccd images` shows the date instead of "N days ago"


def utcnow() -> datetime:
    """The current time; the one place ages read the clock, so tests can replace it."""
    return datetime.now(timezone.utc)


def _aware(moment: datetime) -> datetime:
    """A time without a zone is local time; ages are computed on zone-aware times so DST changes cannot shift them."""
    return moment if moment.tzinfo else moment.astimezone()


def format_age(created: datetime, now: datetime | None = None) -> str:
    """'just now', '5 minutes ago', '3 hours ago', '2 days ago' up to AGO_LIMIT, then the local date ('2026-10-18')."""
    created = _aware(created)
    age = _aware(now or utcnow()) - created
    if age >= AGO_LIMIT:
        return created.astimezone().strftime("%Y-%m-%d")
    seconds = max(int(age.total_seconds()), 0)
    for unit, length in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= length:
            count = seconds // length
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "just now"


def short_id(image_id: str) -> str:
    return image_id.removeprefix("sha256:")[:12]


def parse_size(text: str) -> int | None:
    """'3.82 GB' / '800MB' (decimal units, as docker and buildah print them) to bytes."""
    match = re.fullmatch(r"\s*([\d.]+)\s*([kKMGT]?)B?\s*", text)
    if not match:
        return None
    return int(float(match.group(1)) * 1000 ** ("", "k", "M", "G", "T").index(match.group(2).replace("K", "k")))


def parse_time(text: str) -> datetime | None:
    """RFC 3339 ('2026-10-25T19:50:12.123456789Z') or buildah's 'Oct 25, 2026 19:50' (local time) to a zone-aware datetime."""
    text = text.strip()
    try:
        return datetime.strptime(text, "%b %d, %Y %H:%M").astimezone()
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", text.replace("Z", "+00:00")))
    except ValueError:
        return None
    return _aware(parsed)


class Backend(ABC):
    """A runtime that can build, run and attach to ccd environments."""

    name: str
    can_build = True
    can_run = True
    supports_tags = True  # False for backends that keep a single image

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Hook for contributing backend-specific CLI flags to build/run/attach."""

    def check_available(self) -> None:
        """Raise BackendError if the runtime is unusable on this host."""

    @abstractmethod
    def build(self, request: BuildRequest) -> None: ...

    @abstractmethod
    def run(self, request: RunRequest) -> int | None:
        """Run the environment until the shell or command ends; return its exit code (None counts as 0)."""

    @abstractmethod
    def attach(self, request: AttachRequest) -> None: ...

    @abstractmethod
    def list_running(self) -> list[str]:
        """Names of running environments, unfiltered."""

    def prune(self, options: dict[str, Any], dry_run: bool) -> list[str]:
        """Remove leftovers of dead runs (or only list them when dry_run); returns what was or would be done."""
        return []

    def images(self, options: dict[str, Any]) -> list[ImageInfo]:
        """Build data this backend holds (images, rootfs, kernel) for `ccd images`."""
        return []

    def built_image_id(self, request: BuildRequest) -> str:
        """Builder side: id of the image the last build produced, or "" when there is none."""
        return ""

    def tag_image(self, name: str, source: str, target: str, options: dict[str, Any]) -> None:
        """Builder side: give the image tagged `source` the additional tag `target`, replacing it if it exists."""
        raise BackendError(f"Backend '{self.name}' cannot tag images")

    def read_image_file(self, name: str, tag: str, path: str, options: dict[str, Any]) -> str:
        """Builder side: contents of the file `path` in the image tagged `tag`, without starting its entrypoint."""
        raise BackendError(f"Backend '{self.name}' cannot read files from images")

    def describe_image(self, built_id: str, options: dict[str, Any]) -> str:
        """Runner side: how its prepared image relates to the builder's current image `built_id` (for `ccd status`)."""
        return ""

    def image_formats(self) -> list[str]:
        """Builder side: formats this backend's builds can be provided in, cheapest first (e.g. ["docker-daemon"])."""
        return []

    def provide_image(self, fmt: str, request: BuildRequest) -> AbstractContextManager[str | Path]:
        """Builder side: make the built image available in `fmt` and yield where it is (an image name or a path).
        Whatever was set up is released when the block exits. In-place formats need no work: the image is addressed by name."""
        return nullcontext(image_ref(request.image_name, request.tag))

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
    "buildah": "ccd.backends.buildah:BuildahBackend",
    "podman": "ccd.backends.podman:PodmanBackend",
    "firecracker": "ccd.backends.firecracker:FirecrackerBackend",
    "gvisor-docker": "ccd.backends.gvisor:GVisorDockerBackend",
    "gvisor-podman": "ccd.backends.gvisor:GVisorPodmanBackend",
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
