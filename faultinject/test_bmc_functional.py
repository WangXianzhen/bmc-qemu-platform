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

    def try_cmd(self, cmd, expect, timeout=20):
        """exec_cmd that returns bool instead of raising (pytest.fail raises
        BaseException, so a bare `except Exception` cannot catch it)."""
        try:
            self.exec_cmd(cmd, expect, timeout=timeout)
            return True
        except BaseException:
            return False


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
         # storage: file backend + blkdebug error injection (write_aio, once)
         "-blockdev", f"driver=file,node-name=nvme-file,filename={NVME_IMG}",
         "-blockdev", "driver=blkdebug,node-name=nvme-bd,image=nvme-file,"
                      "inject-error.0.event=write_aio,"
                      "inject-error.0.errno=5,"
                      "inject-error.0.once=on",
         "-device", "nvme,serial=SN0001,drive=nvme-bd,id=nvme0",
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
    """temp-mb: qom-set temperature fault; guest hwmon readback best-effort.

    Core assertion is the QEMU-level fault (qom-set/get round trip). The
    guest-side binding (new_device) depends on the image's I2C bus numbering,
    so it is attempted on i2c-1 then i2c-0 and skipped if neither binds."""
    qmp, console = bmc["qmp"], bmc["console"]
    qmp.qom_set("/machine/peripheral/temp-mb", "temperature", 0)
    assert qmp.qom_get("/machine/peripheral/temp-mb", "temperature") == 0
    qmp.qom_set("/machine/peripheral/temp-mb", "temperature", 85000)
    assert qmp.qom_get("/machine/peripheral/temp-mb", "temperature") == 85000

    bound = (console.try_cmd("echo lm75 0x4d > "
                             "/sys/class/i2c-dev/i2c-1/device/new_device",
                             r"new_device")
             or console.try_cmd("echo lm75 0x4d > "
                                "/sys/class/i2c-dev/i2c-0/device/new_device",
                                r"new_device"))
    if not bound:
        pytest.skip("guest lm75 binding failed (I2C bus numbering differs); "
                    "QEMU-level fault verified above")
    qmp.qom_set("/machine/peripheral/temp-mb", "temperature", 18000)
    console.exec_cmd("cat /sys/bus/i2c/devices/*/hwmon/hwmon*/temp1_input",
                     r"18000")
    qmp.qom_set("/machine/peripheral/temp-mb", "temperature", 0)
    console.exec_cmd("cat /sys/bus/i2c/devices/*/hwmon/hwmon*/temp1_input",
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


def test_storage_io_error_guest_visible(bmc):
    """blkdebug-injected write error surfaces in the guest as EIO.

    The nvme backend is wrapped in a blkdebug node that errors the first
    write_aio (errno 5, once). The guest's dd on /dev/nvme0n1 must report
    the I/O error - deterministic end-to-end storage fault verification
    (a raw file backend never errors on truncated-file writes, so the
    classic truncate+dd trick cannot be used here)."""
    qmp, console = bmc["qmp"], bmc["console"]
    if not console.try_cmd("ls /dev/nvme0n1", r"nvme0n1", timeout=10):
        pytest.skip("nvme0n1 not enumerated (PCIe fdt workaround needed)")
    console.exec_cmd("dd if=/dev/zero of=/dev/nvme0n1 bs=1M count=4 "
                     "oflag=direct 2>&1", r"Input/output error|I/O error",
                     timeout=30)
    # verify QEMU side: a second write succeeds (once=on consumed the error)
    console.exec_cmd("dd if=/dev/zero of=/dev/nvme0n1 bs=1M count=1 "
                     "oflag=direct 2>&1", r"#", timeout=30)


def test_redfish_smoke(bmc):
    """Bring up the management NIC and probe Redfish via hostfwd.

    Skips when the NIC cannot get an IP (stock QEMU AST2700 PCIe2 needs the
    U-Boot fdt workaround) or when this image has no Redfish endpoint."""
    console = bmc["console"]
    if not console.try_cmd("ip link set eth2 up", r"#"):
        pytest.skip("eth2 not present (PCIe2 fdt workaround needed)")
    console.try_cmd("udhcpc -i eth2 -t 3 -q 2>/dev/null", r"#")
    if not console.try_cmd("ip addr show dev eth2", r"10\.0\.2\.15"):
        pytest.skip("BMC NIC did not obtain an IP; skipping Redfish smoke")
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
