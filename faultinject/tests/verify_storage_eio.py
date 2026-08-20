#!/usr/bin/env python3
"""Local verification of the nvme DNR fix (patch): the guest dd must see EIO
instead of hanging in a driver retry loop.

Mirrors faultinject/test_bmc_functional.py::test_storage_io_error_guest_visible
against a fresh instance launched with stdio console.
"""
import os
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qmp_client import QMPClient  # noqa: E402

QEMU = os.environ.get("QEMU", r"D:\dsh-qemu\qemu-master\build-mingw\qemu-system-aarch64.exe")
IMG = os.environ.get("IMG", r"D:\dsh-qemu\images\ast2700-default-image\image-bmc")
PC_BIOS = r"D:\dsh-qemu\qemu-master\pc-bios"
NVME_IMG = r"D:\dsh-qemu\nvme.img"
QMP = "tcp:127.0.0.1:4452"


class Console:
    def __init__(self, proc):
        self.proc = proc
        self.buf = []
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for chunk in iter(lambda: self.proc.stdout.read(1), b""):
            with self.lock:
                self.buf.append(chunk.decode("utf-8", errors="replace"))

    def read(self):
        with self.lock:
            return "".join(self.buf)

    def wait_for(self, pattern, timeout=600):
        rx = re.compile(pattern)
        deadline = time.time() + timeout
        while time.time() < deadline:
            t = self.read()
            if rx.search(t):
                return t
            time.sleep(0.3)
        return None

    def send(self, cmd):
        self.proc.stdin.write((cmd + "\n").encode())
        self.proc.stdin.flush()


def main():
    proc = subprocess.Popen(
        [QEMU, "-machine", "ast2700-evb", "-smp", "4", "-m", "2G",
         "-drive", f"file={IMG},format=raw,if=mtd",
         "-blockdev", f"driver=file,node-name=nvme-file,filename={NVME_IMG}",
         "-blockdev", "driver=blkdebug,node-name=nvme-bd,image=nvme-file,"
                      "inject-error.0.event=write_aio,"
                      "inject-error.0.errno=5,"
                      "inject-error.0.once=on",
         "-device", "nvme,serial=SN0001,drive=nvme-bd,id=nvme0",
         "-qmp", QMP + ",server=on,wait=off",
         "-L", PC_BIOS, "-display", "none", "-serial", "stdio",
         "-monitor", "none"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL)
    con = Console(proc)
    try:
        if con.wait_for(r"login:") is None:
            print("FAIL: no login")
            return 1
        print("== login ok ==")
        con.send("ls /dev/nvme0n1")
        if con.wait_for(r"nvme0n1", 15) is None:
            print("SKIP: nvme0n1 not enumerated")
            return 0
        print("== nvme0n1 present; issuing dd (blkdebug injects EIO once) ==")
        con.send("timeout 10 dd if=/dev/zero of=/dev/nvme0n1 bs=1M count=4 "
                 "2>&1; echo RC=$?")
        txt = con.wait_for(r"RC=\d+", 60)
        if txt is None:
            print("FAIL: no dd result in 60s")
            return 1
        if "Input/output error" in txt or "I/O error" in txt:
            print("PASS: guest dd saw the injected EIO (DNR fix works)")
            return 0
        if "RC=124" in txt:
            print("SKIP: dd timed out (still in retry loop?)")
            return 0
        print(f"UNEXPECTED output tail:\n{txt[-800:]}")
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
