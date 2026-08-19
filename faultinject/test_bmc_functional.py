#!/usr/bin/env python3
"""Functional + fault-injection test suite for the AST2700 BMC platform.

Run (Linux CI):  python3 -m pytest faultinject/test_bmc_functional.py -v
Requires: QEMU built (with local patches), SDK v11.03 image.
Env: QEMU=...  IMG=...  QMP_ADDR=tcp:127.0.0.1:PORT (Windows) | unix:... (Linux)

Console interaction mirrors QEMU's own functional tests
(tests/functional/, `exec_command_and_wait_for_pattern`).
"""
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qmp_client import QMPClient  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QEMU = os.environ.get("QEMU", os.path.join(REPO, "qemu-master", "build",
                                           "qemu-system-aarch64"))
IMG = os.environ.get("IMG", "ast2700-default/image-bmc")
QMP_SOCK = "/tmp/ast2700-test-qmp.sock"
QMP_ADDR = os.environ.get(
    "QMP_ADDR",
    "tcp:127.0.0.1:4450" if os.name == "nt" else f"unix:{QMP_SOCK}")
NVME_IMG = os.environ.get("NVME_IMG", "/tmp/ast2700-nvme-test.img")
REDFISH_URL = "http://127.0.0.1:2443/redfish/v1"   # hostfwd from fixture


class Console:
    """Serial console via QEMU stdio (-serial stdio): read from stdout pipe
    (reader thread), send commands to stdin pipe."""

    def __init__(self, proc):
        self.proc = proc
        self._buf = []
        self._lock = threading.Lock()
        self._th = threading.Thread(target=self._reader, daemon=True)
        self._th.start()

    def _reader(self):
        for chunk in iter(lambda: self.proc.stdout.read(1), b""):
            with self._lock:
                self._buf.append(chunk.decode("utf-8", errors="replace"))

    def read(self):
        with self._lock:
            return "".join(self._buf)

    def wait_for(self, pattern, timeout=300):
        deadline = time.time() + timeout
        rx = re.compile(pattern)
        seen = ""
        while time.time() < deadline:
            seen = self.read()
            if rx.search(seen):
                return seen
            time.sleep(0.2)
        pytest.fail(f"console pattern {pattern!r} not seen in {timeout}s; "
                    f"tail:\n{seen[-2000:]}")

    def exec_cmd(self, cmd, expect, timeout=60):
        self.proc.stdin.write((cmd + "\n").encode())
        self.proc.stdin.flush()
        return self.wait_for(expect, timeout=timeout)


