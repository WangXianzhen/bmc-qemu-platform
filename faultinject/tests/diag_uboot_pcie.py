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

QEMU = r"D:/dsh-qemu/qemu-master/build-mingw/qemu-system-aarch64.exe"
IMG = r"D:/dsh-qemu/images/ast2700-default-image/image-bmc"
PC_BIOS = r"D:/dsh-qemu/qemu-master/pc-bios"
NVME_IMG = r"D:/dsh-qemu/nvme.img"
QMP = "tcp:127.0.0.1:4455"
CPORT = 4568
LOG = r"D:\dsh-qemu\diag-uboot.log"

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
    proc = subprocess.Popen(
        [QEMU, "-machine", "ast2700-evb", "-smp", "4", "-m", "2G",
         "-drive", f"file={IMG},format=raw,if=mtd",
         "-blockdev", f"driver=file,node-name=nvme-file,filename={NVME_IMG}",
         "-blockdev", "driver=blkdebug,node-name=nvme-bd,image=nvme-file,"
                      "inject-error.0.event=write_aio,"
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
         "-display", "none", "-monitor", "none"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

        log("== guest state ==")
        base = len(con.read())
        con.sock.sendall(b"cat /proc/cmdline; echo CMD_DONE\n")
        con.wait_for(r"CMD_DONE", timeout=20)
        seg = con.read()[base:].split("CMD_DONE")[0]
        m = re.search(r"(?:Kernel )?command line: ([^\r\n]+)", seg)
        log(f"  cmdline: {m.group(1) if m else '(not captured)'}")
        log(f"  cryptomgr.notests=1: {'cryptomgr.notests=1' in seg}")

        base = len(con.read())
        con.sock.sendall(b"ls /sys/bus/pci/devices/ 2>&1; echo PCI_END\n")
        con.wait_for(r"PCI_END", timeout=20)
        log(f"  pci: {con.read()[base:].split('PCI_END')[0].strip()!r}")

        base = len(con.read())
        con.sock.sendall(b"ls /dev/nvme0n1 2>&1; echo NVME_END\n")
        con.wait_for(r"NVME_END", timeout=20)
        log(f"  nvme0n1: {con.read()[base:].split('NVME_END')[0].strip()!r}")

        base = len(con.read())
        con.sock.sendall(b"ls /sys/class/net/ 2>&1; echo NET_END\n")
        con.wait_for(r"NET_END", timeout=20)
        log(f"  net: {con.read()[base:].split('NET_END')[0].strip()!r}")

        log("== storage EIO scenario (blkdebug + nvme DNR fix) ==")
        base = len(con.read())
        con.sock.sendall(b"timeout 10 dd if=/dev/zero of=/dev/nvme0n1 "
                         b"bs=1M count=4 2>&1; echo RC=$?\n")
        con.wait_for(r"RC=\d+", timeout=45)
        seg = con.read()[base:].split("RC=")[0]
        log(f"  dd output: {seg.strip()[-600:]!r}")
        log("== DIAG DONE ==")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        logf.close()


if __name__ == "__main__":
    sys.exit(main())
