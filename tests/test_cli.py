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
    assert [(backend, op) for backend, op, _ in calls] == [
        ("fake-build", "build"),
        ("fake-build", "provide"),
        ("fake-run", "import"),
        ("fake-build", "release"),  # the builder keeps the image available until the runner is done
    ]
    assert calls[2][2] == ("fake-native", "ccd")


def test_build_with_the_same_backend_does_not_hand_over(ccd):
    _, calls = ccd("build", toml='[run]\nbackend = "fake"\n')

    assert [(backend, op) for backend, op, _ in calls] == [("fake", "build")]


def test_build_backend_flag_overrides_only_the_build_side(ccd):
    _, calls = ccd("build", "--backend", "fake-build", toml='[run]\nbackend = "fake-run"\n')

    assert [(backend, op) for backend, op, _ in calls if op != "release"] == [("fake-build", "build"), ("fake-build", "provide"), ("fake-run", "import")]


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


def test_prune_is_a_dry_run_unless_told_otherwise(ccd, capsys):
    code, calls = ccd("prune", "--backend", "fake")

    assert (code, calls) == (0, [("fake", "prune", True)])
    out = capsys.readouterr().out
    assert "Would: removed leftover" in out and "--no-dry-run" in out


def test_prune_no_dry_run_removes(ccd, capsys):
    _, calls = ccd("prune", "--no-dry-run", "--backend", "fake")

    assert calls == [("fake", "prune", False)]
    assert "Would" not in capsys.readouterr().out


def test_firecracker_prune_removes_only_what_belongs_to_dead_vms(ccd, tmp_path, monkeypatch, capsys):
    import json
    import os

    from ccd.backends.firecracker import prune as fc_prune

    vms = tmp_path / "fc" / "vms"
    for name, index, pid in (("ccd-app-01", 0, os.getpid()), ("ccd-app-02", 1, 2**22 + 1)):  # 01 is "running" (this process), 02 is dead
        (vms / name).mkdir(parents=True)
        info = {"name": name, "index": index, "pid": pid, "guest_ip": f"10.111.111.{4 * index + 2}"}
        (vms / name / "info.json").write_text(json.dumps(info))
    dead_vmm = f"firecracker --no-api --config-file {vms}/ccd-app-02/vm.json"
    exports = "/home/u/app\n\t\t10.111.111.2(rw,sync)\n\t\t10.111.111.6(rw,sync)\n/srv/other\n\t\t10.1.1.1(rw)\n"
    state = fc_prune.HostState(
        taps=["ccdtap0", "ccdtap1"],
        nft_tables=["ccd_ccd_app_01", "ccd_ccd_app_02"],
        docker_user_rules=["-A DOCKER-USER -i ccdtap0 -j ACCEPT", "-A DOCKER-USER -i ccdtap1 -j ACCEPT"],
        exports=fc_prune.parse_exports(exports),
        mounts=[vms / "ccd-app-01" / "files" / "f0", vms / "ccd-app-02" / "files" / "f0"],
        processes=[(111, f"firecracker --config-file {vms}/ccd-app-01/vm.json"), (222, dead_vmm)],
    )
    monkeypatch.setattr(fc_prune, "read_host_state", lambda root: state)
    monkeypatch.setattr("ccd.backends.firecracker.FirecrackerBackend.check_available", lambda self: None)  # no KVM needed to list
    options = f'[firecracker]\ncache_dir = "{tmp_path / "fc"}"\n'

    code, _ = ccd("prune", "--backend", "firecracker", toml=options)

    out = capsys.readouterr().out
    assert code == 0
    assert "stop orphaned VM process 222" in out and "process 111" not in out
    assert "unexport /home/u/app from 10.111.111.6" in out and "10.111.111.2" not in out and "10.1.1.1" not in out
    assert "delete tap ccdtap1" in out and "ccdtap0" not in out
    assert "delete nft table ccd_ccd_app_02" in out and "ccd_ccd_app_01" not in out
    assert "ccdtap1 -j ACCEPT" in out and "ccdtap0 -j" not in out
    assert str(vms / "ccd-app-02" / "files" / "f0") in out and str(vms / "ccd-app-01" / "files") not in out
    assert f"remove state dir {vms / 'ccd-app-02'}" in out and f"{vms / 'ccd-app-01'}" not in out.replace("process", "")
    assert (vms / "ccd-app-02").exists()  # dry run changes nothing


