#!/usr/bin/env python3
"""
Razer Wolverine Ultimate — GIP audio initialization
Detaches xpad from interface 0, takes full control of the device,
implements GIP audio handshake, and forwards gamepad events via uinput.

Protocol reference: github.com/medusalix/xone, TheNathannator's GIP notes.
Run as root.
"""

import os
import sys
import time
import struct
import threading
import usb.core
import usb.util
from evdev import UInput, AbsInfo, ecodes

VENDOR_ID  = 0x1532
PRODUCT_ID = 0x0a14

# Endpoints
EP_GIP_OUT   = 0x01   # interrupt, interface 0 — main GIP channel
EP_GIP_IN    = 0x81
EP_CTRL_OUT  = 0x02   # bulk, interface 2 — secondary (purpose TBD)
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
GIP_CMD_INPUT         = 0x20
GIP_CMD_AUDIO_SAMPLES = 0x60

# GIP option flags
GIP_OPT_ACKNOWLEDGE = 0x10
GIP_OPT_INTERNAL    = 0x20

# Audio control subcommands
GIP_AUD_CTRL_VOLUME_CHAT = 0x00
GIP_AUD_CTRL_FORMAT      = 0x02
GIP_AUD_CTRL_VOLUME      = 0x03

# Audio format codes
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
    if len(header) % 2 != 0:
        last = length_bytes[-1]
        length_bytes = length_bytes[:-1] + bytes([last | 0x80, 0x00])
        header = bytes([cmd, options, seq]) + length_bytes
    return header + payload


def pkt_identify(seq: int) -> bytes:
    return build_packet(GIP_CMD_IDENTIFY, GIP_OPT_INTERNAL, seq)


def pkt_status(seq: int, status: int = 0x80) -> bytes:
    return build_packet(GIP_CMD_STATUS, GIP_OPT_INTERNAL, seq,
                        bytes([status, 0x00, 0x00, 0x00]))


AUD_CLIENT = 0x01  # audio subsystem uses client_id=1 (opts bit 0)

def pkt_audio_format(seq: int) -> bytes:
    payload = bytes([GIP_AUD_CTRL_FORMAT,
                     GIP_AUD_FORMAT_48KHZ_STEREO,
                     GIP_AUD_FORMAT_48KHZ_STEREO])
    return build_packet(GIP_CMD_AUDIO_CONTROL, GIP_OPT_INTERNAL | AUD_CLIENT, seq, payload)


def pkt_audio_volume(seq: int, out_vol: int = 100, in_vol: int = 100) -> bytes:
    payload = bytes([GIP_AUD_CTRL_VOLUME, 0x04, out_vol, 100, in_vol, 0x00, 0x00, 0x00])
    return build_packet(GIP_CMD_AUDIO_CONTROL, GIP_OPT_INTERNAL | AUD_CLIENT, seq, payload)


def pkt_audio_volume_chat(seq: int, state: int = 0x04,
                          v1: int = 25, v2: int = 25, v3: int = 100) -> bytes:
    """Mirror the device's own volume_chat format (subcommand 0x00)."""
    payload = bytes([GIP_AUD_CTRL_VOLUME_CHAT, state, v1, v2, v3])
    return build_packet(GIP_CMD_AUDIO_CONTROL, GIP_OPT_INTERNAL | AUD_CLIENT, seq, payload)


