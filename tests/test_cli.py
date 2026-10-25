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


def test_images_lists_every_ready_backend_not_only_the_configured_ones(ccd, capsys):
    ccd("images", toml='[build]\nbackend = "fake-build"\n\n[run]\nbackend = "fake-run"\n')

    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["BACKEND", "KIND", "NAME", "ID", "SIZE", "CREATED"]
    assert [line.split()[0] for line in out[1:]] == ["fake", "fake-build", "fake-docker", "fake-run"]  # not-ready backends (docker, ...) are skipped


def test_images_backend_flag_restricts_the_listing(ccd, capsys):
    ccd("images", "--backend", "fake-run")
    assert [line.split()[0] for line in capsys.readouterr().out.splitlines()[1:]] == ["fake-run"]

    ccd("images", "--backend", "docker")  # asked for explicitly: say why there is nothing
    assert "docker: not ready: disabled in tests" in capsys.readouterr().out


def test_status_backend_flag_overrides_the_build_backend(ccd, monkeypatch, capsys):
    from conftest import FakeBuilder

    monkeypatch.setattr(FakeBuilder, "built_id", "id-from-override")

    ccd("status", "--backend", "fake-build", toml='[build]\nbackend = "fake"\n\n[run]\nbackend = "fake-run"\n')

    assert "Image 'ccd' in fake-build: id-from-override" in capsys.readouterr().out


def test_status_reports_backends_image_and_the_run_backends_verdict(ccd, monkeypatch, capsys):
    from conftest import FakeRunner

    monkeypatch.setattr(FakeRunner, "verdict", "rootfs is up to date")
    monkeypatch.setattr(FakeRunner, "running", ["ccd-app-01"])

    ccd("status", toml='[build]\nbackend = "fake-build"\n\n[run]\nbackend = "fake-run"\n')

    out = capsys.readouterr().out
    assert "Build backend: fake-build (ready)" in out and "Run backend:   fake-run (ready)" in out
    assert "Image 'ccd' in fake-build: id-1" in out
    assert "Image for fake-run: rootfs is up to date" in out
    assert "Running: ccd-app-01" in out


def test_status_shows_when_firecracker_rootfs_is_older_than_the_docker_image(ccd, tmp_path, monkeypatch, capsys):
    cache, toml = _firecracker_cache(tmp_path, monkeypatch)  # stamp says: built from sha256:abc
    config = '[build]\nbackend = "fake-docker"\n\n[run]\nbackend = "firecracker"\n' + toml

    ccd("status", toml=config)
    assert "rootfs is up to date" in capsys.readouterr().out

    (cache / "rootfs.image-id").write_text("sha256:old:" + (cache / "rootfs.image-id").read_text().rpartition(":")[2])
    ccd("status", toml=config)
    assert "built from an older image" in capsys.readouterr().out


def test_backends_shows_what_each_can_do_and_whether_it_is_ready(ccd, monkeypatch, capsys):
    monkeypatch.setattr("ccd.backends.firecracker.FirecrackerBackend.check_available", lambda self: (_ for _ in ()).throw(plugin_error("no KVM")))
    monkeypatch.setattr("ccd.backends.buildah.BuildahBackend.check_available", lambda self: None)
    monkeypatch.setattr("ccd.backends.docker.DockerBackend.check_available", lambda self: None)

    ccd("backends")

    rows = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()[1:]}
    assert "build+run" in rows["docker"] and "ready" in rows["docker"]
    assert rows["buildah"].split()[1] == "build"  # builds only
    assert "not ready: no KVM" in rows["firecracker"]


def plugin_error(message):
    from ccd.plugin import BackendError

    return BackendError(message)


