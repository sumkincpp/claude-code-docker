import hashlib
import ipaddress
import json
import logging
import os
import platform
import shutil
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from ccd.backends.firecracker import net as hostnet
from ccd.backends.firecracker import session as agent_session
from ccd.backends.firecracker import vm as microvm
from ccd.backends.firecracker.engines import ENGINES, Engine
from ccd.backends.firecracker.prune import prune as prune_leftovers
from ccd.plugin import AttachRequest, Backend, BackendError, BuildRequest, ImageInfo, RunRequest, format_age, short_id, timed

logger = logging.getLogger(__name__)

ASSETS = Path(__file__).parent / "assets"
CACHE_DIR = Path.home() / ".cache" / "ccd" / "firecracker"
DEFAULT_UPPER_SIZE = "4G"


ROOTFS_NAME = "rootfs.squashfs"
TMP_ROOTFS = ".rootfs.squashfs.tmp"
# The sources are the top-level entries of the container root, minus Docker's runtime artefacts (/proc /sys /dev, our
# /out mount, /.dockerenv), so the output file is never inside a source tree. /proc /sys /dev come back as empty
# directories (-p) because ccd-init mounts onto them. resolv.conf/hosts/hostname are bind-mounted in from the build
# host by Docker and are excluded (ccd-init writes the VM's own).
PACK_SCRIPT = (
    "set -e; cd /; set --; "
    'for e in $(ls -A /); do case "$e" in proc|sys|dev|out|.dockerenv) ;; *) set -- "$@" "/$e" ;; esac; done; '
    'mksquashfs "$@" /out/{tmp} -comp zstd -Xcompression-level 3 -b 128K -processors "$(nproc)" -noappend -no-progress'
    ' -p "proc d 755 0 0" -p "sys d 755 0 0" -p "dev d 755 0 0" -e etc/resolv.conf etc/hosts etc/hostname; '
    "ls -la /out/{tmp}; {chown}"
)


def host_arch() -> str:
    """Architecture name as used in Firecracker release and kernel URLs (x86_64 or aarch64)."""
    machine = platform.machine()
    return "aarch64" if machine == "arm64" else machine


def vm_subnet(options: dict) -> ipaddress.IPv4Network:
    """Range the per-VM /30 blocks are taken from ([firecracker] subnet, default hostnet.NET_BASE)."""
    try:
        return ipaddress.ip_network(options.get("subnet", hostnet.NET_BASE), strict=False)
    except ValueError as e:
        raise BackendError(f"Invalid [firecracker] subnet: {e}") from e


def describe_rootfs(rootfs: Path, stamp: Path) -> str:
    """'image 15388f94c27c, built 2 hours ago': which image the rootfs was packed from, and when."""
    image_id = short_id(stamp.read_text().rpartition(":")[0]) if stamp.exists() else "unknown"
    return f"image {image_id}, built {format_age(datetime.fromtimestamp(rootfs.stat().st_mtime, timezone.utc))}"


def cache_dir(options: dict) -> Path:
    return Path(options.get("cache_dir", CACHE_DIR)).expanduser()


def assets_hash() -> str:
    """Fingerprint of the VM-side files baked into the rootfs (init, agent, Dockerfile.rootfs, pack command)."""
    digest = hashlib.sha256()
    for asset in ("Dockerfile.rootfs", "ccd-init", "ccd-agent"):
        digest.update((ASSETS / asset).read_bytes())
    digest.update(PACK_SCRIPT.encode())
    return digest.hexdigest()[:16]


