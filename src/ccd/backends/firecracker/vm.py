"""MicroVM description and bookkeeping: boot args, Firecracker config, per-VM state."""

import json
import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ccd.plugin import BackendError

CMDLINE_LIMIT = 2048  # arm64 COMMAND_LINE_SIZE; x86_64 allows more
MIN_MEMORY_MIB = 128
GUEST_CID = 3  # any value >= 3; the VM is only reachable through its own host-side Unix socket
_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def parse_memory_mib(value: str) -> int:
    """Docker-style memory string ('512m', '1g', '1073741824') -> MiB."""
    match = re.fullmatch(r"(\d+)([kmg]?)b?", value.strip().lower())
    if not match:
        raise BackendError(f"Invalid memory value: {value!r} (use e.g. 512m or 2g)")
    mib = int(match.group(1)) * _UNITS[match.group(2)] // 1024**2
    if mib < MIN_MEMORY_MIB:
        raise BackendError(f"Memory must be at least {MIN_MEMORY_MIB}m, got {value!r}")
    return mib


def check_cmdline_token(value: str, what: str) -> str:
    """Boot args are whitespace/colon separated; reject values that would break the format."""
    if re.search(r"[\s:]", value):
        raise BackendError(f"{what} contains whitespace or ':' and cannot be passed to the VM: {value!r}")
    return value


@dataclass
class BootSpec:
    name: str
    guest_cidr: str
    gateway: str
    dns: list[str]
    host_ip: str
    nfs_mounts: list[tuple[str, str, bool]] = field(default_factory=list)  # (exported host dir, guest path, readonly)
    files_export: str | None = None  # staging dir holding bind-mounted single files
    file_mounts: list[tuple[str, str, bool]] = field(default_factory=list)  # (staged name, guest path, readonly)


def boot_args(spec: BootSpec) -> str:
    args = [
        "console=ttyS0",
        "reboot=k",
        "panic=1",
        *(["pci=off"] if platform.machine() == "x86_64" else []),  # no PCI bus in a Firecracker guest
        "root=/dev/vda",
        "rootfstype=squashfs",
        "ro",
        "init=/sbin/ccd-init",
        "ccd.overlay=1",
        f"ccd.hostname={check_cmdline_token(spec.name, 'VM name')}",
        f"ccd.ip={spec.guest_cidr}",
        f"ccd.gw={spec.gateway}",
        f"ccd.dns={','.join(spec.dns)}",
    ]
    for export, guest, readonly in spec.nfs_mounts:
        check_cmdline_token(export, "Mount source")
        check_cmdline_token(guest, "Mount target")
        args.append(f"ccd.mount={spec.host_ip}:{export}:{guest}:{'ro' if readonly else 'rw'}")
    if spec.files_export:
        args.append(f"ccd.files={spec.host_ip}:{check_cmdline_token(spec.files_export, 'Staging dir')}")
        for name, guest, readonly in spec.file_mounts:
            args.append(f"ccd.file={name}:{check_cmdline_token(guest, 'File mount target')}:{'ro' if readonly else 'rw'}")

    line = " ".join(args)
    if len(line) >= CMDLINE_LIMIT:
        raise BackendError(f"Kernel command line too long ({len(line)} >= {CMDLINE_LIMIT}); reduce the number of mounts")
    return line


def vm_config(kernel: Path, rootfs: Path, upper: Path, tap: str, index: int, mem_mib: int, cpus: int, args: str, vsock_path: Path) -> dict:
    # AF_UNIX paths are limited to 107 bytes; fail with a clear message instead of an obscure Firecracker error
    if len(str(vsock_path)) > 100:
        raise BackendError(f"vsock socket path too long ({len(str(vsock_path))} chars): {vsock_path}; use a shorter cache_dir")
    return {
        "boot-source": {"kernel_image_path": str(kernel), "boot_args": args},
        "drives": [
            {"drive_id": "rootfs", "path_on_host": str(rootfs), "is_root_device": True, "is_read_only": True},
            {"drive_id": "upper", "path_on_host": str(upper), "is_root_device": False, "is_read_only": False},
        ],
        "network-interfaces": [{"iface_id": "eth0", "host_dev_name": tap, "guest_mac": f"AA:FC:00:00:{index >> 8 & 0xFF:02X}:{index & 0xFF:02X}"}],
        "vsock": {"guest_cid": GUEST_CID, "uds_path": str(vsock_path)},
        "machine-config": {"vcpu_count": cpus, "mem_size_mib": mem_mib},
    }


def make_upper(path: Path, size: str) -> None:
    """Per-VM writable layer: a sparse ext4 file (instant to create, thrown away with the VM)."""
    subprocess.run(["truncate", "-s", size, str(path)], check=True)
    subprocess.run(["mkfs.ext4", "-q", "-F", str(path)], check=True)


@dataclass
class VmInfo:
    name: str
    index: int
    pid: int
    guest_ip: str
    dir: Path

    def alive(self) -> bool:
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @property
    def vsock_path(self) -> Path:
        return self.dir / "v.sock"


class VmRegistry:
    """One directory per VM under <cache>/vms/<name>/ holding info.json, the Firecracker config and drives."""

    def __init__(self, cache_dir: Path):
        self.root = cache_dir / "vms"

    def all(self) -> list[VmInfo]:
        vms: list[VmInfo] = []
        if not self.root.is_dir():
            return vms
        for child in sorted(self.root.iterdir()):
            try:
                data = json.loads((child / "info.json").read_text())
                vms.append(VmInfo(name=data["name"], index=data["index"], pid=data["pid"], guest_ip=data["guest_ip"], dir=child))
            except (OSError, ValueError, KeyError):
                continue
        return vms

    def live(self) -> list[VmInfo]:
        return [vm for vm in self.all() if vm.alive()]

    def get(self, name: str) -> VmInfo | None:
        return next((vm for vm in self.live() if vm.name == name), None)

    def used_indexes(self) -> set[int]:
        return {vm.index for vm in self.live()}

    def create(self, name: str, index: int, guest_ip: str) -> Path:
        directory = self.root / name
        if directory.exists():
            # Stale dir of a dead VM with the same name (e.g. its teardown was interrupted): single-file bind mounts may
            # still be mounted inside it and would make rmtree fail
            for staged in (directory / "files").glob("*"):
                if os.path.ismount(staged):
                    subprocess.run(["sudo", "umount", "-l", str(staged)], check=False)
            shutil.rmtree(directory, ignore_errors=True)
            if directory.exists():
                raise BackendError(f"Leftover state of a dead VM could not be removed: {directory}")
        directory.mkdir(parents=True)
        (directory / "info.json").write_text(json.dumps({"name": name, "index": index, "pid": os.getpid(), "guest_ip": guest_ip}))
        return directory

    @staticmethod
    def remove(directory: Path) -> None:
        shutil.rmtree(directory, ignore_errors=True)


def tail(path: Path, lines: int = 30) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return "(no console log)"
