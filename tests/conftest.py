"""End-to-end harness: drives the real `ccd` CLI (ccd.ccd.main) against recording fake backends."""

import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ccd import ccd as cli
from ccd import plugin
from ccd.config import RunConfig

CALLS: list[tuple] = []


class RecordingBackend(plugin.Backend):
    name = "fake"
    running: list[str] = []

    def build(self, request):
        CALLS.append((self.name, "build", request))

    run_code = 0  # exit code the fake "environment" ends with

    def run(self, request):
        CALLS.append((self.name, "run", request))
        return self.run_code

    def attach(self, request):
        CALLS.append((self.name, "attach", request))

    def list_running(self):
        return list(self.running)

    offers = ["fake-native"]  # builder side
    accepts = ["fake-native"]  # runner side
    import_fails = False

    def image_formats(self):
        return list(self.offers)

    def accepted_formats(self):
        return list(self.accepts)

    @contextmanager
    def provide_image(self, fmt, request):
        CALLS.append((self.name, "provide", fmt))
        try:
            yield request.image_name
        finally:
            CALLS.append((self.name, "release", fmt))

    def import_image(self, fmt, location, request):
        CALLS.append((self.name, "import", (fmt, location)))
        if self.import_fails:
            raise plugin.BackendError("import failed")

    built_id = "id-1"
    verdict = ""

    def built_image_id(self, request):
        return self.built_id

    def images(self, options):
        return [plugin.ImageInfo(self.name, "image", f"{self.name}/ccd", self.built_id, 1_000_000, datetime(2026, 10, 25, 20, 55, tzinfo=timezone.utc))]

    def describe_image(self, built_id, options):
        return self.verdict

    def prune(self, options, dry_run):
        CALLS.append((self.name, "prune", dry_run))
        return ["removed leftover"]


class FakeBuilder(RecordingBackend):
    name = "fake-build"


class FakeRunner(RecordingBackend):
    name = "fake-run"


class FakeDockerLike(RecordingBackend):
    name = "fake-docker"

    offers = ["docker-daemon"]
    built_id = "sha256:abc"


@pytest.fixture
def ccd(tmp_path, monkeypatch):
    """Returns `invoke(*args, toml="")`: runs the CLI in an empty project dir and returns (exit_code, recorded calls)."""
    CALLS.clear()
    RecordingBackend.running = []
    monkeypatch.chdir(tmp_path)
    for name, cls in (("fake", RecordingBackend), ("fake-build", FakeBuilder), ("fake-run", FakeRunner), ("fake-docker", FakeDockerLike)):
        monkeypatch.setitem(plugin.BUILTIN_BACKENDS, name, f"{__name__}:{cls.__name__}")
    # The shipped backends must not touch the developer's docker, buildah or KVM; tests that need one re-enable it
    def unavailable(self):
        raise plugin.BackendError("disabled in tests")

    for real in ("docker.DockerBackend", "buildah.BuildahBackend", "firecracker.FirecrackerBackend", "podman.PodmanBackend", "gvisor.GVisorDockerBackend", "gvisor.GVisorPodmanBackend"):
        monkeypatch.setattr(f"ccd.backends.{real}.check_available", unavailable)
    # Keep the developer's real ~/.config/ccd/ccd.toml out of the tests
    original = RunConfig.load_effective.__func__
    monkeypatch.setattr(RunConfig, "load_effective", classmethod(lambda cls, local, global_path=tmp_path / "no-global.toml": original(cls, local, global_path)))

    def invoke(*args: str, toml: str = "") -> tuple[int, list[tuple]]:
        Path("ccd.toml").write_text(toml)
        monkeypatch.setattr(sys, "argv", ["ccd", *args])
        code = 0
        try:
            cli.main()
        except SystemExit as e:
            code = e.code or 0
        return code, list(CALLS)

    return invoke
