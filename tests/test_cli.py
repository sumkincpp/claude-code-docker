"""End-to-end CLI tests: argv + ccd.toml in, backend calls out. No Docker, KVM or root needed."""

from pathlib import Path


def test_run_mounts_app_and_agent_config_and_defaults_to_user(ccd, tmp_path):
    app, home = tmp_path / "myapp", tmp_path / "home"
    app.mkdir()

    code, calls = ccd("run", str(app), "--home", str(home), "--backend", "fake")

    assert code == 0
    (_, op, request), = calls
    assert op == "run"
    assert request.root is False
    assert request.name == "ccd-myapp-01"
    assert (app.resolve(), Path("/app")) in {(m.host, m.container) for m in request.mounts}
    assert (home / ".claude").is_dir()  # prepared on the host before the run
    assert any(str(m.container) == "/home/user/.claude" for m in request.mounts)


def test_user_flag_logs_in_as_root_and_resources_pass_through(ccd, tmp_path):
    (tmp_path / "app").mkdir()

    code, calls = ccd("run", "app", "--home", str(tmp_path / "h"), "--backend", "fake", "-u", "root", "--memory", "2g", "--cpus", "4")

    request = calls[0][2]
    assert (code, request.root, request.memory, request.cpus) == (0, True, "2g", "4")


def test_run_backend_comes_from_toml_and_gets_its_option_table(ccd, tmp_path):
    (tmp_path / "app").mkdir()
    toml = '[run]\nbackend = "fake-run"\n\n[fake-run]\nvcpus = 3\n'

    _, calls = ccd("run", "app", "--home", str(tmp_path / "h"), toml=toml)

    backend, op, request = calls[0]
    assert (backend, op, request.options) == ("fake-run", "run", {"vcpus": 3})


def test_cli_backend_flag_beats_toml(ccd, tmp_path):
    (tmp_path / "app").mkdir()

    _, calls = ccd("run", "app", "--home", str(tmp_path / "h"), "--backend", "fake", toml='[run]\nbackend = "fake-run"\n')

    assert calls[0][0] == "fake"


def test_build_on_one_backend_hands_the_image_to_the_run_backend(ccd):
    toml = '[build]\nbackend = "fake-build"\n\n[run]\nbackend = "fake-run"\n'

    code, calls = ccd("build", toml=toml)

    assert code == 0
    assert [(backend, op) for backend, op, _ in calls] == [("fake-build", "build"), ("fake-run", "import")]
    assert calls[1][2].formats == {"fake-native"}  # the lazy hand-off advertises what the builder already has


def test_build_with_the_same_backend_does_not_hand_over(ccd):
    _, calls = ccd("build", toml='[run]\nbackend = "fake"\n')

    assert [(backend, op) for backend, op, _ in calls] == [("fake", "build")]


def test_build_backend_flag_overrides_only_the_build_side(ccd):
    _, calls = ccd("build", "--backend", "fake-build", toml='[run]\nbackend = "fake-run"\n')

    assert [(backend, op) for backend, op, _ in calls] == [("fake-build", "build"), ("fake-run", "import")]


def test_attach_picks_the_single_running_environment_as_root(ccd):
    from conftest import RecordingBackend

    RecordingBackend.running = ["ccd-myapp-01", "unrelated"]  # names outside ccd's prefix are ignored

    code, calls = ccd("attach", "--backend", "fake", "-u", "root")

    (_, op, request), = calls
    assert (code, op, request.name, request.root) == (0, "attach", "ccd-myapp-01", True)


def test_unknown_backend_fails_cleanly(ccd, tmp_path, capsys):
    (tmp_path / "app").mkdir()

    code, calls = ccd("run", "app", "--home", str(tmp_path / "h"), "--backend", "nope")

    assert code == 1 and calls == []
    assert "nope" in capsys.readouterr().out


def test_backends_command_lists_shipped_plugins(ccd, capsys):
    ccd("backends")

    assert {"docker", "firecracker"} <= set(capsys.readouterr().out.split())


def test_vm_addresses_avoid_networks_the_host_already_uses():
    from ccd.backends.firecracker import net

    routes = "default via 192.168.88.1 dev eth0\n10.111.111.0/29 dev br-pihole proto kernel\n10.111.111.4/30 dev ccdtap0 proto kernel\n"
    addrs = "2: eth0    inet 192.168.88.10/24 brd 192.168.88.255 scope global eth0\n"
    taken = net.parse_host_networks(routes, addrs)

    index = net.first_free_index(set(), taken)

    # the pihole's 10.111.111.0/29 covers blocks 0 and 1; our own tap's route is ignored, so block 2 is the first free one
    assert net.addresses(index) == ("10.111.111.9", "10.111.111.10")
