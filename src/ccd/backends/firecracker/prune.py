"""Find and remove what dead ccd runs leave behind: taps, firewall rules, NFS exports, bind mounts, VMM processes and VM dirs.

Anything that belongs to a VM whose `ccd` process is still alive (see VmRegistry) is left alone.
"""

import ipaddress
import os
import re
import shlex
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ccd.backends.firecracker import net as hostnet
from ccd.backends.firecracker.vm import VmInfo, VmRegistry
from ccd.plugin import BackendError

TAP_RE = re.compile(r"^\d+:\s+(ccdtap(\d+))[:@]")
CLIENT_RE = re.compile(r"^([\d.]+)\(")
TAP_IN_RULE_RE = re.compile(r"\bccdtap(\d+)\b")


@dataclass
class HostState:
    """What the host currently has, as the commands report it."""

    taps: list[str] = field(default_factory=list)  # ip -o link show
    nft_tables: list[str] = field(default_factory=list)  # names of `table ip <name>`
    docker_user_rules: list[str] = field(default_factory=list)  # iptables -S DOCKER-USER
    exports: list[tuple[str, str]] = field(default_factory=list)  # (path, client ip) from exportfs -v
    mounts: list[Path] = field(default_factory=list)  # mount points under the VM state dir
    processes: list[tuple[int, str]] = field(default_factory=list)  # (pid, cmdline) of firecracker processes


@dataclass
class Plan:
    commands: list[tuple[str, str]] = field(default_factory=list)  # (description, shell line run as root)
    kills: list[tuple[int, str]] = field(default_factory=list)  # (pid, description)
    remove_dirs: list[Path] = field(default_factory=list)

    def descriptions(self) -> list[str]:
        return [d for _, d in self.kills] + [d for d, _ in self.commands] + [f"remove state dir {p}" for p in self.remove_dirs]

    def __bool__(self) -> bool:
        return bool(self.commands or self.kills or self.remove_dirs)


def client_index(ip: str, subnet: ipaddress.IPv4Network) -> int | None:
    """Index of the VM a guest address belongs to, or None when it is not one of ours."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if address not in subnet:
        return None
    return (int(address) - int(subnet.network_address)) // 4


def parse_exports(text: str) -> list[tuple[str, str]]:
    """`exportfs -v`: a path line, then its clients either on the same line or on indented lines below."""
    found: list[tuple[str, str]] = []
    path = ""
    for line in text.splitlines():
        if not line.strip():
            continue
        tokens = line.split()
        if not line[0].isspace():
            path, tokens = tokens[0], tokens[1:]
        for token in tokens:
            match = CLIENT_RE.match(token)
            if match and path:
                found.append((path, match.group(1)))
    return found


def read_host_state(vms_root: Path) -> HostState:
    def root_out(*cmd: str, required: bool = True) -> str:
        """Output of a command run as root. A failure is an error, not an empty answer: prune must not claim 'nothing to prune' unseen."""
        result = subprocess.run(["sudo", *cmd], capture_output=True, text=True, check=False)
        if result.returncode != 0 and required:
            raise BackendError(f"Cannot inspect the host ({' '.join(cmd)}): {result.stderr.strip() or 'exit code ' + str(result.returncode)}")
        return result.stdout if result.returncode == 0 else ""

    state = HostState()
    links = subprocess.run(["ip", "-o", "link", "show"], capture_output=True, text=True, check=False).stdout
    state.taps = [m.group(1) for line in links.splitlines() if (m := TAP_RE.match(line))]
    state.nft_tables = [m.group(1) for line in root_out("nft", "list", "tables").splitlines() if (m := re.match(r"table ip (ccd_\S+)", line))]
    if shutil.which("iptables"):
        state.docker_user_rules = [line for line in root_out("iptables", "-S", "DOCKER-USER", required=False).splitlines() if line.startswith("-A DOCKER-USER") and "ccdtap" in line]
    if shutil.which("exportfs"):
        state.exports = parse_exports(root_out("exportfs", "-v"))
    prefix = str(vms_root) + os.sep
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            point = line.split()[1].replace("\\040", " ")
            if point.startswith(prefix):
                state.mounts.append(Path(point))
    except OSError:
        pass
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            cmdline = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue
        if cmdline.startswith("firecracker") or "/firecracker " in cmdline:
            state.processes.append((int(proc.name), cmdline))
    return state


def make_plan(state: HostState, live: list[VmInfo], state_dirs: list[Path], subnet: ipaddress.IPv4Network) -> Plan:
    live_indexes = {vm.index for vm in live}
    live_tables = {hostnet.nft_table_name(vm.name) for vm in live}
    live_dirs = {vm.dir for vm in live}
    dead_dirs = [d for d in state_dirs if d not in live_dirs]
    q = shlex.quote
    plan = Plan()

    for pid, cmdline in state.processes:
        if any(str(d) + os.sep in cmdline for d in dead_dirs):
            plan.kills.append((pid, f"stop orphaned VM process {pid}"))
    for path, ip in state.exports:
        index = client_index(ip, subnet)
        if index is not None and index not in live_indexes:
            plan.commands.append((f"unexport {path} from {ip}", f"exportfs -u {q(f'{ip}:{path}')}"))
    for point in state.mounts:
        if not any(live_dir in point.parents for live_dir in live_dirs):
            plan.commands.append((f"unmount {point}", f"umount -l {q(str(point))}"))
    for table in state.nft_tables:
        if table not in live_tables:
            plan.commands.append((f"delete nft table {table}", f"nft delete table ip {q(table)}"))
    for rule in state.docker_user_rules:
        match = TAP_IN_RULE_RE.search(rule)
        if match and int(match.group(1)) not in live_indexes:
            plan.commands.append((f"delete DOCKER-USER rule: {rule[len('-A DOCKER-USER '):]}", "iptables " + rule.replace("-A", "-D", 1)))
    for tap in state.taps:
        if int(tap.removeprefix("ccdtap")) not in live_indexes:
            plan.commands.append((f"delete tap {tap}", f"ip link del {q(tap)}"))
    plan.remove_dirs = dead_dirs
    return plan


def execute(plan: Plan) -> None:
    # Ctrl-C would stop the cleanup halfway and leave a mix of leftovers behind
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        for pid, _ in plan.kills:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if plan.commands:
            script = "\n".join(f"{line} || true" for _, line in plan.commands)
            subprocess.run(["sudo", "sh", "-c", script], check=False)
        for directory in plan.remove_dirs:
            shutil.rmtree(directory, ignore_errors=True)
    finally:
        signal.signal(signal.SIGINT, previous)


def prune(cache: Path, dry_run: bool, subnet: ipaddress.IPv4Network = hostnet.NET_BASE) -> list[str]:
    """Remove (or with dry_run, only list) leftovers of dead VMs; returns what was or would be done."""
    registry = VmRegistry(cache)
    state_dirs = sorted(p for p in registry.root.iterdir() if p.is_dir()) if registry.root.is_dir() else []
    plan = make_plan(read_host_state(registry.root), registry.live(), state_dirs, subnet)
    if plan and not dry_run:
        execute(plan)
    return plan.descriptions()
