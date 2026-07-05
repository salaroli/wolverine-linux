#!/usr/bin/env python3
"""
Razer Wolverine Ultimate — GIP audio initialization
Implements Xbox Game Interface Protocol (GIP) to activate the audio
jack and microphone on interfaces 1 and 2.

Protocol reference: github.com/medusalix/xone, TheNathannator's GIP notes.
Run as root.
"""

import sys
import time
import threading
import usb.core
import usb.util

VENDOR_ID  = 0x1532
PRODUCT_ID = 0x0a14

# Endpoints
EP_HID_OUT   = 0x01   # interrupt, interface 0 (owned by xpad)
EP_HID_IN    = 0x81
EP_CTRL_OUT  = 0x02   # bulk, interface 2 — GIP audio control
EP_CTRL_IN   = 0x82
EP_AUDIO_OUT = 0x03   # isochronous, interface 1 — audio playback
EP_AUDIO_IN  = 0x83   # isochronous, interface 1 — mic capture

# GIP command IDs
GIP_CMD_ACKNOWLEDGE   = 0x01
GIP_CMD_ANNOUNCE      = 0x02
GIP_CMD_STATUS        = 0x03
GIP_CMD_IDENTIFY      = 0x04
GIP_CMD_POWER         = 0x05
GIP_CMD_AUDIO_CONTROL = 0x08
GIP_CMD_AUDIO_SAMPLES = 0x60

# GIP option flags
GIP_OPT_ACKNOWLEDGE = 0x10
GIP_OPT_INTERNAL    = 0x20
GIP_OPT_CHUNK_START = 0x40
GIP_OPT_CHUNK       = 0x80

# Audio control subcommands
GIP_AUD_CTRL_VOLUME_CHAT  = 0x00
GIP_AUD_CTRL_FORMAT_CHAT  = 0x01
GIP_AUD_CTRL_FORMAT       = 0x02
GIP_AUD_CTRL_VOLUME       = 0x03

# Audio format codes
GIP_AUD_FORMAT_16KHZ_MONO   = 0x05
GIP_AUD_FORMAT_24KHZ_MONO   = 0x09
GIP_AUD_FORMAT_48KHZ_STEREO = 0x10

TIMEOUT_MS = 500


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
    # Header must be even length; pad if needed
    if len(header) % 2 != 0:
        last = length_bytes[-1]
        length_bytes = length_bytes[:-1] + bytes([last | 0x80, 0x00])
        header = bytes([cmd, options, seq]) + length_bytes
    return header + payload


def pkt_identify(seq: int = 1) -> bytes:
    return build_packet(GIP_CMD_IDENTIFY, GIP_OPT_INTERNAL, seq)


def pkt_audio_format(seq: int, in_fmt: int, out_fmt: int) -> bytes:
    payload = bytes([GIP_AUD_CTRL_FORMAT, in_fmt, out_fmt])
    return build_packet(GIP_CMD_AUDIO_CONTROL, GIP_OPT_INTERNAL, seq, payload)


def pkt_audio_volume(seq: int, out_vol: int = 100, in_vol: int = 100) -> bytes:
    # mute=0x04 (unmuted), chat_vol=100
    payload = bytes([GIP_AUD_CTRL_VOLUME, 0x04, out_vol, 100, in_vol, 0x00, 0x00, 0x00])
    return build_packet(GIP_CMD_AUDIO_CONTROL, GIP_OPT_INTERNAL, seq, payload)


