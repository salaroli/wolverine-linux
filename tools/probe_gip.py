#!/usr/bin/env python3
"""
Razer Wolverine Ultimate — GIP protocol probe
Systematically tests command IDs and subcommands to find what activates
the audio routing (headphone DAC + mic ADC).

Run as root. Results are saved to probe_results.log.
"""

import os
import sys
import time
import struct
import threading
import usb.core
import usb.util

VENDOR_ID  = 0x1532
PRODUCT_ID = 0x0a14

EP_GIP_OUT  = 0x01
EP_GIP_IN   = 0x81
EP_AUDIO_OUT = 0x03
EP_AUDIO_IN  = 0x83

GIP_OPT_INTERNAL = 0x20
AUD_CLIENT       = 0x01
TIMEOUT_MS       = 400
LOG_FILE         = "probe_results.log"


# ---------------------------------------------------------------------------
# GIP packet encoding
# ---------------------------------------------------------------------------

def encode_varint(value: int) -> bytes:
    result = []
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def build_packet(cmd: int, options: int, seq: int, payload: bytes = b"") -> bytes:
    length_bytes = encode_varint(len(payload))
    header = bytes([cmd, options, seq]) + length_bytes
    if len(header) % 2 != 0:
        last = length_bytes[-1]
        length_bytes = length_bytes[:-1] + bytes([last | 0x80, 0x00])
        header = bytes([cmd, options, seq]) + length_bytes
    return header + payload


def hexstr(data: bytes) -> str:
    return data.hex(" ") if data else "(empty)"


# ---------------------------------------------------------------------------
# Probe machinery
# ---------------------------------------------------------------------------

class Probe:
    def __init__(self, dev):
        self.dev = dev
        self.seq = 1
        self.results = []
        self.log_f = open(LOG_FILE, "w")
        self.log_f.write(f"Probe started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")

    def _log(self, line: str) -> None:
        print(line)
        self.log_f.write(line + "\n")
        self.log_f.flush()

    def _send(self, pkt: bytes) -> None:
        self.dev.write(EP_GIP_OUT, pkt, timeout=TIMEOUT_MS)

    def _recv_gip(self, timeout_ms: int = TIMEOUT_MS) -> bytes | None:
        try:
            return bytes(self.dev.read(EP_GIP_IN, 64, timeout=timeout_ms))
        except usb.core.USBTimeoutError:
            return None
        except usb.core.USBError:
            return None

    def _check_audio_in(self, duration_ms: int = 300) -> bool:
        """Return True if any non-zero data arrives on EP3 IN within duration."""
        deadline = time.time() + duration_ms / 1000
        while time.time() < deadline:
            try:
                data = bytes(self.dev.read(EP_AUDIO_IN, 228, timeout=50))
                if any(data):
                    return True
            except usb.core.USBTimeoutError:
                pass
            except usb.core.USBError:
                pass
        return False

    def test(self, description: str, pkt: bytes) -> dict:
        seq_used = self.seq
        self.seq = (self.seq % 254) + 1

        self._log(f"\n[{description}]")
        self._log(f"  → send: {hexstr(pkt)}")

        try:
            self._send(pkt)
        except usb.core.USBError as e:
            self._log(f"  ✗ send error: {e}")
            result = {"desc": description, "pkt": pkt.hex(), "response": None,
                      "audio_in": False, "error": str(e)}
            self.results.append(result)
            return result

        # Wait for response on EP1 IN
        resp = self._recv_gip(TIMEOUT_MS)
        if resp:
            self._log(f"  ← response: {hexstr(resp)}")
        else:
            self._log(f"  ← timeout (no response)")

        # Check if audio routing activated
        audio_active = self._check_audio_in(300)
        if audio_active:
            self._log(f"  ★ AUDIO IN ACTIVATED! Non-zero data on EP3 IN!")
        else:
            self._log(f"  · EP3 IN: silence")

        result = {
            "desc": description,
            "pkt": pkt.hex(),
            "response": resp.hex() if resp else None,
            "audio_in": audio_active,
        }
        self.results.append(result)

        time.sleep(0.1)
        return result

    def summary(self) -> None:
        self._log("\n" + "=" * 60)
        self._log("SUMMARY")
        self._log("=" * 60)
        responded = [r for r in self.results if r["response"]]
        activated = [r for r in self.results if r["audio_in"]]

        self._log(f"\nTotal tests: {len(self.results)}")
        self._log(f"Got response: {len(responded)}")
        self._log(f"Audio activated: {len(activated)}")

        if responded:
            self._log("\n--- Commands that got a response: ---")
            for r in responded:
                self._log(f"  {r['desc']}: {r['response']}")

        if activated:
            self._log("\n--- Commands that activated audio: ---")
            for r in activated:
                self._log(f"  ★ {r['desc']}")

        self.log_f.close()
        print(f"\nFull results saved to {LOG_FILE}")


# ---------------------------------------------------------------------------
# GIP init (copied from gip_init.py)
# ---------------------------------------------------------------------------

