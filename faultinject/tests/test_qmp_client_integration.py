#!/usr/bin/env python3
"""Integration test for the control plane against a *fake* QMP server.

Runs the real QMPClient against a minimal in-process QMP protocol server, so
the control plane can be validated without a full QEMU build/guest boot.
Covers: handshake, qom-set/get, set_link, inject-nmi, watchdog-set-action,
HMP wrapper, AER command-line formatting, LTPI fault paths, status polling.

Run:  .tools/py/python.exe -m pytest faultinject/tests/ -v
"""
import json
import os
import socket
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qmp_client import QMPClient  # noqa: E402


class FakeQMPServer:
    """Tiny QMP server: greeting, qmp_capabilities, echo commands."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.state = {"qom": {}, "commands": []}
        self._th = threading.Thread(target=self._serve, daemon=True)
        self._th.start()

    def _send(self, conn, obj):
        conn.sendall((json.dumps(obj) + "\r\n").encode())

    def _serve(self):
        conn, _ = self.sock.accept()
        with conn:
            self._send(conn, {"QMP": {"version": {"qemu": {"micro": 0,
                "minor": 1, "major": 11}}, "capabilities": []}})
            buf = b""
            while True:
                data = conn.recv(65536)
                if not data:
                    return
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    req = json.loads(line)
                    self.state["commands"].append(req)
                    self._reply(conn, req)

    def _reply(self, conn, req):
        name = req["execute"]
        args = req.get("arguments", {})
        if name == "qmp_capabilities":
            self._send(conn, {"return": {}})
        elif name == "qom-set":
            self.state["qom"][(args["path"], args["property"])] = args["value"]
            self._send(conn, {"return": {}})
        elif name == "qom-get":
            self._send(conn, {"return": self.state["qom"].get(
                (args["path"], args["property"]), 0)})
        elif name == "query-status":
            self._send(conn, {"return": {"status": "running",
                                         "singlestep": False,
                                         "running": True}})
        elif name == "human-monitor-command":
            self._send(conn, {"return": args["command-line"]})
        elif name == "quit":
            self._send(conn, {"return": {}})
            raise SystemExit
        else:
            self._send(conn, {"return": {}})


@pytest.fixture
def qmp():
    srv = FakeQMPServer()
    c = QMPClient(f"tcp:127.0.0.1:{srv.port}")
    c.connect()
    yield c, srv
    try:
        c.close()
    except Exception:
        pass


def test_handshake(qmp):
    c, srv = qmp
    assert srv.state["commands"][0]["execute"] == "qmp_capabilities"


def test_qom_set_get(qmp):
    c, _ = qmp
    c.qom_set("/machine/peripheral/temp-mb", "temperature", 18000)
    assert c.qom_get("/machine/peripheral/temp-mb", "temperature") == 18000


def test_fault_commands(qmp):
    c, srv = qmp
    c.set_link("net0", False)
    c.inject_nmi()
    c.watchdog_set_action("inject-nmi")
    c.device_del("nvme0")
    names = [r["execute"] for r in srv.state["commands"]]
    assert "set_link" in names and "inject-nmi" in names
    assert "watchdog-set-action" in names and "device_del" in names


def test_aer_hmp_formatting(qmp):
    c, _ = qmp
    out = c.aer_inject("nvme0", "0x4000")
    assert out == "pcie_aer_inject_error nvme0 0x4000"
    out = c.aer_inject("nvme0", "0x0", correctable=True)
    assert out == "pcie_aer_inject_error -c nvme0 0x0"
    out = c.aer_inject("nic0", "0x1", header=(1, 2, 3, 4))
    assert out == "pcie_aer_inject_error nic0 0x1 1 2 3 4"


def test_ltpi_fault_paths(qmp):
    c, _ = qmp
    c.ltpi_link_down(0, True)
    assert c.qom_get("/machine/soc/ltpi-ctrl[0]", "link-down") is True
    c.ltpi_fault_code(1, 0xDEAD)
    assert c.qom_get("/machine/soc/ltpi-ctrl[1]", "fault-code") == 0xDEAD


def test_status_poll(qmp):
    c, _ = qmp
    assert c.status()["status"] == "running"