def pkt_acknowledge(ack_cmd: int, ack_opts: int, ack_seq: int) -> bytes:
    payload = bytes([0x00, ack_cmd, ack_opts, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    return build_packet(GIP_CMD_ACKNOWLEDGE, GIP_OPT_INTERNAL, ack_seq, payload)


# ---------------------------------------------------------------------------
# USB helpers
# ---------------------------------------------------------------------------

def hexdump(data: bytes, prefix: str = "  ") -> None:
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"{prefix}{i:04x}  {hex_part:<48}  {asc_part}")


def send(dev, ep_out: int, data: bytes, label: str) -> None:
    print(f"\n→ [{label}] sending {len(data)} bytes to EP{ep_out & 0x0F} OUT:")
    hexdump(data)
    try:
        sent = dev.write(ep_out, data, timeout=TIMEOUT_MS)
        print(f"  sent {sent} bytes")
    except usb.core.USBError as e:
        print(f"  ERROR sending: {e}")


def recv(dev, ep_in: int, size: int = 64, label: str = "") -> bytes | None:
    try:
        data = bytes(dev.read(ep_in, size, timeout=TIMEOUT_MS))
        print(f"\n← [{label}] received {len(data)} bytes from EP{ep_in & 0x0F} IN:")
        hexdump(data)
        return data
    except usb.core.USBTimeoutError:
        print(f"  [{label}] timeout — no response")
        return None
    except usb.core.USBError as e:
        print(f"  [{label}] read error: {e}")
        return None


# ---------------------------------------------------------------------------
# Main init sequence
# ---------------------------------------------------------------------------

def gip_init(dev) -> bool:
    seq = 1

    # --- Step 1: IDENTIFY ---
    print("\n" + "=" * 60)
    print("STEP 1 — IDENTIFY (discover supported audio formats)")
    print("=" * 60)

    # Try on EP2 (bulk, interface 2) first
    send(dev, EP_CTRL_OUT, pkt_identify(seq), "IDENTIFY→EP2")
    resp = recv(dev, EP_CTRL_IN, 64, "IDENTIFY←EP2")

    if resp is None:
        # Fallback: try on EP1 (interrupt, interface 0) — may conflict with xpad
        print("\n  No response on EP2 — trying EP1 (may conflict with xpad)...")
        try:
            send(dev, EP_HID_OUT, pkt_identify(seq), "IDENTIFY→EP1")
            resp = recv(dev, EP_HID_IN, 64, "IDENTIFY←EP1")
        except usb.core.USBError as e:
            print(f"  EP1 failed: {e}")

    if resp:
        cmd = resp[0]
        print(f"\n  Response command: 0x{cmd:02x}")
        if cmd == GIP_CMD_IDENTIFY:
            print("  ✓ IDENTIFY acknowledged — device supports GIP audio")
        elif cmd == GIP_CMD_ACKNOWLEDGE:
            print("  ✓ ACK received")
        else:
            print(f"  ? Unexpected command 0x{cmd:02x} — continuing anyway")
    else:
        print("\n  ✗ No IDENTIFY response — EP2 may not be the GIP control channel")

    seq += 1
    time.sleep(0.05)

    # --- Step 2: Request 48kHz stereo format ---
    print("\n" + "=" * 60)
    print("STEP 2 — AUDIO FORMAT negotiation (48kHz stereo)")
    print("=" * 60)

    pkt = pkt_audio_format(seq, GIP_AUD_FORMAT_48KHZ_STEREO, GIP_AUD_FORMAT_48KHZ_STEREO)
    send(dev, EP_CTRL_OUT, pkt, "AUDIO_FORMAT→EP2")
    resp = recv(dev, EP_CTRL_IN, 64, "AUDIO_FORMAT←EP2")

    if resp is None:
        send(dev, EP_HID_OUT, pkt, "AUDIO_FORMAT→EP1")
        resp = recv(dev, EP_HID_IN, 64, "AUDIO_FORMAT←EP1")

    if resp:
        cmd = resp[0]
        if cmd == GIP_CMD_AUDIO_CONTROL and len(resp) >= 5:
            sub = resp[4]
            print(f"\n  ✓ Audio control response, subcommand: 0x{sub:02x}")
            if sub == GIP_AUD_CTRL_FORMAT:
                in_fmt, out_fmt = resp[5], resp[6]
                print(f"  Negotiated format — IN: 0x{in_fmt:02x}  OUT: 0x{out_fmt:02x}")
        elif cmd == GIP_CMD_ACKNOWLEDGE:
            print("  ✓ ACK received")

    seq += 1
    time.sleep(0.05)

    # --- Step 3: Set volume ---
    print("\n" + "=" * 60)
    print("STEP 3 — VOLUME init (unmute, 100%)")
    print("=" * 60)

    pkt = pkt_audio_volume(seq)
    send(dev, EP_CTRL_OUT, pkt, "VOLUME→EP2")
    resp = recv(dev, EP_CTRL_IN, 64, "VOLUME←EP2")

    if resp is None:
        send(dev, EP_HID_OUT, pkt, "VOLUME→EP1")
        resp = recv(dev, EP_HID_IN, 64, "VOLUME←EP1")

    seq += 1
    time.sleep(0.05)

    return True


def monitor_audio(dev, stop_event: threading.Event) -> None:
    print("\n[audio] Monitoring EP3 IN (isochronous mic) — waiting for stream...")
    packet_count = 0
    while not stop_event.is_set():
        try:
            data = bytes(dev.read(EP_AUDIO_IN, 228, timeout=100))
            if data:
                packet_count += 1
                if packet_count <= 3:
                    ts = time.strftime("%H:%M:%S")
                    print(f"\n[audio {ts}] EP3 IN — {len(data)} bytes (#{packet_count}):")
                    hexdump(data)
                elif packet_count == 4:
                    print(f"[audio] Stream active — {packet_count} packets received, suppressing dumps")
                    print("  SUCCESS: audio isochronous stream is flowing!")
        except usb.core.USBTimeoutError:
            if packet_count > 0:
                print(f"\n[audio] Stream paused after {packet_count} packets")
                packet_count = 0
        except usb.core.USBError as e:
            if not stop_event.is_set():
                print(f"[audio] error: {e}")
            time.sleep(0.1)


def monitor_ctrl(dev, stop_event: threading.Event) -> None:
    print("[ctrl] Monitoring EP2 IN (bulk) for media button events...")
    while not stop_event.is_set():
        try:
            data = bytes(dev.read(EP_CTRL_IN, 64, timeout=100))
            if data:
                ts = time.strftime("%H:%M:%S")
                cmd = data[0]
                print(f"\n[ctrl {ts}] EP2 IN — cmd=0x{cmd:02x} ({len(data)} bytes):")
                hexdump(data)
        except usb.core.USBTimeoutError:
            pass
        except usb.core.USBError as e:
            if not stop_event.is_set():
                print(f"[ctrl] error: {e}")
            time.sleep(0.1)


def main():
    if __import__("os").geteuid() != 0:
        print("ERROR: run as root (sudo)")
        sys.exit(1)

    print("=== Razer Wolverine Ultimate — GIP Audio Init ===\n")

    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        print("ERROR: device not found")
        sys.exit(1)
    print(f"Found: {dev.manufacturer} {dev.product} (bus {dev.bus}, addr {dev.address})")

    for iface in [1, 2]:
        if dev.is_kernel_driver_active(iface):
            dev.detach_kernel_driver(iface)
        usb.util.claim_interface(dev, iface)
        dev.set_interface_altsetting(interface=iface, alternate_setting=1)
        print(f"Interface {iface} claimed, alt setting 1 active")

    gip_init(dev)

    print("\n" + "=" * 60)
    print("MONITORING — press media buttons, plug headset, Ctrl+C to stop")
    print("=" * 60)

    stop = threading.Event()
    threads = [
        threading.Thread(target=monitor_audio, args=(dev, stop), daemon=True),
        threading.Thread(target=monitor_ctrl,  args=(dev, stop), daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()
        print("\nStopping...")

    for t in threads:
        t.join(timeout=1)

    for iface in [1, 2]:
        usb.util.release_interface(dev, iface)

    print("Done")


if __name__ == "__main__":
    main()