def test_images_lists_buildah_images_from_its_json_output(ccd, monkeypatch, capsys):
    import json
    import subprocess
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr("ccd.backends.buildah.BuildahBackend.check_available", lambda self: None)
    listing = [
        {"id": "15388f94c27c", "names": ["localhost/ccd:latest"], "size": "3.7 GB", "createdat": "2 hours ago"},  # buildah only lists relative ages
        {"id": "223950bd783", "names": ["localhost/ccd-vm:latest"], "size": 3730000000, "createdat": "2 hours ago"},
        {"id": "aaaa", "names": ["docker.io/library/alpine:3.24"], "size": "8 MB", "createdat": "a month ago"},  # not ours
        {"id": "bbbb", "names": None, "size": "1 MB", "createdat": "a month ago"},  # dangling layer
    ]
    now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("ccd.plugin.utcnow", lambda: now)  # no real clock: the test must not depend on when or where it runs
    two_hours_ago = (now - timedelta(hours=2)).isoformat()

    def fake_run(cmd, **kwargs):
        out = json.dumps({"OCIv1": {"created": two_hours_ago}}) if cmd[1] == "inspect" else json.dumps(listing)
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="WARN something")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ccd("images", "--backend", "buildah")

    lines = capsys.readouterr().out.splitlines()[1:]
    assert [line.split()[:3] for line in lines] == [["buildah", "image", "localhost/ccd:latest"], ["buildah", "image", "localhost/ccd-vm:latest"]]
    assert "15388f94c27c" in lines[0] and "3.7GB" in lines[0] and "3.7GB" in lines[1]  # "3.7 GB" text and plain bytes look the same
    assert "2 hours ago" in lines[0] and "2 hours ago" in lines[1]


def test_images_tolerates_unparseable_buildah_output(ccd, monkeypatch, capsys):
    import subprocess

    monkeypatch.setattr("ccd.backends.buildah.BuildahBackend.check_available", lambda self: None)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr=""))

    code, _ = ccd("images", "--backend", "buildah")

    assert code == 0 and "No build data yet" in capsys.readouterr().out


