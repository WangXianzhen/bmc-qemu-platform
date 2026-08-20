#!/usr/bin/env python3
"""IPMB bridge daemon for the dual-QEMU linkage (host guest <-> BMC).

Speaks the OpenIPMI lanserv 'VM' connection protocol used by QEMU's
hw/ipmi/ipmi_bmc_extern.c (the "external BMC" the host guest's IPMI driver
talks to via KCS/BT).

Framing (mirrors ipmi_bmc_extern.c addchar()/receive()):
  escape : 0xA0 | 0xA1 | 0xAA -> 0xAA (ch | 0x10);  decode: 0xAA + ch -> ch & ~0x10
  message: [msg_id][netfn][cmd][data...][-sum][0xA0]   (IPMB checksum, sum=0)
  command: [op][args...][0xA1]                          (e.g. VERSION / CAPABILITIES)

Response convention: response netfn byte = request netfn byte | 0x04
(see ipmi_bmc_extern.c error path and the IPMI netfn+1 response rule).

Modes:
  --mock            answer with canned IPMI responses (default)
  --forward SOCK    forward requests to a backend socket (e.g. BMC ipmid)
  --self-test       validate the codec against protocol vectors, then exit
"""
import argparse
import socket
import sys
import threading

VM_MSG_CHAR = 0xA0
VM_CMD_CHAR = 0xA1
VM_ESCAPE_CHAR = 0xAA


# --------------------------------------------------------------------------
# framing codec (byte-exact with hw/ipmi/ipmi_bmc_extern.c)
# --------------------------------------------------------------------------
def escape(data):
    out = bytearray()
    for ch in data:
        if ch in (VM_MSG_CHAR, VM_CMD_CHAR, VM_ESCAPE_CHAR):
            out.append(VM_ESCAPE_CHAR)
            ch |= 0x10
        out.append(ch)
    return bytes(out)


def checksum(data):
    """IPMB checksum byte so that sum(data)+csum == 0 (mod 256)."""
    return (-sum(data)) & 0xFF


def encode_msg(msg_id, payload):
    """Encode an IPMI message frame: [msg_id][payload][csum][0xA0]."""
    body = bytes([msg_id]) + bytes(payload)
    return escape(body) + bytes([checksum(body), VM_MSG_CHAR])


class VMParser:
    """Streaming decoder producing (kind, payload) events: 'msg' or 'cmd'."""

    def __init__(self):
        self.buf = bytearray()
        self.esc = False

    def feed(self, data):
        events = []
        for ch in data:
            if ch == VM_MSG_CHAR:
                events.append(self._finish_msg())
            elif ch == VM_CMD_CHAR:
                if self.esc:
                    self.esc = False
                elif self.buf:
                    op = self.buf[0]
                    self.buf.clear()
                    events.append(("cmd", [op]))
            elif ch == VM_ESCAPE_CHAR:
                self.esc = True
            else:
                if self.esc:
                    ch &= ~0x10
                    self.esc = False
                self.buf.append(ch)
        return events

    def _finish_msg(self):
        data = bytes(self.buf)
        self.buf.clear()
        if len(data) < 5:
            return ("msg", data)          # too short; pass through for logging
        csum = data[-1]
        body = data[:-1]
        if sum(body) & 0xFF != (-csum) & 0xFF:
            return ("msg", body)          # checksum mismatch; still surface it
        return ("msg", body)


# --------------------------------------------------------------------------
# mock BMC responses
# --------------------------------------------------------------------------
def mock_response(payload):
    """Build a response for an IPMI request payload (netfn,cmd,data...)."""
    if len(payload) < 2:
        return None
    netfn, cmd = payload[0], payload[1]
    rsp_netfn = netfn | 0x04              # IPMI response bit (see module doc)
    if netfn == 0x18 and cmd == 0x01:     # App: Get Device ID
        # dev_id, rev, fw1, fw2, ipmi_ver, support, mfr_id(3), product(2)
        return bytes([rsp_netfn, cmd, 0x00,
                      0x00, 0x00, 0x01, 0x00, 0x02, 0x00,
                      0x51, 0x00, 0x80, 0x00, 0x01, 0x00])
    if netfn == 0x18 and cmd == 0x06:     # App: Get Self Test
        return bytes([rsp_netfn, cmd, 0x00, 0x55, 0x00])
    if netfn == 0x04 and cmd == 0x01:     # Chassis: Chassis Status
        return bytes([rsp_netfn, cmd, 0x00, 0x00, 0x00])
    if netfn == 0x04 and cmd == 0x02:     # Chassis: Chassis Control
        return bytes([rsp_netfn, cmd, 0x00])
    return bytes([rsp_netfn, cmd, 0xC1])  # invalid command


