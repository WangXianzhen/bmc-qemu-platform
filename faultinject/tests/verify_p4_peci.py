#!/usr/bin/env python3
"""Verify patch P4 (PECI fault properties) at the QOM level on ast2600-evb.

The AST2600 SoC instantiates the PECI controller (/machine/soc/peci), so the
runtime properties added by patch P4 can be exercised without a full boot.
(AST2700 does not instantiate PECI yet - wiring is datasheet-dependent.)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from qmp_client import QMPClient  # noqa: E402


def main():
    qmp = QMPClient("tcp:127.0.0.1:4447")
    qmp.connect()

    ok = True
    try:
        props = {p["name"] for p in qmp.qom_list("/machine/soc/peci")}
        print("peci props:", sorted(props))
        assert "host-lost" in props and "temp-fault" in props, "P4 props missing"
        print("[PASS] P4 properties registered on /machine/soc/peci")

        qmp.qom_set("/machine/soc/peci", "host-lost", True)
        assert qmp.qom_get("/machine/soc/peci", "host-lost") is True
        print("[PASS] host-lost runtime set")
        qmp.qom_set("/machine/soc/peci", "temp-fault", True)
        assert qmp.qom_get("/machine/soc/peci", "temp-fault") is True
        print("[PASS] temp-fault runtime set")
        qmp.qom_set("/machine/soc/peci", "host-lost", False)
        qmp.qom_set("/machine/soc/peci", "temp-fault", False)
        print("[PASS] faults cleared")
    except AssertionError as e:
        print("[FAIL]", e)
        ok = False
    finally:
        try:
            qmp.close()
        except Exception:
            pass
    print("P4 DEVICE-LEVEL VERIFICATION:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