def test_images_shows_id_size_and_age_the_same_way_for_every_backend(ccd, tmp_path, monkeypatch, capsys):
    import json
    import subprocess
    from datetime import datetime, timedelta, timezone

    _, toml = _firecracker_cache(tmp_path, monkeypatch)  # stamp: "sha256:abc:<assets hash>"
    monkeypatch.setattr("ccd.backends.docker.DockerBackend.check_available", lambda self: None)
    monkeypatch.setattr("ccd.backends.buildah.BuildahBackend.check_available", lambda self: None)
    now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("ccd.plugin.utcnow", lambda: now)  # fixed clock, so the day arithmetic cannot depend on timezone or DST
    docker_inspect = [{"Id": "sha256:f86a9d67e2ce" + "0" * 52, "RepoTags": ["ccd:latest"], "Size": 3_720_000_000, "Created": (now - timedelta(days=3, hours=1)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")}]
    old = now - timedelta(days=30)
    buildah_list = [{"id": "15388f94c27c", "names": ["localhost/ccd:latest"], "size": "3.72 GB", "createdat": "a month ago"}]
    buildah_inspect = {"OCIv1": {"created": old.isoformat()}}

    def fake_run(cmd, **kwargs):
        out = {"docker": json.dumps(docker_inspect), "buildah": json.dumps(buildah_inspect if cmd[1] == "inspect" else buildah_list)}.get(cmd[0], "")
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ccd("images", toml=toml)

    rows = {line.split()[0]: line.split() for line in capsys.readouterr().out.splitlines()[1:] if line.split()[1] in ("image", "rootfs")}
    assert rows["docker"][3:] == ["f86a9d67e2ce", "3.7GB", "3", "days", "ago"]  # recent: relative
    assert rows["buildah"][3:] == ["15388f94c27c", "3.7GB", old.astimezone().strftime("%Y-%m-%d")]  # older than a week: the date
    assert rows["firecracker"][3] == "abc"  # the same short form: no "sha256:" prefix, at most 12 characters


def test_age_is_relative_for_a_week_then_a_date():
    from datetime import datetime, timedelta, timezone

    from ccd.plugin import format_age

    now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)  # zone-aware, so the result does not depend on the machine's timezone
    assert format_age(now - timedelta(seconds=20), now) == "just now"
    assert format_age(now - timedelta(minutes=1), now) == "1 minute ago"
    assert format_age(now - timedelta(minutes=59), now) == "59 minutes ago"
    assert format_age(now - timedelta(hours=2), now) == "2 hours ago"
    assert format_age(now - timedelta(days=6, hours=23), now) == "6 days ago"
    assert format_age(now - timedelta(days=7), now) == (now - timedelta(days=7)).astimezone().strftime("%Y-%m-%d")  # local date


def test_firecracker_run_logs_which_image_the_rootfs_was_built_from(ccd, tmp_path, monkeypatch, capsys):
    from ccd.backends import firecracker as fc

    cache, toml = _firecracker_cache(tmp_path, monkeypatch)  # stamp: "sha256:abc:<assets hash>"
    monkeypatch.setattr(fc.hostnet, "setup", lambda net: None)
    monkeypatch.setattr(fc.hostnet, "teardown", lambda net: None)
    monkeypatch.setattr(fc.hostnet, "host_networks", lambda: [])
    monkeypatch.setattr(fc.microvm, "make_upper", lambda path, size: path.write_bytes(b""))
    monkeypatch.setattr(fc.FirecrackerBackend, "_run_agent_session", staticmethod(lambda *args: None))
    (tmp_path / "app").mkdir()

    code, _ = ccd("-v", "run", "app", "--home", str(tmp_path / "h"), "--backend", "firecracker", toml=toml + 'dns = ["1.1.1.1"]\n')

    out = capsys.readouterr().out
    assert code == 0
    assert f"Rootfs: {cache / 'rootfs.squashfs'} (image abc, built just now)" in out
    assert f"Kernel: {cache / 'vmlinux'}" in out


def test_age_ignores_the_systems_timezone_and_dst_changes():
    from datetime import datetime, timedelta, timezone

    from ccd.plugin import format_age

    created = datetime(2026, 10, 22, 18, 0, tzinfo=timezone(timedelta(hours=2)))  # 16:00 UTC, summer time
    now = datetime(2026, 10, 25, 16, 30, tzinfo=timezone.utc)  # three days and half an hour later; the clocks went back in between

    assert format_age(created, now) == "3 days ago"  # wall-clock subtraction (18:00 -> 17:30) would give 2 days


def _podman(monkeypatch, exists=True):
    """Make podman 'installed' and record every command it is given."""
    import subprocess

    monkeypatch.setattr("ccd.backends.podman.PodmanBackend.check_available", lambda self: None)
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd if isinstance(cmd, list) else cmd.split())
        return subprocess.CompletedProcess(cmd, 0 if exists or cmd[:3] != ["podman", "image", "exists"] else 1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return commands


def test_podman_builds_and_runs_localhost_images(ccd, tmp_path, monkeypatch):
    commands = _podman(monkeypatch)
    (tmp_path / "app").mkdir()

    ccd("build", "--backend", "podman")
    ccd("run", "app", "--home", str(tmp_path / "h"), "--backend", "podman", "-u", "root", "--memory", "2g")

    build = next(cmd for cmd in commands if cmd[:2] == ["podman", "build"])
    assert build[-3:-1] == ["-t", "localhost/ccd"] or "localhost/ccd" in build  # podman needs the localhost/ prefix
    run = next(cmd for cmd in commands if cmd[:2] == ["podman", "run"])
    assert run[-1] == "localhost/ccd" and ["--user", "root"] == run[run.index("--user") : run.index("--user") + 2]
    assert "2g" in run and any(arg.endswith(":/app") for arg in run)


def test_podman_runs_an_image_built_by_buildah_without_copying_it(ccd, monkeypatch):
    commands = _podman(monkeypatch)
    monkeypatch.setattr("ccd.backends.buildah.BuildahBackend.check_available", lambda self: None)

    code, _ = ccd("build", toml='[build]\nbackend = "buildah"\n\n[run]\nbackend = "podman"\n')

    assert code == 0
    assert ["podman", "image", "exists", "localhost/ccd"] in commands
    assert not any(cmd[:2] in (["buildah", "push"], ["podman", "load"]) for cmd in commands)  # same store: nothing is moved


def test_podman_reports_a_missing_image_after_a_buildah_build(ccd, monkeypatch, capsys):
    _podman(monkeypatch, exists=False)
    monkeypatch.setattr("ccd.backends.buildah.BuildahBackend.check_available", lambda self: None)

    code, _ = ccd("build", toml='[build]\nbackend = "buildah"\n\n[run]\nbackend = "podman"\n')

    assert code == 1 and "not in podman's storage" in capsys.readouterr().out


def test_images_lists_an_image_once_when_buildah_and_podman_share_it(ccd, monkeypatch, capsys):
    import json
    import subprocess

    monkeypatch.setattr("ccd.backends.podman.PodmanBackend.check_available", lambda self: None)
    monkeypatch.setattr("ccd.backends.buildah.BuildahBackend.check_available", lambda self: None)
    listing = [{"id": "15388f94c27c", "names": ["localhost/ccd:latest"], "size": "3.7 GB", "createdat": "1 hour ago"}]
    inspect = [{"Id": "15388f94c27c" + "0" * 52, "RepoTags": ["localhost/ccd:latest"], "Size": 3_700_000_000, "Created": "2026-10-25T19:50:12Z"}]

    def fake_run(cmd, **kwargs):
        out = json.dumps(listing if cmd[:3] == ["buildah", "images", "--json"] else inspect)
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ccd("images")

    rows = [line.split() for line in capsys.readouterr().out.splitlines()[1:]]
    assert [(row[0], row[2]) for row in rows if row[0] in ("buildah", "podman")] == [("buildah", "localhost/ccd:latest")]


def test_podman_runs_as_container_root_with_the_image_users_home_by_default(ccd, tmp_path, monkeypatch):
    commands = _podman(monkeypatch)
    (tmp_path / "app").mkdir()

    ccd("run", "app", "--home", str(tmp_path / "h"), "--backend", "podman")

    run = next(cmd for cmd in commands if cmd[:2] == ["podman", "run"])
    assert ["--user", "root", "-e", "HOME=/home/user"] == run[run.index("--user") : run.index("--user") + 4]
    assert run.index("--user") < run.index("-v") < run.index("localhost/ccd")  # run flags, then volumes, then the image


def test_podman_user_image_setting_keeps_the_image_user(ccd, tmp_path, monkeypatch):
    commands = _podman(monkeypatch)
    (tmp_path / "app").mkdir()

    ccd("run", "app", "--home", str(tmp_path / "h"), "--backend", "podman", toml='[podman]\nuser = "image"\n')

    run = next(cmd for cmd in commands if cmd[:2] == ["podman", "run"])
    assert "--user" not in run and "HOME=/home/user" not in run


def test_podman_attach_uses_the_same_user_as_run(ccd, monkeypatch):
    commands = _podman(monkeypatch)
    monkeypatch.setattr("ccd.backends.podman.PodmanBackend.list_running", lambda self: ["ccd-app-01"])

    ccd("attach", "--backend", "podman")

    exec_cmd = next(cmd for cmd in commands if cmd[:2] == ["podman", "exec"])
    assert exec_cmd == ["podman", "exec", "-it", "--user", "root", "-e", "HOME=/home/user", "ccd-app-01", "/bin/bash"]


def test_docker_user_flags_are_unchanged(ccd, tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr("ccd.backends.docker.DockerBackend.check_available", lambda self: None)
    commands = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: commands.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
    (tmp_path / "app").mkdir()

    ccd("run", "app", "--home", str(tmp_path / "h"), "--backend", "docker")
    ccd("run", "app", "--home", str(tmp_path / "h"), "--backend", "docker", "-u", "root")

    plain, as_root = [cmd for cmd in commands if cmd[:2] == ["docker", "run"]]
    assert "--user" not in plain and as_root[as_root.index("--user") + 1] == "root" and "HOME=/home/user" not in as_root


def test_command_after_double_dash_is_passed_to_the_backend(ccd, tmp_path):
    (tmp_path / "app").mkdir()

    code, calls = ccd("run", "app", "--home", str(tmp_path / "h"), "--backend", "fake", "-u", "root", "--", "pytest", "-q", "-k", "a b")

    request = calls[0][2]
    assert code == 0 and request.command == ["pytest", "-q", "-k", "a b"] and request.root is True  # arguments stay separate


def test_dot_takes_a_command_too_and_without_one_the_shell_is_interactive(ccd):
    code, calls = ccd(".", "--backend", "fake", "--home", "h", "--", "ls", "-l")
    assert calls[0][2].command == ["ls", "-l"]

    _, calls = ccd(".", "--backend", "fake", "--home", "h")
    assert calls[1][2].command == []


def test_exit_code_of_the_command_is_the_exit_code_of_ccd(ccd, tmp_path, monkeypatch):
    from conftest import RecordingBackend

    (tmp_path / "app").mkdir()
    monkeypatch.setattr(RecordingBackend, "run_code", 3)

    code, _ = ccd("run", "app", "--home", str(tmp_path / "h"), "--backend", "fake", "--", "false")

    assert code == 3


def test_command_banner_goes_to_stderr_so_stdout_stays_the_commands(ccd, tmp_path, capsys):
    (tmp_path / "app").mkdir()

    ccd("run", "app", "--home", str(tmp_path / "h"), "--backend", "fake", "--", "echo", "hi")
    quiet = capsys.readouterr()
    ccd("run", "app", "--home", str(tmp_path / "h"), "--backend", "fake")
    shell = capsys.readouterr()

    assert "Starting container" not in quiet.out and "Starting container" in quiet.err
    assert "Starting container" in shell.out  # the interactive shell is unchanged


def test_double_dash_keeps_its_meaning_for_build(ccd):
    _, calls = ccd("build", "--", "--progress=plain", toml='[run]\nbackend = "fake"\n')

    assert calls[0][2].extra_args == ["--", "--progress=plain"]  # exactly what argparse handed over before commands existed


def test_docker_runs_a_command_without_a_terminal_and_returns_its_exit_code(ccd, tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr("ccd.backends.docker.DockerBackend.check_available", lambda self: None)
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 7, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    (tmp_path / "app").mkdir()

    code, _ = ccd("run", "app", "--home", str(tmp_path / "h"), "--backend", "docker", "--", "pytest", "-q")
    ccd("run", "app", "--home", str(tmp_path / "h"), "--backend", "docker")

    command_run, shell_run = [cmd for cmd in commands if cmd[:2] == ["docker", "run"]]
    assert code == 7  # not "Container run failed": the exit code is passed through
    assert "-i" in command_run and "-it" not in command_run and "-t" not in command_run  # pytest has no terminal on stdin
    assert command_run[-3:] == ["ccd", "pytest", "-q"]  # image, then the command
    assert "-it" in shell_run and shell_run[-1] == "ccd"


def test_podman_command_runs_as_container_root_after_the_image(ccd, tmp_path, monkeypatch):
    commands = _podman(monkeypatch)
    (tmp_path / "app").mkdir()

    ccd("run", "app", "--home", str(tmp_path / "h"), "--backend", "podman", "--", "make", "test")

    run = next(cmd for cmd in commands if cmd[:2] == ["podman", "run"])
    assert run[-3:] == ["localhost/ccd", "make", "test"] and "--user" in run