def test_vm_addresses_avoid_networks_the_host_already_uses():
    from ccd.backends.firecracker import net

    routes = "default via 192.168.88.1 dev eth0\n10.111.111.0/29 dev br-pihole proto kernel\n10.111.111.4/30 dev ccdtap0 proto kernel\n"
    addrs = "2: eth0    inet 192.168.88.10/24 brd 192.168.88.255 scope global eth0\n"
    taken = net.parse_host_networks(routes, addrs)

    index = net.first_free_index(set(), taken)

    # the pihole's 10.111.111.0/29 covers blocks 0 and 1; our own tap's route is ignored, so block 2 is the first free one
    assert net.addresses(index) == ("10.111.111.9", "10.111.111.10")


def _firecracker_cache(tmp_path, monkeypatch, stamp=True, kernel=True):
    """A cache dir as `ccd build` leaves it, with host-side checks stubbed out (no KVM, NFS server or root)."""
    from ccd.backends import firecracker as fc

    cache = tmp_path / "fc"
    cache.mkdir()
    (cache / "rootfs.squashfs").write_bytes(b"")
    if stamp:
        (cache / "rootfs.image-id").write_text(f"sha256:abc:{fc.assets_hash()}")
    if kernel:
        (cache / "vmlinux").write_bytes(b"")
    monkeypatch.setattr(fc.FirecrackerBackend, "check_available", lambda self: None)
    monkeypatch.setattr(fc.hostnet, "check_nfs_server", lambda: None)
    return cache, f'[firecracker]\ncache_dir = "{cache}"\nuplink = "eth0"\n'


def test_firecracker_run_refuses_a_rootfs_without_build_stamp(ccd, tmp_path, monkeypatch, capsys):
    cache, toml = _firecracker_cache(tmp_path, monkeypatch, stamp=False)
    (tmp_path / "app").mkdir()

    code, _ = ccd("run", "app", "--home", str(tmp_path / "h"), "--backend", "firecracker", toml=toml)

    assert code == 1 and "run 'ccd build'" in capsys.readouterr().out


def test_firecracker_run_fails_without_dns_and_leaves_no_vm_state(ccd, tmp_path, monkeypatch, capsys):
    from ccd.backends.firecracker import net

    cache, toml = _firecracker_cache(tmp_path, monkeypatch)
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 127.0.0.53\nnameserver fe80::1%wlan0\n")  # only addresses the VM cannot use
    monkeypatch.setattr(net, "RESOLV_CONF", resolv)
    (tmp_path / "app").mkdir()

    code, _ = ccd("run", "app", "--home", str(tmp_path / "h"), "--backend", "firecracker", toml=toml)

    assert code == 1 and "No usable IPv4 DNS server" in capsys.readouterr().out
    assert not (cache / "vms").exists()


def test_firecracker_build_without_a_kernel_fails(ccd, tmp_path, monkeypatch, capsys):
    from ccd.backends import firecracker as fc

    _, toml = _firecracker_cache(tmp_path, monkeypatch, kernel=False)
    monkeypatch.setattr(fc.FirecrackerBackend, "_rootfs_from", staticmethod(lambda *args: None))

    code, calls = ccd("build", toml='[build]\nbackend = "fake-docker"\n\n[run]\nbackend = "firecracker"\n' + toml)

    assert code == 1 and "No guest kernel" in capsys.readouterr().out


def test_firecracker_refuses_images_in_formats_it_cannot_import(ccd, tmp_path, monkeypatch, capsys):
    _, toml = _firecracker_cache(tmp_path, monkeypatch)

    code, _ = ccd("build", toml='[build]\nbackend = "fake-build"\n\n[run]\nbackend = "firecracker"\n' + toml)

    assert code == 1 and "cannot run images built by 'fake-build'" in capsys.readouterr().out