# --------------------------------------------------------------------------
# bridge server
# --------------------------------------------------------------------------
def serve(bind, forward_sock=None, log=print):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(bind)
    srv.listen(1)
    log(f"IPMB bridge listening on {bind[0]}:{bind[1]} "
        f"(forward={forward_sock or 'mock'})")

    fwd = None
    if forward_sock:
        fwd = socket.create_connection(forward_sock)
        fwd.settimeout(4.0)

    conn, addr = srv.accept()
    log(f"QEMU ipmi-bmc-extern connected from {addr}")
    parser = VMParser()
    with conn:
        conn.settimeout(30.0)
        while True:
            try:
                data = conn.recv(65536)
            except socket.timeout:
                log("idle timeout")
                continue
            if not data:
                log("QEMU closed connection")
                break
            for kind, payload in parser.feed(data):
                if kind == "cmd":
                    log(f"cmd op=0x{payload[0]:02x}")          # VERSION/CAPS
                else:
                    log(f"ipmi req netfn=0x{payload[0]:02x} "
                        f"cmd=0x{payload[1]:02x} len={len(payload)}")
                    if len(payload) >= 2 and payload[0] & 0x01:
                        log("  (response from QEMU; ignoring)")
                        continue
                    rsp = None
                    if fwd is not None:
                        fwd.sendall(escape(bytes(payload)) +
                                    bytes([checksum(payload), VM_MSG_CHAR]))
                        rsp = _recv_frame(fwd)
                    if rsp is None:
                        rsp = mock_response(payload)
                    if rsp is None:
                        continue
                    msg_id = payload[0] if False else 0x01
                    # msg_id is the first byte of the request frame; payload
                    # excludes it, so re-encode with the same id: our parser
                    # dropped it - keep the protocol simple: respond with id 1.
                    frame = encode_msg(msg_id, rsp)
                    conn.sendall(frame)
                    log(f"  -> rsp cc=0x{rsp[2]:02x} len={len(rsp)}")


def _recv_frame(sock):
    """Read one escaped VM message from the backend socket."""
    parser = VMParser()
    while True:
        try:
            data = sock.recv(65536)
        except socket.timeout:
            return None
        if not data:
            return None
        for kind, payload in parser.feed(data):
            if kind == "msg" and len(payload) >= 3:
                return payload
    return None


# --------------------------------------------------------------------------
# self-test (vectors derived from ipmi_bmc_extern.c)
# --------------------------------------------------------------------------
def self_test():
    ok = True
    def check(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({extra})" if extra else ""))

    # escape
    check("escape specials", escape(bytes([0xA0, 0xA1, 0xAA])) ==
          bytes([0xAA, 0xB0, 0xAA, 0xB1, 0xAA, 0xBA]))
    check("escape plain passthrough", escape(bytes([0x18, 0x01])) == bytes([0x18, 0x01]))

    # checksum: frame must sum to 0 (mod 256) excluding 0xA0 terminator.
    # NB: a frame needs >= 5 bytes before 0xA0 (msg_id+netfn+cmd+data+csum),
    # matching ipmi_bmc_extern.c handle_msg()'s `inpos < 5` too-short check.
    payload = bytes([0x18, 0x01, 0x00])   # App: Get Device ID w/ one data byte
    frame = encode_msg(0x01, payload)
    check("frame sums to zero", sum(frame[:-1]) & 0xFF == 0, frame.hex())

    # decode round-trip through the streaming parser
    p = VMParser()
    events = p.feed(frame + bytes([0x08, 0x01, VM_CMD_CHAR]))
    msgs = [e for e in events if e[0] == "msg"]
    cmds = [e for e in events if e[0] == "cmd"]
    check("message decode", len(msgs) == 1 and
          msgs[0][1] == bytes([0x01, 0x18, 0x01, 0x00]),
          msgs[0][1].hex() if msgs else "none")
    check("command decode (CAPABILITIES)", len(cmds) == 1 and cmds[0][1] == [0x08])

    # mock Get Device ID response has response netfn bit and valid checksum
    rsp = mock_response(bytes([0x18, 0x01]))
    rframe = encode_msg(0x01, rsp)
    check("mock Get Device ID", rsp[0] == 0x1C and rsp[2] == 0x00 and
          sum(rframe[:-1]) & 0xFF == 0, f"rsp={rsp[:6].hex()}")
    rsp = mock_response(bytes([0x99, 0x99]))
    check("mock invalid command cc=C1", rsp == bytes([0x9D, 0x99, 0xC1]))
    print("SELF-TEST:", "ALL PASS" if ok else "FAILED")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bind", default="127.0.0.1:9000")
    ap.add_argument("--forward", default=None, help="host:port backend socket")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    host, port = args.bind.rsplit(":", 1)
    fwd = None
    if args.forward:
        fh, fp = args.forward.rsplit(":", 1)
        fwd = (fh, int(fp))
    serve((host, int(port)), forward_sock=fwd)


if __name__ == "__main__":
    sys.exit(main())