class FirecrackerBackend(Backend):
    name = "firecracker"
    can_build = False  # images come from another backend (see accepted_formats)

    def check_available(self) -> None:
        if shutil.which("firecracker") is None:
            raise BackendError("firecracker not found in PATH (see misc/install-firecracker.sh)")
        if not os.access("/dev/kvm", os.R_OK | os.W_OK):
            raise BackendError("/dev/kvm is missing or not accessible")

    def build(self, request: BuildRequest) -> None:
        raise BackendError(
            "Building images with the firecracker backend is not implemented yet; "
            'set [build] backend = "docker" (the image is handed over to firecracker automatically)'
        )

    def describe_image(self, built_id: str, options: dict) -> str:
        cache = cache_dir(options)
        stamp = cache / "rootfs.image-id"
        if not (cache / ROOTFS_NAME).exists() or not stamp.exists():
            return "no rootfs yet (run 'ccd build')"
        stamped_id, _, stamped_assets = stamp.read_text().rpartition(":")
        if not built_id:
            return "the build backend has no image to compare with (run 'ccd build')"
        if stamped_id != built_id:
            return "rootfs is built from an older image (run 'ccd build' to repack)"
        if stamped_assets != assets_hash():
            return "rootfs predates the current ccd VM files (run 'ccd build' to repack)"
        return "rootfs is up to date"

    def images(self, options: dict) -> list[ImageInfo]:
        cache = cache_dir(options)
        rows: list[ImageInfo] = []
        for kind, path in (("rootfs", cache / ROOTFS_NAME), ("kernel", cache / "vmlinux")):
            if path.exists():
                info = path.stat()
                image_id = (cache / "rootfs.image-id").read_text().rpartition(":")[0] if kind == "rootfs" and (cache / "rootfs.image-id").exists() else ""
                rows.append(ImageInfo(self.name, kind, str(path), image_id, info.st_size, datetime.fromtimestamp(info.st_mtime, timezone.utc)))
        return rows

    def accepted_formats(self) -> list[str]:
        return list(ENGINES)

    def import_image(self, fmt: str, location: str | Path, request: BuildRequest) -> None:
        """Produce the base rootfs (+ guest kernel) in the cache dir from an image built elsewhere."""
        cache = cache_dir(request.options)
        cache.mkdir(parents=True, exist_ok=True)  # created as the invoking user, not root

        self._rootfs_from(ENGINES[fmt], str(location), cache, request)
        self._ensure_kernel(cache, request.options.get("kernel_url"))
        logger.info("rootfs ready: %s", cache / ROOTFS_NAME)

    @staticmethod
    def _rootfs_from(engine: Engine, image: str, cache: Path, request: BuildRequest) -> None:
        """Add the VM layer as a normal cached image, then pack it straight into the cache dir (no export pass)."""
        rootfs, stamp = cache / ROOTFS_NAME, cache / "rootfs.image-id"
        image_id = engine.image_id(image)
        if not image_id:
            raise BackendError(f"Image '{image}' not found; build it first")
        # The stamp covers the inputs we control as well as the image: editing ccd-init or Dockerfile.rootfs must rebuild
        stamp_value = f"{image_id}:{assets_hash()}"
        if rootfs.exists() and stamp.exists() and stamp.read_text() == stamp_value and not request.config.force:
            logger.info("rootfs is up to date for image %s", image_id[:19])
            return

        vm_image = f"{image}-vm"
        # mksquashfs runs inside the container and writes directly into the mounted cache dir
        pack_script = PACK_SCRIPT.format(tmp=TMP_ROOTFS, chown=engine.chown(f"/out/{TMP_ROOTFS}"))
        try:
            with timed("VM layer image", logger):
                engine.add_vm_layer(image, vm_image, ASSETS / "Dockerfile.rootfs", ASSETS, request.config.force)
            with timed("Pack squashfs", logger):
                engine.pack(vm_image, cache, pack_script)
        except subprocess.CalledProcessError as e:
            (cache / TMP_ROOTFS).unlink(missing_ok=True)
            raise BackendError(f"Rootfs build failed with exit code {e.returncode}") from e
        os.replace(cache / TMP_ROOTFS, rootfs)
        stamp.write_text(stamp_value)

    @staticmethod
    def _ensure_kernel(cache: Path, kernel_url: str | None) -> None:
        if kernel_url:
            kernel_url = kernel_url.replace("{arch}", host_arch())
        kernel, stamp = cache / "vmlinux", cache / "vmlinux.url"
        if kernel_url:
            if kernel.exists() and stamp.exists() and stamp.read_text() == kernel_url:
                logger.info("Guest kernel already downloaded")
                return
            with timed(f"Kernel download ({kernel_url})", logger):
                urllib.request.urlretrieve(kernel_url, kernel)
            stamp.write_text(kernel_url)
        elif not kernel.exists():
            raise BackendError(f"No guest kernel at {kernel}; set kernel_url in the [firecracker] table of ccd.toml or place a vmlinux there")

    def run(self, request: RunRequest) -> None:
        options = request.options
        cache = cache_dir(options)
        rootfs = cache / ROOTFS_NAME
        if not rootfs.exists():
            raise BackendError(f"No rootfs in {cache}; run 'ccd build' first")
        stamp = cache / "rootfs.image-id"
        if not stamp.exists() or not stamp.read_text().endswith(f":{assets_hash()}"):
            # Booting it would not fail clearly: an older init ignores newer boot arguments and simply behaves differently
            raise BackendError("The VM rootfs is missing its build stamp or was built from older ccd VM files (init/agent changed); run 'ccd build' first")
        kernel = cache / "vmlinux"
        if not kernel.exists():
            raise BackendError(f"{kernel} not found; run 'ccd build' first")

        hostnet.check_nfs_server()
        mem_mib = microvm.parse_memory_mib(request.memory)
        cpus = int(float(options.get("vcpus", request.cpus)))
        # Resolved before the VM's state directory exists, so a failure here leaves nothing behind
        iface = options.get("uplink") or hostnet.default_iface()
        dns = list(options.get("dns") or hostnet.host_dns())

        registry = microvm.VmRegistry(cache)
        subnet = vm_subnet(options)
        index = hostnet.first_free_index(registry.used_indexes(), hostnet.host_networks(), subnet)
        host_ip, guest_ip = hostnet.addresses(index, subnet)
        directory = registry.create(request.name, index, guest_ip)

        net = hostnet.VmNet(
            name=request.name,
            index=index,
            tap=f"ccdtap{index}",
            host_ip=host_ip,
            guest_ip=guest_ip,
            uid=os.getuid(),
            gid=os.getgid(),
            iface=iface,
            dns=dns,
        )
        spec = microvm.BootSpec(
            name=request.name,
            guest_cidr=net.cidr,
            gateway=host_ip,
            dns=net.dns,
            host_ip=host_ip,
        )
        seen: set[tuple[Path, bool]] = set()
        for i, mount in enumerate(request.mounts):
            if mount.host.is_dir():
                if (mount.host, mount.readonly) not in seen:
                    seen.add((mount.host, mount.readonly))
                    net.exports.append(hostnet.Export(mount.host, mount.readonly))
                spec.nfs_mounts.append((str(mount.host), str(mount.container), mount.readonly))
            else:
                staged = f"f{i}"
                net.file_binds.append(hostnet.FileBind(mount.host, staged, mount.readonly))
                spec.file_mounts.append((staged, str(mount.container), mount.readonly))
        if net.file_binds:
            net.staging = directory / "files"
            net.staging.mkdir()
            spec.files_export = str(net.staging)

        try:
            upper = directory / "upper.ext4"
            microvm.make_upper(upper, str(options.get("upper_size", DEFAULT_UPPER_SIZE)))
            config = microvm.vm_config(kernel, rootfs, upper, net.tap, index, mem_mib, cpus, microvm.boot_args(spec), directory / "v.sock")
            config_path = directory / "vm.json"
            config_path.write_text(json.dumps(config, indent=2))

            logger.info("  Rootfs: %s (%s)", rootfs, describe_rootfs(rootfs, stamp))
            logger.info("  Kernel: %s", kernel)
            logger.info("  VM: %s  guest %s  host %s  tap %s via %s", request.name, guest_ip, host_ip, net.tap, net.iface)
            hostnet.setup(net)
            self._run_agent_session(directory, config_path, request.root)
        finally:
            hostnet.teardown(net)
            microvm.VmRegistry.remove(directory)

    @staticmethod
    def _run_agent_session(directory: Path, config_path: Path, root: bool) -> None:
        """Boot the VM in the background (console -> console.log), run the session through the guest agent, then power off."""
        log_path = directory / "console.log"
        uds = directory / "v.sock"
        with open(log_path, "wb") as log:
            vmm = subprocess.Popen(["firecracker", "--no-api", "--config-file", str(config_path)], stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            try:
                try:
                    with timed("VM boot to agent", logger):
                        agent_session.wait_for_agent(uds, vmm)
                except BackendError as e:
                    raise BackendError(f"{e}\n--- VM console (last lines) ---\n{microvm.tail(log_path)}") from e
                agent_session.run_interactive(uds, root)
            except KeyboardInterrupt:
                logger.info("VM stopped by user")
            finally:
                if vmm.poll() is None:
                    agent_session.poweroff(uds)  # clean shutdown first so NFS clients flush; fall back to killing the VMM
                    try:
                        vmm.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        vmm.kill()
                        vmm.wait()

    def attach(self, request: AttachRequest) -> None:
        info = microvm.VmRegistry(cache_dir(request.options)).get(request.name)
        if info is None:
            raise BackendError(f"No running firecracker VM named '{request.name}'")
        try:
            agent_session.run_interactive(info.vsock_path, request.root)
        except OSError as e:
            raise BackendError(f"Cannot reach the agent of '{request.name}': {e}") from e

    def prune(self, options: dict, dry_run: bool) -> list[str]:
        return prune_leftovers(cache_dir(options), dry_run, vm_subnet(options))

    def list_running(self) -> list[str]:
        return [vm.name for vm in microvm.VmRegistry(cache_dir({})).live()]
