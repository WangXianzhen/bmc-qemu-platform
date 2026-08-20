#!/usr/bin/env python3
"""Local diagnostic: does the U-Boot PCIe2 fdt workaround actually execute?

Launches ast2700-evb with the SDK image, drives the U-Boot sequence from the
official QEMU functional test step by step, logs EVERY command + response,
then (after login) reports:
  - /proc/cmdline            (is cryptomgr.notests=1 present?)
  - PCIe devices (sysfs)     (did the fdt workaround enable them?)
  - /dev/nvme0n1, eth2       (are the platform components visible?)
"""
import os
import re
import socket
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
IS_WIN = os.name == "nt"
QEMU = os.environ.get(
    "QEMU_DIAG",
    os.path.join(REPO, "qemu-master",
                 "build-mingw/qemu-system-aarch64.exe" if IS_WIN
                 else "build-ci/qemu-system-aarch64"))
IMG = os.environ.get(
    "IMG_DIAG",
    os.path.join(REPO, "images", "ast2700-default-image", "image-bmc"))
PC_BIOS = os.environ.get(
    "PC_BIOS", os.path.join(REPO, "qemu-master", "pc-bios"))
NVME_IMG = os.environ.get(
    "NVME_IMG",
    os.path.join(REPO, "nvme.img" if IS_WIN else "/tmp/ast2700-nvme-diag.img"))
QMP = "tcp:127.0.0.1:4455"
CPORT = 4568
LOG = os.environ.get("DIAG_LOG", os.path.join(REPO, "diag-uboot.log"))

logf = open(LOG, "w", encoding="utf-8")


def log(msg):
    print(msg, flush=True)
    logf.write(msg + "\n")
    logf.flush()


class Console:
    def __init__(self, port):
        self._buf = []
        self._lock = threading.Lock()
        deadline = time.time() + 120
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            try:
                data = self.sock.recv(65536)
            except OSError:
                return
            if not data:
                return
            with self._lock:
                self._buf.append(data.decode("utf-8", errors="replace"))

    def read(self):
        with self._lock:
            return "".join(self._buf)

    def wait_for(self, pattern, timeout=300):
        rx = re.compile(pattern)
        deadline = time.time() + timeout
        while time.time() < deadline:
            seen = self.read()
            if rx.search(seen):
                return seen
            time.sleep(0.2)
        return None

    def cmd(self, cmd, expect, timeout=30):
        """Send a command, wait for the expected marker, log the delta."""
        before = len(self.read())
        self.sock.sendall((cmd + "\n").encode())
        res = self.wait_for(expect, timeout=timeout)
        delta = self.read()[before:][-800:]
        ok = res is not None
        log(f"  [{'OK ' if ok else 'FAIL'}] {cmd!r} -> {delta.strip()[-300:]!r}")
        return ok


