#!/usr/bin/env python3
"""Control-plane verification against a *fake* QMP server (no QEMU needed).

Runs the real QMPClient against a minimal in-process QMP server to validate:
handshake, qom-set/get, set_link, inject-nmi, watchdog-set-action, device_del,
HMP/AER command-line formatting, LTPI fault paths, status polling.

Usage:  .tools/py/python.exe faultinject/tests/verify_control_plane.py
Exit code 0 = all checks passed.
"""
import json
import os
import socket
import sys
import threading

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
                    if not self._reply(conn, req):
                        return

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
            return False
        else:
            self._send(conn, {"return": {}})
        return True


def check(name, cond, extra=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({extra})" if extra and not cond else ""))
    return cond


def main():
    srv = FakeQMPServer()
    c = QMPClient(f"tcp:127.0.0.1:{srv.port}")
    c.connect()

    results = []
    results.append(check("handshake qmp_capabilities",
                         srv.state["commands"][0]["execute"] == "qmp_capabilities"))
    c.qom_set("/machine/peripheral/temp-mb", "temperature", 18000)
    results.append(check("qom-set/get round-trip",
                         c.qom_get("/machine/peripheral/temp-mb", "temperature") == 18000))
    c.set_link("net0", False)
    c.inject_nmi()
    c.watchdog_set_action("inject-nmi")
    c.device_del("nvme0")
    names = [r["execute"] for r in srv.state["commands"]]
    results.append(check("fault commands dispatched",
                         all(n in names for n in ("set_link", "inject-nmi",
                                                  "watchdog-set-action",
                                                  "device_del"))))
    out = c.aer_inject("nvme0", "0x4000")
    results.append(check("AER HMP formatting (uncor)",
                         out == "pcie_aer_inject_error nvme0 0x4000"))
    out = c.aer_inject("nvme0", "0x0", correctable=True)
    results.append(check("AER HMP formatting (-c)",
                         out == "pcie_aer_inject_error -c nvme0 0x0"))
    out = c.aer_inject("nic0", "0x1", header=(1, 2, 3, 4))
    results.append(check("AER HMP formatting (header)",
                         out == "pcie_aer_inject_error nic0 0x1 1 2 3 4"))
    c.ltpi_link_down(0, True)
    results.append(check("LTPI link-down qom path",
                         c.qom_get("/machine/soc/ltpi-ctrl[0]", "link-down") is True))
    c.ltpi_fault_code(1, 0xDEAD)
    results.append(check("LTPI fault-code qom path",
                         c.qom_get("/machine/soc/ltpi-ctrl[1]", "fault-code") == 0xDEAD))
    results.append(check("status poll", c.status()["status"] == "running"))

    try:
        c.close()
    except Exception:
        pass

    ok = all(results)
    print(f"\n{'ALL CHECKS PASSED' if ok else 'SOME CHECKS FAILED'} "
          f"({sum(results)}/{len(results)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