def test_hand_over_uses_the_runners_first_choice_among_what_the_builder_offers(ccd, monkeypatch):
    from conftest import FakeBuilder, FakeRunner

    monkeypatch.setattr(FakeBuilder, "offers", ["cheap", "rich"])
    monkeypatch.setattr(FakeRunner, "accepts", ["rich", "cheap"])  # the runner prefers rich even though it costs more

    _, calls = ccd("build", toml='[build]\nbackend = "fake-build"\n\n[run]\nbackend = "fake-run"\n')

    assert ("fake-run", "import", ("rich", "ccd")) in calls


def test_hand_over_without_a_common_format_names_both_backends(ccd, monkeypatch, capsys):
    from conftest import FakeRunner

    monkeypatch.setattr(FakeRunner, "accepts", ["something-else"])

    code, calls = ccd("build", toml='[build]\nbackend = "fake-build"\n\n[run]\nbackend = "fake-run"\n')

    out = capsys.readouterr().out
    assert code == 1 and [op for _, op, _ in calls] == ["build"]
    assert "fake-build offers: fake-native" in out and "fake-run accepts: something-else" in out


def test_builder_releases_the_image_even_when_the_import_fails(ccd, monkeypatch):
    from conftest import FakeRunner

    monkeypatch.setattr(FakeRunner, "import_fails", True)

    code, calls = ccd("build", toml='[build]\nbackend = "fake-build"\n\n[run]\nbackend = "fake-run"\n')

    assert code == 1 and calls[-1][:2] == ("fake-build", "release")


def test_buildah_builds_and_firecracker_packs_from_containers_storage(ccd, tmp_path, monkeypatch):
    import subprocess

    from ccd.backends import firecracker as fc

    cache, toml = _firecracker_cache(tmp_path, monkeypatch)
    monkeypatch.setattr("ccd.backends.buildah.BuildahBackend.check_available", lambda self: None)
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        if cmd[:2] == ["buildah", "run"]:
            (cache / fc.TMP_ROOTFS).write_bytes(b"squashfs")  # what mksquashfs would have written
        return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    code, _ = ccd("build", toml='[build]\nbackend = "buildah"\n\n[run]\nbackend = "firecracker"\n' + toml)

    assert code == 0
    assert not any(cmd[0] == "docker" for cmd in commands)
    bud, images, vm_layer, from_, run, rm = commands
    assert bud[:6] == ["buildah", "bud", "--layers", "-t", "localhost/ccd", "-f"] and bud[-1] == "."
    assert images[-1] == "localhost/ccd"
    assert vm_layer[:5] == ["buildah", "bud", "--layers", "-t", "localhost/ccd-vm"] and "BASE_IMAGE=localhost/ccd" in vm_layer
    assert from_ == ["buildah", "from", "localhost/ccd-vm"]
    assert run[:2] == ["buildah", "run"] and f"{cache}:/out" in run and "--mount" not in run  # --mount type=bind is read-only by default
    script = run[-1]
    assert "mksquashfs" in script and "chown" not in script  # rootless: the output is already the user's
    assert rm == ["buildah", "rm", "abc123"]
    assert (cache / "rootfs.squashfs").read_bytes() == b"squashfs"
    assert (cache / "rootfs.image-id").read_text().startswith("abc123:")


def test_docker_image_is_packed_by_docker_and_owned_by_the_invoking_user(ccd, tmp_path, monkeypatch):
    import os
    import subprocess

    from ccd.backends import firecracker as fc

    cache, toml = _firecracker_cache(tmp_path, monkeypatch, stamp=False)  # nothing built yet
    monkeypatch.setattr("ccd.backends.docker.DockerBackend.check_available", lambda self: None)
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd if isinstance(cmd, list) else cmd.split())
        if cmd[:2] == ["docker", "run"]:
            (cache / fc.TMP_ROOTFS).write_bytes(b"squashfs")
        return subprocess.CompletedProcess(cmd, 0, stdout="sha256:abc\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    code, _ = ccd("build", toml='[build]\nbackend = "docker"\n\n[run]\nbackend = "firecracker"\n' + toml)

    assert code == 0
    run = next(cmd for cmd in commands if cmd[:2] == ["docker", "run"])
    assert f"chown {os.getuid()}:{os.getgid()} /out/{fc.TMP_ROOTFS}" in run[-1]
    assert (cache / "rootfs.squashfs").exists()
