#!/usr/bin/env python3
"""Minimal QMP client (stdlib only) for the BMC fault-injection control plane.

Protocol reference: https://raw.githubusercontent.com/qemu/qemu/master/docs/interop/qmp-spec.txt
Verified against QEMU master fa19879d / 11.1.0.
"""
import json
import socket
import time


class QMPError(Exception):
    """Raised when QEMU replies with an error to a QMP command."""


class QMPClient:
    def __init__(self, path, timeout=10.0):
        """path: 'unix:/path/to/sock' or 'tcp:127.0.0.1:PORT'."""
        self.path = path
        self.timeout = timeout
        self.sock = None
        self._buf = ""

    # -- transport ---------------------------------------------------------
    def _open_socket(self):
        if self.path.startswith("tcp:"):
            host, port = self.path[4:].rsplit(":", 1)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((host, int(port)))
            return s
        if self.path.startswith("unix:"):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(self.path[5:])
            return s
        raise QMPError(f"unsupported QMP address: {self.path!r}")

    def connect(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                self.sock = self._open_socket()
                self.sock.settimeout(self.timeout)
                break
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                if time.time() > deadline:
                    raise
                time.sleep(0.2)
        # consume greeting: {"QMP": {"version": ...}}
        self._recv_msg()
        # handshake
        self.cmd("qmp_capabilities")

    def close(self):
        if self.sock is not None:
            try:
                self.cmd("quit")
            except Exception:
                pass
            self.sock.close()
            self.sock = None

    def _recv_msg(self):
        """Return one JSON object from the stream; buffered across recv() calls.

        Uses raw_decode so events + responses arriving in one chunk are
        consumed one object at a time instead of hanging on concatenated JSON.
        """
        decoder = json.JSONDecoder()
        while True:
            self._buf = self._buf.lstrip()
            if self._buf:
                try:
                    obj, end = decoder.raw_decode(self._buf)
                    self._buf = self._buf[end:]
                    return obj
                except (ValueError, json.JSONDecodeError):
                    pass  # incomplete object; need more data
            chunk = self.sock.recv(65536)
            if not chunk:
                raise QMPError("QMP connection closed by QEMU")
            self._buf += chunk.decode("utf-8", errors="replace")

    def _send(self, obj):
        # QMP requires newline-terminated JSON commands (CRLF per spec;
        # QEMU accepts either).
        self.sock.sendall(json.dumps(obj).encode("utf-8") + b"\r\n")

    # -- commands ----------------------------------------------------------
    def cmd(self, execute, arguments=None):
        """Execute a QMP command; returns the 'return' payload."""
        req = {"execute": execute}
        if arguments is not None:
            req["arguments"] = arguments
        self._send(req)
        while True:
            msg = self._recv_msg()
            if "error" in msg:
                err = msg["error"]
                raise QMPError(f"{execute}: {err.get('class')}: {err.get('desc')}")
            if "return" in msg:
                return msg["return"]
            # otherwise it's an event; ignore for now

    def hmp(self, command_line):
        """Run an HMP command through QMP (needed e.g. for x86 `mce`)."""
        return self.cmd("human-monitor-command", {"command-line": command_line})

    # -- convenience wrappers for the fault catalogue ----------------------
    def qom_set(self, path, prop, value):
        return self.cmd("qom-set", {"path": path, "property": prop, "value": value})

    def qom_get(self, path, prop):
        return self.cmd("qom-get", {"path": path, "property": prop})

    def set_link(self, netdev_id, up):
        """Network link fault: up=False == link down (qapi/net.json `set_link`)."""
        return self.cmd("set_link", {"name": netdev_id, "up": up})

    def device_del(self, device_id):
        """PCIe/device hot-unplug fault."""
        return self.cmd("device_del", {"id": device_id})

    def inject_nmi(self):
        """Deliver NMI to the guest (GICv3 NMI is wired on ast27x0)."""
        return self.cmd("inject-nmi")

    def watchdog_set_action(self, action):
        """action: 'reset'|'shutdown'|'poweroff'|'pause'|'debug'|'none'|'inject-nmi'."""
        return self.cmd("watchdog-set-action", {"action": action})

    def throttle(self, drive_id=None, bps=0, iops=0, bps_rd=0, bps_wr=0,
                 iops_rd=0, iops_wr=0):
        """Storage performance fault injection.

        Modern QEMU (>=7.0) removed blockdev-set-io-throttle; the mechanism is
        a throttle-group object (qapi/qom.json 'throttle-group') applied to a
        block node via the 'throttle' driver. This helper creates the group
        with the requested limits (boxed ObjectOptions, flattened props).
        """
        limits = {}
        for name, val in (("bps", bps), ("iops", iops), ("bps-read", bps_rd),
                          ("bps-write", bps_wr), ("iops-read", iops_rd),
                          ("iops-write", iops_wr)):
            if val:
                limits[name] = val
        tg = f"tg-perf-{id(limits)}"
        self.cmd("object-add", {"qom-type": "throttle-group", "id": tg,
                                "limits": limits})
        return tg

    def blockdev_reopen(self, options):
        """Reopen (reconfigure) a block node at runtime.

        Used to re-arm blkdebug error injection: reopen the node with a fresh
        inject-error rule so the NEXT request deterministically errors.
        QAPI expects an array of BlockdevOptions.
        """
        return self.cmd("blockdev-reopen", {"options": [options]})

    def status(self):
        return self.cmd("query-status")

    def qom_list(self, path="/"):
        return self.cmd("qom-list", {"path": path})

    # -- extensibility: injected QMP commands from local patches -----------
    def aer_inject(self, device_id, error_status="0x0", correctable=False,
                   advisory_non_fatal=False, header=None, prefix=None):
        """PCIe AER error injection via the stock HMP command
        `pcie_aer_inject_error` (hw/pci/pci-hmp-cmds.c:173, hmp-commands.hx).
        error_status: name string or numeric; header/prefix: 4-tuples of u32.
        """
        cl = "pcie_aer_inject_error"
        if correctable:
            cl += " -c"
        if advisory_non_fatal:
            cl += " -a"
        cl += f" {device_id} {error_status}"
        if header:
            cl += " " + " ".join(str(h) for h in header)
        if prefix:
            cl += " " + " ".join(str(p) for p in prefix)
        return self.hmp(cl)

    def ltpi_link_down(self, index, down):
        """[local patch P2] Force AST2700 LTPI link-management register to
        report link-down (bit 0 cleared on reads). index: 0/1 LTPI ctrl."""
        return self.qom_set(f"/machine/soc/ltpi-ctrl[{index}]", "link-down", down)

    def ltpi_fault_code(self, index, code):
        """[local patch P2] OR a protocol fault code into the LTPI
        fault-status register on reads (0 clears)."""
        return self.qom_set(f"/machine/soc/ltpi-ctrl[{index}]", "fault-code", code)
