"""End-to-end harness: drives the real `ccd` CLI (ccd.ccd.main) against recording fake backends."""

import sys
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

    def run(self, request):
        CALLS.append((self.name, "run", request))

    def attach(self, request):
        CALLS.append((self.name, "attach", request))

    def list_running(self):
        return list(self.running)

    def image_formats(self):
        return {"fake-native"}

    def import_image(self, source, request):
        CALLS.append((self.name, "import", source))


class FakeBuilder(RecordingBackend):
    name = "fake-build"


class FakeRunner(RecordingBackend):
    name = "fake-run"


@pytest.fixture
def ccd(tmp_path, monkeypatch):
    """Returns `invoke(*args, toml="")`: runs the CLI in an empty project dir and returns (exit_code, recorded calls)."""
    CALLS.clear()
    RecordingBackend.running = []
    monkeypatch.chdir(tmp_path)
    for name, cls in (("fake", RecordingBackend), ("fake-build", FakeBuilder), ("fake-run", FakeRunner)):
        monkeypatch.setitem(plugin.BUILTIN_BACKENDS, name, f"{__name__}:{cls.__name__}")
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
