"""Host-side plumbing for one microVM: addressing, tap device, NAT and NFS exports.

Everything that needs root is collected into a single `sudo sh -c` script for setup and one for teardown,
so a run asks for the sudo password at most once.
"""

import ipaddress
import logging
import re
import shlex
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ccd.plugin import BackendError

logger = logging.getLogger(__name__)

# Each VM gets a /30 out of this block: .1 = host (tap), .2 = guest
# Docker hands out user-defined networks from 172.17.0.0/16 up to 172.31.0.0/16, so ccd stays out of that pool.
# Whatever the range, /30 blocks that overlap a route or address the host already has are skipped (see first_free_index).
NET_BASE = ipaddress.ip_network("10.111.111.0/24")
FALLBACK_DNS = ["1.1.1.1", "8.8.8.8"]


@dataclass
class Export:
    path: Path
    readonly: bool = False


@dataclass
class FileBind:
    """A single host file made available to the guest via a bind mount inside the exported staging dir."""

    source: Path
    staged_as: str
    readonly: bool = False


@dataclass
class VmNet:
    name: str
    index: int
    tap: str
    host_ip: str
    guest_ip: str
    uid: int
    gid: int
    iface: str
    dns: list[str] = field(default_factory=list)
    exports: list[Export] = field(default_factory=list)
    staging: Path | None = None
    file_binds: list[FileBind] = field(default_factory=list)

    @property
    def cidr(self) -> str:
        return f"{self.guest_ip}/30"

    @property
    def nft_table(self) -> str:
        return nft_table_name(self.name)


def nft_table_name(vm_name: str) -> str:
    return "ccd_" + re.sub(r"[^A-Za-z0-9]", "_", vm_name)


def addresses(index: int, base_net: ipaddress.IPv4Network = NET_BASE) -> tuple[str, str]:
    """(host_ip, guest_ip) of the index-th /30 of `base_net`."""
    base = int(base_net.network_address) + 4 * index
    if base + 3 > int(base_net.broadcast_address):
        raise BackendError("Too many Firecracker VMs running (address block exhausted)")
    return str(ipaddress.ip_address(base + 1)), str(ipaddress.ip_address(base + 2))


def parse_host_networks(routes: str, addrs: str) -> list[ipaddress.IPv4Network]:
    """Networks the host already uses: `ip -4 -o route show` and `ip -4 -o addr show` output. Our own taps are ignored."""
    nets: list[ipaddress.IPv4Network] = []
    for line in routes.splitlines():
        tokens = line.split()
        if not tokens or tokens[0] == "default" or re.search(r"\bdev ccdtap\d+\b", line):
            continue
        try:
            nets.append(ipaddress.ip_network(tokens[0], strict=False))
        except ValueError:
            continue
    for line in addrs.splitlines():
        match = re.search(r"\binet (\S+)", line)
        if match and not re.search(r"^\d+:\s+ccdtap\d+", line):
            nets.append(ipaddress.ip_network(match.group(1), strict=False))
    return nets


def host_networks() -> list[ipaddress.IPv4Network]:
    def ip(*args: str) -> str:
        return subprocess.run(["ip", "-4", "-o", *args], capture_output=True, text=True, check=False).stdout

    return parse_host_networks(ip("route", "show"), ip("addr", "show"))


def first_free_index(used: set[int], taken: list[ipaddress.IPv4Network] = (), base_net: ipaddress.IPv4Network = NET_BASE) -> int:
    """Lowest index that no live VM uses and whose /30 does not overlap anything in `taken`."""
    index = 0
    while True:
        if index not in used:
            start = int(base_net.network_address) + 4 * index
            block = ipaddress.ip_network((start, 30))
            if start + 3 > int(base_net.broadcast_address):
                raise BackendError(f"No free address block left in {base_net} (the rest overlaps the host's own networks)")
            if not any(block.overlaps(net) for net in taken):
                return index
        index += 1


def default_iface() -> str:
    """Interface carrying the host's default route (what the VM's traffic is NATed out of)."""
    out = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True, check=False).stdout
    match = re.search(r"\bdev (\S+)", out)
    if not match:
        raise BackendError("No default route found; the VM needs a host interface to NAT through")
    return match.group(1)


def host_dns() -> list[str]:
    """Host resolvers, skipping loopback stubs (127.0.0.53) that the guest cannot reach."""
    servers: list[str] = []
    try:
        for line in Path("/etc/resolv.conf").read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "nameserver" and not parts[1].startswith("127."):
                try:
                    if ipaddress.ip_address(parts[1]).version == 4:  # the VM has no IPv6 route
                        servers.append(parts[1])
                except ValueError:
                    continue  # e.g. fe80::1%wlan0
    except OSError:
        pass
    return servers or FALLBACK_DNS


