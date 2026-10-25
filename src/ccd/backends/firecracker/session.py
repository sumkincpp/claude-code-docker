"""Host side of the guest agent (assets/ccd-agent): interactive sessions over Firecracker's vsock Unix socket.

Firecracker exposes the guest's vsock as a Unix socket on the host: connect, send "CONNECT <port>\\n", and it answers
"OK <host-port>\\n" once the guest is listening (otherwise it closes the connection). After that the stream is the agent's.
"""

import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path

from ccd.plugin import BackendError

AGENT_PORT = 1024


def _read_line(sock: socket.socket) -> bytes:
    buf = b""
    while not buf.endswith(b"\n") and len(buf) < 4096:
        chunk = sock.recv(1)
        if not chunk:
            break
        buf += chunk
    return buf


def connect(uds: Path, timeout: float = 3.0) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(uds))
        sock.sendall(f"CONNECT {AGENT_PORT}\n".encode())
        if not _read_line(sock).startswith(b"OK"):
            raise ConnectionError("guest agent not listening")
    except OSError:
        sock.close()
        raise
    sock.settimeout(None)
    return sock


def wait_for_agent(uds: Path, vmm: subprocess.Popen, timeout: float = 60.0) -> None:
    """Block until the agent accepts connections; fail fast if the VMM exits first."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if vmm.poll() is not None:
            raise BackendError(f"The VM exited (code {vmm.returncode}) before its agent came up")
        try:
            connect(uds, timeout=1.0).close()  # an empty connection is the readiness probe
            return
        except OSError:
            time.sleep(0.2)
    raise BackendError(f"Timed out after {timeout:.0f}s waiting for the VM's agent")


def _send(uds: Path, request: dict) -> None:
    """One-shot request on its own connection (resize, poweroff); the VM may already be gone, so errors are ignored."""
    try:
        with connect(uds) as sock:
            sock.sendall(json.dumps(request).encode() + b"\n")
    except OSError:
        pass


def exit_status(uds: Path, sid: int) -> int:
    """Exit code of an ended session; the agent keeps it, since the byte stream cannot carry it."""
    try:
        with connect(uds) as sock:
            sock.sendall(json.dumps({"op": "status", "sid": sid}).encode() + b"\n")
            reply = _read_line(sock).strip()
        return int(reply)
    except (OSError, ValueError):
        return 255  # the agent is gone or did not know the session


def poweroff(uds: Path) -> None:
    _send(uds, {"op": "poweroff"})


def _write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data) :]


def run_interactive(uds: Path, root: bool, command: list[str] | None = None) -> int:
    """Attach the local terminal to a new session in the guest until it ends; returns the session's exit code.

    Without `command` it is a login shell on a pty. With one, it runs that command, on a pty only when both our stdin and
    stdout are terminals; otherwise over pipes, so output can be piped or redirected and end of input reaches the command.
    """
    stdin_fd, stdout_fd = sys.stdin.fileno(), sys.stdout.fileno()
    is_tty = os.isatty(stdin_fd)
    use_pty = not command or (is_tty and os.isatty(stdout_fd))
    size = shutil.get_terminal_size() if os.isatty(stdout_fd) else None

    sock = connect(uds)
    request = {"op": "session", "user": "root" if root else "user", "term": os.environ.get("TERM", "linux"), "tty": use_pty}
    if command:
        request["argv"] = command
    if size:
        request.update(rows=size.lines, cols=size.columns)
    sock.sendall(json.dumps(request).encode() + b"\n")
    reply = _read_line(sock).split()
    if len(reply) < 2 or reply[0] != b"OK":
        sock.close()
        raise BackendError("The guest agent refused the session")
    sid = int(reply[1])
    if command and b"command" not in reply[2:]:
        # An agent from before one-off commands ignores them and starts a login shell: end that session instead of
        # letting the user sit in a shell that never ran their command
        sock.close()
        raise BackendError("The guest agent in the VM rootfs is too old to run commands; run 'ccd build' to update it")

    # Terminal in raw mode: we forward every byte unchanged (Enter stays a carriage return, Ctrl-C reaches the guest)
    saved = termios.tcgetattr(stdin_fd) if is_tty and use_pty else None
    if saved is not None:
        tty.setraw(stdin_fd)
    # SIGWINCH wakes select() through a socketpair so resizes are forwarded immediately
    wake_r, wake_w = socket.socketpair()
    wake_w.setblocking(False)
    previous_wakeup = signal.set_wakeup_fd(wake_w.fileno())
    previous_winch = signal.signal(signal.SIGWINCH, lambda *_: None)
    watching_stdin = True
    try:
        while True:
            readable, _, _ = select.select([sock, wake_r] + ([stdin_fd] if watching_stdin else []), [], [])
            if wake_r in readable and bytes([signal.SIGWINCH]) in wake_r.recv(1024):
                new = shutil.get_terminal_size()
                _send(uds, {"op": "resize", "sid": sid, "rows": new.lines, "cols": new.columns})
            if stdin_fd in readable:
                data = os.read(stdin_fd, 65536)
                if data:
                    sock.sendall(data)
                else:  # stdin closed (piped input): keep draining output
                    watching_stdin = False
                    if not use_pty:
                        sock.shutdown(socket.SHUT_WR)  # tells the command its input has ended
            if sock in readable:
                data = sock.recv(65536)
                if not data:
                    break
                _write_all(stdout_fd, data)
    finally:
        signal.signal(signal.SIGWINCH, previous_winch)
        signal.set_wakeup_fd(previous_wakeup)
        wake_r.close()
        wake_w.close()
        sock.close()
        if saved is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
    return exit_status(uds, sid)