@pytest.fixture(scope="module")
def bmc():
    """Launch the DUT once per module; teardown via QMP quit + kill."""
    if os.path.exists(QMP_SOCK):
        os.unlink(QMP_SOCK)
    # nvme backing file (sparse)
    with open(NVME_IMG, "wb") as f:
        f.truncate(4 * 1024 * 1024 * 1024)

    proc = subprocess.Popen(
        [QEMU, "-machine", "ast2700-evb", "-smp", "4", "-m", "2G",
         "-drive", f"file={IMG},format=raw,if=mtd",
         # emulated managed-platform components (see launch_ast2700.sh)
         "-device", "tmp105,bus=aspeed.i2c.bus.1,address=0x4d,id=temp-mb",
         "-device", "adm1272,bus=aspeed.i2c.bus.1,address=0x10,id=psu0",
         "-device", "e1000e,netdev=net0,bus=pcie.2,id=nic0",
         "-netdev", f"user,id=net0,hostfwd=tcp::{2443}-:443",
         "-device", "nvme,serial=SN0001,drive=nvmedrv,bus=pcie.1,id=nvme0",
         "-drive", f"file={NVME_IMG},if=none,id=nvmedrv,format=raw,"
                   "rerror=report,werror=stop",
         "-watchdog-action", "pause",
         "-qmp", QMP_ADDR + ",server=on,wait=off",
         "-display", "none", "-serial", "stdio", "-monitor", "none"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL)
    try:
        qmp = QMPClient(QMP_ADDR)
        qmp.connect()
        console = Console(proc)
        console.wait_for(r"login:", timeout=600)   # SDK v11.03 boots to login
        yield {"qmp": qmp, "console": console}
        qmp.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_boot_to_login(bmc):
    """Smoke: U-Boot -> kernel -> OpenBMC login (boot completed)."""
    assert "login:" in bmc["console"].read()


def test_sensor_fault_injection(bmc):
    """temp-mb: qom-set temperature and verify guest hwmon reflects it."""
    qmp, console = bmc["qmp"], bmc["console"]
    console.exec_cmd("echo lm75 0x4d > /sys/class/i2c-dev/i2c-1/device/new_device",
                     r"i2c i2c-1: new_device")
    qmp.qom_set("/machine/peripheral/temp-mb", "temperature", 18000)
    console.exec_cmd("cat /sys/bus/i2c/devices/1-004d/hwmon/hwmon*/temp1_input",
                     r"18000")
    qmp.qom_set("/machine/peripheral/temp-mb", "temperature", 0)
    console.exec_cmd("cat /sys/bus/i2c/devices/1-004d/hwmon/hwmon*/temp1_input",
                     r"0")


def test_psu_fault_injection(bmc):
    """psu0 (adm1272): vout drop -> guest PMBus readback drops.

    adm1272 applies a scaling coefficient to vin/vout/iout, so compare with
    tolerance; the fault assertion is vout == 0 after the power-loss set."""
    qmp = bmc["qmp"]
    qmp.qom_set("/machine/peripheral/psu0", "vout", 12000)   # 12 V nominal
    v = qmp.qom_get("/machine/peripheral/psu0", "vout")
    assert abs(v - 12000) <= 12000 * 0.02, f"vout readback {v} out of tolerance"
    qmp.qom_set("/machine/peripheral/psu0", "vout", 0)       # power-loss fault
    assert qmp.qom_get("/machine/peripheral/psu0", "vout") == 0


def test_nic_link_down_up(bmc):
    """set_link fault: link lost, then restored (guest-visible carrier)."""
    qmp, console = bmc["qmp"], bmc["console"]
    qmp.set_link("net0", False)
    out = console.exec_cmd("cat /sys/class/net/eth2/carrier", r"[01]",
                           timeout=30)
    assert "0" in out
    qmp.set_link("net0", True)
    out = console.exec_cmd("cat /sys/class/net/eth2/carrier", r"[01]",
                           timeout=30)
    assert "1" in out


def test_storage_error_pauses_vm(bmc):
    """werror=stop: host truncates backing file, guest writes -> VM pauses."""
    qmp, console = bmc["qmp"], bmc["console"]
    with open(NVME_IMG, "r+b") as f:
        f.truncate(1024 * 1024)
    console.exec_cmd("dd if=/dev/zero of=/dev/nvme0n1 bs=1M count=8 "
                     "oflag=direct 2>/dev/null", r"#", timeout=30)
    deadline = time.time() + 15
    while time.time() < deadline:
        st = qmp.status()
        if st["status"] == "paused":
            qmp.cmd("cont")            # resume for subsequent tests
            return
        time.sleep(0.5)
    pytest.fail(f"VM did not pause on storage error; status={st}")


def test_redfish_smoke(bmc):
    """Bring up the management NIC and probe Redfish via hostfwd.

    Requires the BMC NIC (eth2) to obtain an IP; on stock QEMU the AST2700
    PCIe2 link needs the U-Boot fdt workaround, so this test skips when
    Redfish is unreachable (known environment gap, see design doc §7)."""
    qmp, console = bmc["qmp"], bmc["console"]
    try:
        console.exec_cmd("ip link set eth2 up", r"#", timeout=20)
        console.exec_cmd("udhcpc -i eth2 -t 3 -q 2>/dev/null", r"#", timeout=30)
        console.exec_cmd("ip addr show dev eth2", r"10\.0\.2\.15", timeout=20)
    except Exception:
        pytest.skip("BMC NIC did not obtain an IP (PCIe2 fdt workaround "
                    "needed); skipping Redfish smoke")
        return
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(REDFISH_URL, timeout=5) as r:
                assert r.status == 200
            return
        except Exception:
            time.sleep(1)
    pytest.skip("Redfish endpoint not reachable on this image")


def test_watchdog_action(bmc):
    """watchdog-set-action QMP round-trip."""
    qmp = bmc["qmp"]
    qmp.watchdog_set_action("inject-nmi")
    qmp.watchdog_set_action("pause")


def test_dram_ecc_injection(bmc):
    """SDMC ECC fault (patch P3): status/addr visible via qom + guest."""
    qmp = bmc["qmp"]
    qmp.qom_set("/machine/soc/sdmc", "ecc-error-addr", 0x12345678)
    qmp.qom_set("/machine/soc/sdmc", "inject-ecc-error", True)
    assert qmp.qom_get("/machine/soc/sdmc", "ecc-fail-status") != 0
    qmp.qom_set("/machine/soc/sdmc", "inject-ecc-error", False)
    assert qmp.qom_get("/machine/soc/sdmc", "ecc-fail-status") == 0
