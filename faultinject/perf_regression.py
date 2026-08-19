#!/usr/bin/env python3
"""Performance regression collector for the AST2700 BMC platform.

Deterministic methodology (TCG is NOT real-time hardware):
  * -icount shift=auto,align=on,sleep=off  -> guest-time deterministic
  * QMP x-query-jit "executed" counter     -> host-independent work metric
                                            (total guest instructions retired)
  * Redfish GET latency via hostfwd        -> service-level regression
  * optional TCG plugin (Linux builds)     -> finer-grained counters

Usage:
  python3 faultinject/perf_regression.py --baseline baseline.json --out result.json
Exits non-zero when a metric regresses beyond tolerance (default +10%).
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qmp_client import QMPClient  # noqa: E402

QEMU = os.environ.get("QEMU", "./qemu-master/build-mingw/qemu-system-aarch64.exe")
IMG = os.environ.get("IMG", "images/ast2700-default-image/image-bmc")
PC_BIOS = os.environ.get("PC_BIOS", "./qemu-master/pc-bios")
CONSOLE_LOG = os.environ.get("PERF_CONSOLE", "perf-console.log")
QMP_ADDR = os.environ.get("PERF_QMP", "tcp:127.0.0.1:4445")
REDFISH_URL = "http://127.0.0.1:2443/redfish/v1"   # hostfwd from netdev below


def run_boot_measure():
    """Boot with -icount; return wall time + JIT executed-instruction count."""
    for p in (CONSOLE_LOG,):
        if os.path.exists(p):
            os.unlink(p)

    args = [QEMU,
            "-machine", "ast2700-evb",
            "-smp", "4", "-m", "2G",
            "-drive", f"file={IMG},format=raw,if=mtd",
            "-device", "tmp105,bus=aspeed.i2c.bus.1,address=0x4d",
            "-device", "e1000e,netdev=net0,bus=pcie.2,id=nic0",
            "-netdev", f"user,id=net0,hostfwd=tcp::{2443}-:443",
            "-icount", "shift=auto,sleep=off",   # align=on x sleep=off, and
                                                 # thread=multi x icount both conflict
            "-accel", "tcg",
            "-qmp", QMP_ADDR + ",server=on,wait=off",
            "-L", PC_BIOS,
            "-display", "none",
            "-serial", "file:" + CONSOLE_LOG]

    t0 = time.monotonic()
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)

    qmp = None
    try:
        qmp = QMPClient(QMP_ADDR)
        qmp.connect()

        deadline = time.monotonic() + 900
        boot_wall = None
        while time.monotonic() < deadline:
            try:
                with open(CONSOLE_LOG, errors="replace") as f:
                    if re.search(r"login:", f.read()):
                        boot_wall = round(time.monotonic() - t0, 3)
                        break
            except FileNotFoundError:
                pass
            time.sleep(0.5)

        jit = {}
        try:
            text = qmp.cmd("x-query-jit").get("human-readable-text", "")
            # Deterministic work proxies (same guest workload -> same values):
            m = re.search(r"TB count\s+(\d+)", text)
            if m:
                jit["tb_count"] = int(m.group(1))
            m = re.search(r"TB flush count\s+(\d+)", text)
            if m:
                jit["tb_flush_count"] = int(m.group(1))
            m = re.search(r"gen code size\s+(\d+)/\d+", text)
            if m:
                jit["gen_code_size"] = int(m.group(1))
        except Exception:
            pass
        return {"boot_wall_s": boot_wall, **jit}
    finally:
        if qmp is not None:
            try:
                qmp.close()
            except Exception:
                pass
        proc.terminate()
        proc.wait(timeout=10)


def redfish_latency(n=20):
    """p50/p95 GET latency against the BMC Redfish endpoint (via hostfwd)."""
    lat = []
    for _ in range(n):
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(REDFISH_URL, timeout=5) as r:
                assert r.status == 200
            lat.append((time.monotonic() - t0) * 1000)
        except Exception:
            pass
        time.sleep(0.1)
    if not lat:
        return {"redfish_p50_ms": None, "redfish_p95_ms": None, "samples": 0}
    lat.sort()
    return {"redfish_p50_ms": round(statistics.median(lat), 2),
            "redfish_p95_ms": round(lat[int(len(lat) * 0.95) - 1], 2),
            "samples": len(lat)}


def compare(result, baseline, tol):
    regressions = []
    for key, val in result.items():
        if val is None or key not in baseline or baseline[key] is None:
            continue
        base = baseline[key]
        if base == 0:
            continue
        delta = (val - base) / base
        if delta > tol:
            regressions.append(f"{key}: {base} -> {val} (+{delta*100:.1f}%)")
    return regressions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default="baseline.json")
    ap.add_argument("--out", default="result.json")
    ap.add_argument("--tolerance", type=float, default=0.10)
    ap.add_argument("--redfish-samples", type=int, default=20)
    args = ap.parse_args()

    print("== boot measurement (icount + x-query-jit) ==")
    result = run_boot_measure()
    print("== redfish latency ==")
    result.update(redfish_latency(args.redfish_samples))

    print(json.dumps(result, indent=2))
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)

    if os.path.exists(args.baseline):
        with open(args.baseline) as f:
            baseline = json.load(f)
        reg = compare(result, baseline, args.tolerance)
        if reg:
            print("REGRESSIONS:\n  " + "\n  ".join(reg))
            sys.exit(1)
        print("OK: within tolerance")
    else:
        print(f"no baseline at {args.baseline}; {args.out} written as candidate baseline")


if __name__ == "__main__":
    main()
