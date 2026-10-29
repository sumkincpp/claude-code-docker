"""Host session code against the real guest agent, joined by a Unix socket that stands in for Firecracker's vsock bridge."""

import importlib.machinery
import importlib.util
import socket
import sys
import threading
from pathlib import Path

import pytest

from ccd.backends.firecracker import session

AGENT = Path(session.__file__).parent / "assets" / "ccd-agent"


@pytest.fixture
def agent(monkeypatch):
    loader = importlib.machinery.SourceFileLoader("ccd_agent", str(AGENT))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("ccd_agent", loader))
    loader.exec_module(module)
    # No su or login users here: run the requested command directly
    monkeypatch.setattr(module, "session_argv", lambda request: request["argv"])
    return module


@pytest.fixture
def uds(agent, tmp_path):
    """Firecracker's per-VM socket: answer 'CONNECT <port>' with 'OK', then hand the connection to the agent."""
    path = tmp_path / "v.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(8)

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            assert session._read_line(conn).startswith(b"CONNECT")
            conn.sendall(b"OK 1024\n")
            threading.Thread(target=agent.handle, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    yield path
    server.close()


def run(uds, command, tmp_path, monkeypatch, stdin_text=""):
    """run_interactive with files for stdin/stdout (so there is no terminal, like `ccd . -- cmd < in > out`)."""
    stdin, stdout = tmp_path / "stdin", tmp_path / "stdout"
    stdin.write_text(stdin_text)
    with open(stdin) as fin, open(stdout, "w") as fout:
        monkeypatch.setattr(sys, "stdin", fin)
        monkeypatch.setattr(sys, "stdout", fout)
        code = session.run_interactive(uds, False, command)
    return code, stdout.read_text()


def test_command_output_and_exit_code_come_back(uds, tmp_path, monkeypatch):
    code, out = run(uds, ["sh", "-c", "echo out; echo err >&2; exit 3"], tmp_path, monkeypatch)

    assert code == 3
    assert sorted(out.split()) == ["err", "out"]  # stdout and stderr are merged, with plain newlines (no pty translation)


def test_input_reaches_the_command_and_end_of_input_ends_it(uds, tmp_path, monkeypatch):
    code, out = run(uds, ["tr", "a-z", "A-Z"], tmp_path, monkeypatch, stdin_text="hello\nworld\n")

    assert (code, out) == (0, "HELLO\nWORLD\n")  # tr only finishes because the end of stdin was passed on


def test_a_failing_command_reports_its_own_code_and_a_killed_one_128_plus_signal(uds, tmp_path, monkeypatch):
    assert run(uds, ["sh", "-c", "exit 42"], tmp_path, monkeypatch)[0] == 42
    assert run(uds, ["sh", "-c", "kill -9 $$"], tmp_path, monkeypatch)[0] == 137


def test_unknown_session_status_is_not_a_success(uds):
    assert session.exit_status(uds, 99999) == 255


def test_an_agent_from_before_commands_is_refused_instead_of_opening_a_shell(tmp_path, monkeypatch):
    """Old agents answer 'OK <sid>', ignore the command and start a login shell. The host must not leave you in one."""
    from ccd.plugin import BackendError

    path = tmp_path / "old.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    seen = []

    def old_agent():
        conn, _ = server.accept()
        session._read_line(conn)  # CONNECT
        conn.sendall(b"OK 1024\n")
        seen.append(session._read_line(conn))  # the session request
        conn.sendall(b"OK 1\n")  # no feature list
        seen.append(conn.recv(1))  # the host hangs up: that is what ends the (shell) session in the old agent
        conn.close()

    thread = threading.Thread(target=old_agent, daemon=True)
    thread.start()

    with pytest.raises(BackendError, match="too old to run commands"):
        run(path, ["uname", "-a"], tmp_path, monkeypatch)

    thread.join(timeout=5)
    assert seen[1] == b""  # the connection was closed, so the old agent's shell gets SIGHUP


def test_an_old_agent_is_fine_for_an_interactive_shell(tmp_path, monkeypatch):
    path = tmp_path / "old.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)

    def old_agent():
        conn, _ = server.accept()
        session._read_line(conn)
        conn.sendall(b"OK 1024\n")
        session._read_line(conn)
        conn.sendall(b"OK 1\n")
        conn.close()
        server.close()  # later connections (the status request) fail at once instead of waiting for a timeout

    threading.Thread(target=old_agent, daemon=True).start()

    # no command: the old reply format is accepted (the status request then gets no answer: 255)
    assert run(path, None, tmp_path, monkeypatch)[0] == 255