def pkt_ack(ack_cmd: int, ack_opts: int, ack_seq: int) -> bytes:
    payload = bytes([0x00, ack_cmd, ack_opts, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    return build_packet(GIP_CMD_ACKNOWLEDGE, GIP_OPT_INTERNAL, ack_seq, payload)


# ---------------------------------------------------------------------------
# Debug helpers
# ---------------------------------------------------------------------------

def hexdump(data: bytes, prefix: str = "  ") -> None:
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"{prefix}{i:04x}  {hex_part:<48}  {asc_part}")


def gip_send(dev, data: bytes, label: str) -> bool:
    print(f"\n→ [{label}] {len(data)} bytes:")
    hexdump(data)
    try:
        dev.write(EP_GIP_OUT, data, timeout=TIMEOUT_MS)
        return True
    except usb.core.USBError as e:
        print(f"  ERROR: {e}")
        return False


def gip_recv(dev, label: str) -> bytes | None:
    try:
        data = bytes(dev.read(EP_GIP_IN, 64, timeout=TIMEOUT_MS))
        print(f"\n← [{label}] {len(data)} bytes:")
        hexdump(data)
        return data
    except usb.core.USBTimeoutError:
        print(f"  [{label}] timeout")
        return None
    except usb.core.USBError as e:
        print(f"  [{label}] error: {e}")
        return None


# ---------------------------------------------------------------------------
# uinput gamepad forwarding
# ---------------------------------------------------------------------------

# uinput constants
UINPUT_PATH = "/dev/uinput"
UI_SET_EVBIT   = 0x40045564
UI_SET_KEYBIT  = 0x40045565
UI_SET_ABSBIT  = 0x40045567
UI_DEV_CREATE  = 0x5501
UI_DEV_DESTROY = 0x5502

def create_uinput_gamepad() -> UInput:
    return UInput(
        events={
            ecodes.EV_KEY: [
                ecodes.BTN_A, ecodes.BTN_B, ecodes.BTN_X, ecodes.BTN_Y,
                ecodes.BTN_TL, ecodes.BTN_TR,
                ecodes.BTN_SELECT, ecodes.BTN_START, ecodes.BTN_MODE,
                ecodes.BTN_THUMBL, ecodes.BTN_THUMBR,
            ],
            ecodes.EV_ABS: [
                (ecodes.ABS_X,     AbsInfo(0, -32768, 32767, 16, 128, 0)),
                (ecodes.ABS_Y,     AbsInfo(0, -32768, 32767, 16, 128, 0)),
                (ecodes.ABS_Z,     AbsInfo(0, 0, 255, 0, 0, 0)),
                (ecodes.ABS_RX,    AbsInfo(0, -32768, 32767, 16, 128, 0)),
                (ecodes.ABS_RY,    AbsInfo(0, -32768, 32767, 16, 128, 0)),
                (ecodes.ABS_RZ,    AbsInfo(0, 0, 255, 0, 0, 0)),
                (ecodes.ABS_HAT0X, AbsInfo(0, -1, 1, 0, 0, 0)),
                (ecodes.ABS_HAT0Y, AbsInfo(0, -1, 1, 0, 0, 0)),
            ],
        },
        name="Razer Wolverine Ultimate",
        vendor=VENDOR_ID,
        product=PRODUCT_ID,
        version=0x0101,
    )


def parse_and_forward_gamepad(ui: UInput, data: bytes) -> None:
    """Parse a GIP_CMD_INPUT (0x20) packet and forward via uinput."""
    if len(data) < 4 + 12:
        return
    payload = data[4:]
    buttons = struct.unpack_from("<H", payload, 0)[0]
    lt, rt = payload[2], payload[3]
    lx, ly = struct.unpack_from("<hh", payload, 4)
    rx, ry = struct.unpack_from("<hh", payload, 8)

    btn_map = [
        (0x0001, ecodes.BTN_SELECT), (0x0002, ecodes.BTN_MODE),
        (0x0004, ecodes.BTN_START),  (0x0008, ecodes.BTN_A),
        (0x0010, ecodes.BTN_B),      (0x0020, ecodes.BTN_X),
        (0x0040, ecodes.BTN_Y),      (0x0100, ecodes.BTN_TL),
        (0x0200, ecodes.BTN_TR),     (0x1000, ecodes.BTN_THUMBL),
        (0x2000, ecodes.BTN_THUMBR),
    ]
    for mask, btn in btn_map:
        ui.write(ecodes.EV_KEY, btn, 1 if (buttons & mask) else 0)

    ui.write(ecodes.EV_ABS, ecodes.ABS_X,     lx)
    ui.write(ecodes.EV_ABS, ecodes.ABS_Y,     ly)
    ui.write(ecodes.EV_ABS, ecodes.ABS_Z,     lt)
    ui.write(ecodes.EV_ABS, ecodes.ABS_RX,    rx)
    ui.write(ecodes.EV_ABS, ecodes.ABS_RY,    ry)
    ui.write(ecodes.EV_ABS, ecodes.ABS_RZ,    rt)
    ui.syn()


# ---------------------------------------------------------------------------
# GIP init sequence
# ---------------------------------------------------------------------------

def gip_init(dev) -> None:
    seq = 1

    print("\n" + "=" * 60)
    print("STEP 1 — IDENTIFY (discover supported audio formats)")
    print("=" * 60)
    gip_send(dev, pkt_identify(seq), "IDENTIFY")
    resp = gip_recv(dev, "IDENTIFY response")
    if resp and resp[0] == GIP_CMD_IDENTIFY:
        print("  ✓ Device acknowledged IDENTIFY")
        if resp[0] & GIP_OPT_ACKNOWLEDGE:
            dev.write(EP_GIP_OUT, pkt_ack(resp[0], resp[1], resp[2]), timeout=TIMEOUT_MS)
    seq += 1
    time.sleep(0.05)

    print("\n" + "=" * 60)
    print("STEP 2 — STATUS (host ready)")
    print("=" * 60)
    gip_send(dev, pkt_status(seq), "STATUS")
    resp = gip_recv(dev, "STATUS response")
    seq += 1
    time.sleep(0.05)

    print("\n" + "=" * 60)
    print("STEP 3 — AUDIO FORMAT (48kHz stereo)")
    print("=" * 60)
    gip_send(dev, pkt_audio_format(seq), "AUDIO_FORMAT")
    resp = gip_recv(dev, "AUDIO_FORMAT response")
    if resp:
        if resp[0] == GIP_CMD_AUDIO_CONTROL:
            sub = resp[4] if len(resp) > 4 else 0xFF
            print(f"  ✓ Audio control response, subcommand: 0x{sub:02x}")
        elif resp[0] == GIP_CMD_ACKNOWLEDGE:
            print("  ✓ ACK")
    seq += 1
    time.sleep(0.05)

    print("\n" + "=" * 60)
    print("STEP 4 — VOLUME (unmute, 100%)")
    print("=" * 60)
    gip_send(dev, pkt_audio_volume(seq), "VOLUME")
    resp = gip_recv(dev, "VOLUME response")
    seq += 1
    time.sleep(0.05)

    print("\n" + "=" * 60)
    print("STEP 4 — VOLUME_CHAT (unmute, announce audio ready)")
    print("=" * 60)
    gip_send(dev, pkt_audio_volume_chat(seq), "VOLUME_CHAT")
    resp = gip_recv(dev, "VOLUME_CHAT response")
    seq += 1
    time.sleep(0.05)


# ---------------------------------------------------------------------------
# Monitor threads
# ---------------------------------------------------------------------------

def monitor_gip(dev, ui: UInput | None, stop: threading.Event, seq_ref: list) -> None:
    """Read EP1 IN — handle GIP messages: forward gamepad, log audio/media."""
    print("[gip] Monitoring EP1 IN...")
    while not stop.is_set():
        try:
            data = bytes(dev.read(EP_GIP_IN, 64, timeout=100))
            if not data:
                continue
            cmd = data[0]
            opts = data[1] if len(data) > 1 else 0
            seq  = data[2] if len(data) > 2 else 0

            if cmd == GIP_CMD_INPUT:
                if ui is not None:
                    parse_and_forward_gamepad(ui, data)
            elif cmd == GIP_CMD_STATUS:
                # Device heartbeat — mirror it back so device knows host is alive
                s = seq_ref[0]; seq_ref[0] += 1
                try:
                    status_val = data[4] if len(data) > 4 else 0x80
                    dev.write(EP_GIP_OUT, pkt_status(s, status_val), timeout=TIMEOUT_MS)
                except usb.core.USBError:
                    pass
            elif cmd == GIP_CMD_AUDIO_CONTROL:
                sub = data[4] if len(data) > 4 else 0xFF
                ts = time.strftime("%H:%M:%S")
                print(f"\n[gip {ts}] AUDIO_CONTROL subcommand=0x{sub:02x}:")
                hexdump(data)
                # Device sends its volume state — echo back in same format (subcommand 0x00)
                if sub == 0x00 and len(data) >= 9:
                    state, v1, v2, v3 = data[5], data[6], data[7], data[8]
                    s = seq_ref[0]; seq_ref[0] += 1
                    try:
                        dev.write(EP_GIP_OUT,
                                  pkt_audio_volume_chat(s, state, v1, v2, v3),
                                  timeout=TIMEOUT_MS)
                        print(f"  → sent VOLUME_CHAT echo (seq={s}, state=0x{state:02x})")
                    except usb.core.USBError as e:
                        print(f"  → VOLUME_CHAT send failed: {e}")
                # ACK if requested
                if opts & GIP_OPT_ACKNOWLEDGE:
                    try:
                        dev.write(EP_GIP_OUT, pkt_ack(cmd, opts, seq), timeout=TIMEOUT_MS)
                    except usb.core.USBError:
                        pass
            else:
                ts = time.strftime("%H:%M:%S")
                print(f"\n[gip {ts}] cmd=0x{cmd:02x} ({len(data)} bytes):")
                hexdump(data)
        except usb.core.USBTimeoutError:
            pass
        except usb.core.USBError as e:
            if not stop.is_set():
                print(f"[gip] error: {e}")
            time.sleep(0.1)


def monitor_ctrl(dev, stop: threading.Event) -> None:
    """Read EP2 IN — secondary channel, media buttons hypothesis."""
    print("[ctrl] Monitoring EP2 IN (bulk)...")
    while not stop.is_set():
        try:
            data = bytes(dev.read(EP_CTRL_IN, 64, timeout=100))
            if data:
                ts = time.strftime("%H:%M:%S")
                print(f"\n[ctrl {ts}] EP2 IN cmd=0x{data[0]:02x} ({len(data)} bytes):")
                hexdump(data)
        except usb.core.USBTimeoutError:
            pass
        except usb.core.USBError as e:
            if not stop.is_set():
                print(f"[ctrl] error: {e}")
            time.sleep(0.1)


def stream_audio_out(dev, stop: threading.Event) -> None:
    """Send silence on EP3 OUT (isochronous) to open the bidirectional audio channel.
    Tries three formats in sequence to discover which one the device accepts."""
    candidates = [
        ("194B (2B header + 192B PCM)", struct.pack("<H", 192) + bytes(192)),
        ("192B raw PCM (no header)",    bytes(192)),
        ("228B raw (max packet)",       bytes(228)),
    ]
    fmt_name, silence = candidates[0]
    print(f"[audio-out] Streaming silence on EP3 OUT — format: {fmt_name}")
    sent = 0
    errors = 0
    last_log = time.time()
    fmt_idx = 0

    while not stop.is_set():
        try:
            dev.write(EP_AUDIO_OUT, silence, timeout=5)
            sent += 1
            errors = 0
        except usb.core.USBTimeoutError:
            pass
        except usb.core.USBError as e:
            errors += 1
            if errors == 1:
                print(f"[audio-out] write error: {e}")
            if errors > 100:
                # Try next format
                fmt_idx = (fmt_idx + 1) % len(candidates)
                fmt_name, silence = candidates[fmt_idx]
                print(f"[audio-out] switching format → {fmt_name}")
                errors = 0

        now = time.time()
        if now - last_log >= 5.0:
            print(f"[audio-out] {sent} packets sent in last 5s "
                  f"({sent/5:.0f}/s) — format: {fmt_name}")
            sent = 0
            last_log = now


def monitor_audio(dev, stop: threading.Event) -> None:
    """Read EP3 IN — isochronous mic stream."""
    print("[audio-in]  Monitoring EP3 IN (isochronous mic)...")
    count = 0
    while not stop.is_set():
        try:
            data = bytes(dev.read(EP_AUDIO_IN, 228, timeout=100))
            if data:
                count += 1
                if count <= 3:
                    ts = time.strftime("%H:%M:%S")
                    print(f"\n[audio-in {ts}] EP3 IN {len(data)} bytes (#{count}):")
                    hexdump(data)
                elif count == 4:
                    print("[audio-in] ✓ Stream active — mic audio is flowing!")
        except usb.core.USBTimeoutError:
            if count > 0:
                print(f"[audio-in] stream paused after {count} packets")
                count = 0
        except usb.core.USBError as e:
            if not stop.is_set():
                print(f"[audio-in] error: {e}")
            time.sleep(0.1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if os.geteuid() != 0:
        print("ERROR: run as root (sudo)")
        sys.exit(1)

    print("=== Razer Wolverine Ultimate — GIP Audio Init ===\n")

    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        print("ERROR: device not found")
        sys.exit(1)
    print(f"Found: {dev.manufacturer} {dev.product} (bus {dev.bus}, addr {dev.address})")

    print("\nDetaching kernel drivers and claiming all interfaces...")
    for iface in [0, 1, 2]:
        try:
            if dev.is_kernel_driver_active(iface):
                dev.detach_kernel_driver(iface)
                print(f"  xpad/kernel detached from interface {iface}")
            usb.util.claim_interface(dev, iface)
            print(f"  Interface {iface} claimed")
        except usb.core.USBError as e:
            print(f"  Interface {iface}: {e}")

    print("\nCreating virtual gamepad (uinput)...")
    try:
        uinput_fd = create_uinput_gamepad()
        print(f"  ✓ Virtual gamepad active (fd={uinput_fd.fd})")
    except Exception as e:
        print(f"  WARNING: uinput failed ({e}) — gamepad forwarding disabled")
        uinput_fd = None

    stop = threading.Event()
    seq_ref = [10]

    # GIP init with interfaces in alt=0 (no isochronous endpoints yet).
    # Standard USB audio: negotiate format first, then activate alt=1.
    print("\nRunning GIP init (isochronous endpoints still idle)...")
    gip_init(dev)

    # NOW activate alt=1 — isochronous endpoints come alive after format is agreed.
    print("\nActivating isochronous endpoints (alt setting 1)...")
    for iface in [1, 2]:
        try:
            dev.set_interface_altsetting(interface=iface, alternate_setting=1)
            print(f"  Interface {iface} alt setting 1 active")
        except usb.core.USBError as e:
            print(f"  Interface {iface} alt setting: {e}")

    time.sleep(0.1)

    # Start audio streams after alt=1 is live
    print("Starting EP3 audio streams...")
    t_audio_out = threading.Thread(target=stream_audio_out, args=(dev, stop), daemon=True)
    t_audio_in  = threading.Thread(target=monitor_audio,   args=(dev, stop), daemon=True)
    t_audio_out.start()
    t_audio_in.start()

    print("\n" + "=" * 60)
    print("MONITORING — press buttons, media keys, plug headset")
    print("Ctrl+C to stop")
    print("=" * 60 + "\n")

    threads = [
        threading.Thread(target=monitor_gip,  args=(dev, uinput_fd, stop, seq_ref), daemon=True),
        threading.Thread(target=monitor_ctrl, args=(dev, stop), daemon=True),
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

    for iface in [0, 1, 2]:
        try:
            usb.util.release_interface(dev, iface)
        except Exception:
            pass

    if uinput_fd:
        uinput_fd.close()

    print("Done")


if __name__ == "__main__":
    main()