def nft_ruleset(net: VmNet) -> str:
    return (
        f"table ip {net.nft_table} {{\n"
        # Chain names must not be nft keywords ("fwd", "post" etc. are reserved)
        # Any egress interface: a LAN resolver may be reached over a different interface than the default route
        f"  chain nat_out {{ type nat hook postrouting priority srcnat; ip saddr {net.guest_ip} oifname != \"{net.tap}\" masquerade; }}\n"
        f"  chain filter_forward {{ type filter hook forward priority filter; iifname \"{net.tap}\" accept;"
        f" oifname \"{net.tap}\" ct state established,related accept; }}\n"
        f"}}\n"
    )


def export_options(net: VmNet, readonly: bool, crossmnt: bool = False) -> str:
    opts = ["ro" if readonly else "rw", "sync", "no_subtree_check", "all_squash", f"anonuid={net.uid}", f"anongid={net.gid}"]
    if crossmnt:
        opts.append("crossmnt")
    return ",".join(opts)


def docker_user_rules(net: VmNet, action: str) -> list[str]:
    """Docker sets the FORWARD policy to DROP and, in nftables, an accept in our own chain cannot override another
    table's drop. DOCKER-USER is Docker's hook for user rules; add ours there when it exists. action: -I or -D."""
    tap = shlex.quote(net.tap)
    return [
        f"iptables {action} DOCKER-USER -i {tap} -j ACCEPT",
        f"iptables {action} DOCKER-USER -o {tap} -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
    ]


def setup_script(net: VmNet) -> str:
    q = shlex.quote
    lines = [
        "set -e",
        f"ip tuntap add dev {q(net.tap)} mode tap user {net.uid}",
        f"ip addr add {net.host_ip}/30 dev {q(net.tap)}",
        f"ip link set {q(net.tap)} up",
        "sysctl -qw net.ipv4.ip_forward=1",
        f"echo {q(nft_ruleset(net))} | nft -f -",
        "if command -v iptables >/dev/null 2>&1 && iptables -n -L DOCKER-USER >/dev/null 2>&1; then",
        *[f"  {rule}" for rule in docker_user_rules(net, "-I")],
        "fi",
    ]
    for export in net.exports:
        lines.append(f"exportfs -o {export_options(net, export.readonly)} {q(f'{net.guest_ip}:{export.path}')}")
    if net.staging is not None and net.file_binds:
        for bind in net.file_binds:
            target = net.staging / bind.staged_as
            lines.append(f"touch {q(str(target))}")
            lines.append(f"mount --bind {q(str(bind.source))} {q(str(target))}")
            if bind.readonly:
                lines.append(f"mount -o remount,ro,bind {q(str(target))}")
        lines.append(f"exportfs -o {export_options(net, False, crossmnt=True)} {q(f'{net.guest_ip}:{net.staging}')}")
    return "\n".join(lines)


def teardown_script(net: VmNet) -> str:
    """Best effort: every step runs even if an earlier one fails."""
    q = shlex.quote
    lines = [f"nft delete table ip {net.nft_table} 2>/dev/null"]
    lines += [f"{rule} 2>/dev/null" for rule in docker_user_rules(net, "-D")]
    for export in net.exports:
        lines.append(f"exportfs -u {q(f'{net.guest_ip}:{export.path}')} 2>/dev/null")
    if net.staging is not None and net.file_binds:
        lines.append(f"exportfs -u {q(f'{net.guest_ip}:{net.staging}')} 2>/dev/null")
        for bind in net.file_binds:
            lines.append(f"umount -l {q(str(net.staging / bind.staged_as))} 2>/dev/null")
    lines.append(f"ip link del {q(net.tap)} 2>/dev/null")
    return "\n".join(f"{line} || true" for line in lines)


def _sudo(script: str) -> None:
    logger.debug("sudo script:\n%s", script)
    subprocess.run(["sudo", "sh", "-c", script], check=True)


def check_nfs_server() -> None:
    for tool in ("exportfs", "nft"):
        if subprocess.run(["which", tool], capture_output=True, check=False).returncode != 0:
            raise BackendError(f"'{tool}' not found (install nfs-kernel-server / nftables)")
    active = subprocess.run(["systemctl", "is-active", "--quiet", "nfs-server"], check=False).returncode == 0
    if not active:
        raise BackendError("The NFS server is not running; start it with: sudo systemctl enable --now nfs-server")


def setup(net: VmNet) -> None:
    try:
        _sudo(setup_script(net))
    except subprocess.CalledProcessError as e:
        teardown(net)
        raise BackendError(f"Host network setup failed with exit code {e.returncode}") from e


def teardown(net: VmNet) -> None:
    # An impatient Ctrl-C here would kill the script halfway and leave exports, bind mounts and the tap behind; the child
    # inherits the ignored SIGINT
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        _sudo(teardown_script(net))
    except subprocess.CalledProcessError:
        logger.warning("Host network teardown reported errors for %s", net.name)
    finally:
        signal.signal(signal.SIGINT, previous)
