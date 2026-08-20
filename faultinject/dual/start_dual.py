#!/usr/bin/env python3
"""Dual-QEMU linkage skeleton: x86 host guest + AST2700 BMC.

Scenario under test: host CPU/DIMM faults (MCE) must be perceived by the BMC
via out-of-band channels (PECI / IPMB).

Two QEMU instances:
  * host: qemu-system-x86_64, server guest with QEMU's IPMI BMC emulation
          (-device ipmi-bmc-sim + isa-ipmi-kcs) so the guest sees a BMC,
          and HMP `mce` injection for CPU/DIMM faults.
  * bmc:  qemu-system-aarch64 -M ast2700-evb (the DUT, OpenBMC firmware).

Bridge (QMP-to-QMP, implemented here as the skeleton):
  host MCE / IPMI sensor events  ->  bmc PECI fault properties (patch P4:
  /machine/soc/peci host-lost / temp-fault), so the BMC's host-monitoring
  logic reacts as if the host reported a fault.

Notes: this is a test harness skeleton - the *real* wire protocol bridge
(IPMB-over-socket, PECI wire) is future work (design doc M4).
"""
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qmp_client import QMPClient  # noqa: E402
import ipmb_bridge  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
QEMU_X86 = os.environ.get("QEMU_X86", os.path.join(REPO, "qemu-master", "build-mingw",
                                                   "qemu-system-x86_64.exe"))
QEMU_ARM = os.environ.get("QEMU_ARM", os.path.join(REPO, "qemu-master", "build-mingw",
                                                   "qemu-system-aarch64.exe"))
HOST_IMG = os.environ.get("HOST_IMG", "")          # x86 guest disk (qcow2)
BMC_IMG = os.environ.get("BMC_IMG", "ast2700-default/image-bmc")
HOST_QMP = os.environ.get("HOST_QMP", "tcp:127.0.0.1:4460")
BMC_QMP = os.environ.get("BMC_QMP", "tcp:127.0.0.1:4461")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "9000"))


def launch_host():
    """x86 server guest with the external-BMC bridge as its IPMI BMC.

    QEMU's ipmi-bmc-extern talks the OpenIPMI lanserv 'VM' protocol; the
    IPMB bridge (ipmb_bridge.py) answers as the BMC, optionally forwarding
    to the real BMC's ipmid socket. The guest kernel (ipmi_si/KCS) will
    discover a healthy BMC and issue Get Device ID etc."""
    args = [QEMU_X86, "-machine", "q35", "-smp", "4", "-m", "4G",
            "-chardev", f"socket,id=ipmi0,host=127.0.0.1,port={BRIDGE_PORT}",
            "-device", "ipmi-bmc-extern,id=bmc0,chardev=ipmi0",
            "-device", "isa-ipmi-kcs,bmc=bmc0",
            "-qmp", HOST_QMP + ",server=on,wait=off",
            "-display", "none"]
    if HOST_IMG:
        args += ["-drive", f"file={HOST_IMG},format=qcow2,if=virtio"]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def launch_bmc():
    args = [QEMU_ARM, "-machine", "ast2700-evb", "-smp", "4", "-m", "2G",
            "-drive", f"file={BMC_IMG},format=raw,if=mtd",
            "-qmp", BMC_QMP + ",server=on,wait=off",
            "-display", "none", "-serial", "stdio", "-monitor", "none"]
    return subprocess.Popen(args, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def bridge_host_mce_to_bmc(host, bmc):
    """Inject an x86 MCE into the host guest, then raise the matching PECI
    fault on the BMC (as the BMC would see via PECI host-status)."""
    print("== injecting host MCE ==")
    host.hmp("mce 0 0 0xbd80000000100034 0x0 0x0 0x0")
    print("== signaling BMC via PECI temp-fault (P4) ==")
    bmc.qom_set("/machine/soc/peci", "temp-fault", True)
    time.sleep(0.5)
    bmc.qom_set("/machine/soc/peci", "temp-fault", False)
    print("bridge done")


def main():
    procs = []
    bridge = None
    try:
        # IPMB bridge: answers the host guest's BMC (mock mode by default;
        # pass --forward to relay to the real BMC's ipmid socket)
        bridge = threading.Thread(
            target=ipmb_bridge.serve,
            args=(("127.0.0.1", BRIDGE_PORT), None, print),
            daemon=True)
        bridge.start()
        time.sleep(0.5)

        print("launching host x86 guest ...")
        procs.append(launch_host())
        print("launching AST2700 BMC ...")
        procs.append(launch_bmc())

        host = QMPClient(HOST_QMP)
        host.connect()
        bmc = QMPClient(BMC_QMP)
        bmc.connect()
        print("both QMP connected")

        bridge_host_mce_to_bmc(host, bmc)
        print("status: host=", host.status()["status"],
              "bmc=", bmc.status()["status"])
        host.close()
        bmc.close()
    finally:
        for p in procs:
            p.terminate()
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()


if __name__ == "__main__":
    main()