def gip_init(dev):
    seq = [1]

    def send_recv(pkt, label=""):
        try:
            dev.write(EP_GIP_OUT, pkt, timeout=500)
        except usb.core.USBError as e:
            print(f"  [{label}] send error: {e}")
            return None
        try:
            return bytes(dev.read(EP_GIP_IN, 64, timeout=500))
        except usb.core.USBTimeoutError:
            return None

    s = seq[0]; seq[0] += 1
    pkt = build_packet(0x04, GIP_OPT_INTERNAL, s)
    send_recv(pkt, "IDENTIFY")

    s = seq[0]; seq[0] += 1
    pkt = build_packet(0x08, GIP_OPT_INTERNAL | AUD_CLIENT, s,
                       bytes([0x02, 0x10, 0x10]))
    resp = send_recv(pkt, "AUDIO_FORMAT")
    if resp:
        print(f"  AUDIO_FORMAT response: {resp.hex()}")

    return seq[0]


# ---------------------------------------------------------------------------
# Candidate commands to probe
# ---------------------------------------------------------------------------

def build_candidates(p: Probe) -> list[tuple[str, bytes]]:
    opts_i  = GIP_OPT_INTERNAL
    opts_ia = GIP_OPT_INTERNAL | AUD_CLIENT

    candidates = []

    # --- AUDIO_CONTROL (0x08) all subcommands ---
    for sub in range(0x00, 0x10):
        if sub in (0x02, 0x03):
            continue  # already tested
        for payload_extra in [b"", b"\x04", b"\x04\x64\x64\x64", b"\x00\x64\x64\x64"]:
            payload = bytes([sub]) + payload_extra
            desc = f"AUDIO_CTRL sub=0x{sub:02x} payload={payload.hex()}"
            candidates.append((desc, build_packet(0x08, opts_ia, p.seq, payload)))
            p.seq = (p.seq % 254) + 1

    # --- AUDIO_FORMAT chat variants ---
    for fmt in [0x04, 0x05, 0x09]:  # 24kHz, 16kHz, 24kHz mono
        payload = bytes([0x01, fmt])  # FORMAT_CHAT subcommand
        desc = f"AUDIO_FORMAT_CHAT fmt=0x{fmt:02x}"
        candidates.append((desc, build_packet(0x08, opts_ia, p.seq, payload)))
        p.seq = (p.seq % 254) + 1

    # --- GIP commands not yet tried ---
    for cmd in [0x05, 0x06, 0x07, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f,
                0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19,
                0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f,
                0x30, 0x31, 0x32, 0x40, 0x41, 0x50, 0x5f]:
        desc = f"CMD 0x{cmd:02x} empty"
        candidates.append((desc, build_packet(cmd, opts_i, p.seq)))
        p.seq = (p.seq % 254) + 1

        desc = f"CMD 0x{cmd:02x} payload=0x80000000"
        candidates.append((desc, build_packet(cmd, opts_i, p.seq,
                                              b"\x80\x00\x00\x00")))
        p.seq = (p.seq % 254) + 1

    # --- AUDIO_CONTROL with opts=0x20 (no client id) ---
    for sub in [0x00, 0x01, 0x02, 0x03]:
        payload = bytes([sub, 0x04, 0x64, 0x64, 0x64, 0x00, 0x00, 0x00])
        desc = f"AUDIO_CTRL no-client sub=0x{sub:02x}"
        candidates.append((desc, build_packet(0x08, opts_i, p.seq, payload)))
        p.seq = (p.seq % 254) + 1

    return candidates


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if os.geteuid() != 0:
        print("ERROR: run as root")
        sys.exit(1)

    print("=== Razer Wolverine Ultimate — GIP Probe ===\n")

    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        print("ERROR: device not found")
        sys.exit(1)
    print(f"Found: {dev.manufacturer} {dev.product}")

    print("\nClaiming interfaces...")
    for iface in [0, 1, 2]:
        try:
            if dev.is_kernel_driver_active(iface):
                dev.detach_kernel_driver(iface)
            usb.util.claim_interface(dev, iface)
            print(f"  Interface {iface} claimed")
        except usb.core.USBError as e:
            print(f"  Interface {iface}: {e}")

    for iface in [1, 2]:
        try:
            dev.set_interface_altsetting(interface=iface, alternate_setting=1)
        except usb.core.USBError:
            pass

    print("\nRunning GIP init...")
    next_seq = gip_init(dev)

    p = Probe(dev)
    p.seq = next_seq

    candidates = build_candidates(p)
    p.seq = next_seq  # reset after building (seq was incremented during build)

    # Rebuild with correct seq now that we know starting point
    p.seq = next_seq
    candidates = build_candidates(p)
    p.seq = next_seq  # reset again to run sequentially

    print(f"\nRunning {len(candidates)} probe tests...")
    print(f"Results → {LOG_FILE}\n")
    print("Press Ctrl+C to stop early (partial results will be saved)\n")

    try:
        for i, (desc, pkt) in enumerate(candidates):
            sys.stdout.write(f"\r[{i+1}/{len(candidates)}] {desc[:60]:<60}")
            sys.stdout.flush()
            p.test(desc, pkt)
    except KeyboardInterrupt:
        print("\n\nInterrupted early.")

    p.summary()

    for iface in [0, 1, 2]:
        try:
            usb.util.release_interface(dev, iface)
        except Exception:
            pass


if __name__ == "__main__":
    main()