def main():
    # ensure the nvme backing file exists (QEMU raw driver creates it, but
    # be explicit; a missing parent dir would fail the launch)
    os.makedirs(os.path.dirname(NVME_IMG), exist_ok=True)
    if not os.path.exists(NVME_IMG):
        with open(NVME_IMG, "wb") as f:
            f.truncate(4 * 1024 * 1024 * 1024)

    args = [
        QEMU, "-machine", "ast2700-evb", "-smp", "4", "-m", "2G",
        "-drive", f"file={IMG},format=raw,if=mtd",
        "-trace", "enable=pci_nvme_*", "-D", os.path.join(
            os.path.dirname(LOG), "diag-nvme-trace.log"),
        "-device", "tmp105,bus=aspeed.i2c.bus.1,address=0x4d,id=temp-mb",
        "-device", "adm1272,bus=aspeed.i2c.bus.1,address=0x10,id=psu0",
        "-watchdog-action", "pause",
        "-blockdev", f"driver=file,node-name=nvme-file,filename={NVME_IMG}",
        "-blockdev", "driver=blkdebug,node-name=nvme-bd,image=nvme-file,"
                     "inject-error.0.event=pwritev,"
                     "inject-error.0.errno=5,"
                     "inject-error.0.once=on",
        "-device", "nvme,serial=SN0001,drive=nvme-bd,bus=pcie.2,id=nvme0",
        "-device", "e1000e,netdev=net0,bus=pcie.2,id=nic0",
        "-netdev", "user,id=net0,hostfwd=tcp::2443-:443",
        "-qmp", QMP + ",server=on,wait=off",
        "-L", PC_BIOS,
        "-chardev", f"socket,id=console0,host=127.0.0.1,port={CPORT},"
                    "server=on,wait=off",
        "-serial", "chardev:console0",
        "-display", "none", "-monitor", "none",
    ]
    log(f"launching QEMU: {' '.join(args)}")
    errlog = open(LOG + ".qemu-stderr", "w")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=errlog)
    try:
        con = Console(CPORT)

        log("== waiting for U-Boot ==")
        t0 = time.time()
        if con.wait_for(r"Hit any key to stop autoboot", timeout=420) is None:
            log("FAIL: no U-Boot autoboot prompt")
            return 1
        log(f"U-Boot autoboot prompt at {time.time()-t0:.0f}s")
        con.sock.sendall(b"\n")
        if con.wait_for(r"=>", timeout=10) is None:
            log("FAIL: no U-Boot shell prompt after interrupt")
            return 1
        log("at U-Boot shell prompt")

        log("== official PCIe2 workaround sequence ==")
        ok = True
        ok &= con.cmd('setenv bootargs "${bootargs} cryptomgr.notests=1"', r"=>")
        ok &= con.cmd("cp 100420000 403000000 900000", r"=>")
        ok &= con.cmd("bootm start 403000000", r"=>")
        ok &= con.cmd("bootm loados", r"=>")
        ok &= con.cmd("bootm ramdisk", r"=>")
        ok &= con.cmd("bootm prep", r"=>")
        ok &= con.cmd('fdt set /soc@14000000/pcie@140d0000 status "okay"', r"=>")
        if not ok:
            log("WARNING: some U-Boot steps failed - falling back to plain boot")
        con.sock.sendall(b"bootm go\n")
        if con.wait_for(r"login:", timeout=600) is None:
            log("FAIL: no login after bootm go")
            return 1
        log("== login ==")
        con.sock.sendall(b"root\n")
        con.wait_for(r"Password:", timeout=30)
        con.sock.sendall(b"0penBmc\n")
        if con.wait_for(r"root@", timeout=30) is None:
            log("FAIL: no shell after login")
            return 1
        log("logged in")

        log("== guest state (whole-console checks) ==")
        full = lambda: con.read()

        con.sock.sendall(b"cat /proc/cmdline\n")
        con.wait_for(r"root@", timeout=20)
        m = re.search(r"(?:Kernel )?command line: ([^\r\n]+)", full())
        log(f"  cmdline: {m.group(1) if m else '(not captured)'}")
        log(f"  cryptomgr.notests=1: {'cryptomgr.notests=1' in full()}")

        con.sock.sendall(b"ls /sys/bus/pci/devices/\n")
        con.wait_for(r"root@", timeout=20)
        log(f"  pci 0002:xx: {bool(re.search(r'0002:[0-9a-f:]+', full()))}")
        log(f"  pci tail: {full()[-400:]!r}")

        con.sock.sendall(b"ls /dev/nvme0n1\n")
        con.wait_for(r"root@", timeout=20)
        log(f"  nvme0n1 present: {'nvme0n1' in full().split('ls /dev/nvme0n1')[-1]}")

        con.sock.sendall(b"ls /sys/class/net/\n")
        con.wait_for(r"root@", timeout=20)
        seg = full().split("ls /sys/class/net/")[-1][:200]
        log(f"  net: {seg.strip()!r}")

        log("== storage EIO scenario (launch pwritev+once rule + DNR) ==")
        base = len(full())
        con.sock.sendall(b"dd if=/dev/zero of=/dev/nvme0n1 bs=1M count=4 "
                         b"2>&1; echo RC=$?\n")
        con.wait_for(r"RC=\d+", timeout=45)
        seg = full()[base:]
        log(f"  EIO seen: {'Input/output error' in seg or 'I/O error' in seg}")
        log(f"  dd seg: {seg[-700:]!r}")
        log("== DIAG DONE ==")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        errlog.close()
        logf.close()


if __name__ == "__main__":
    sys.exit(main())
