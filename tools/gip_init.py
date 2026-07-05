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
GIP_AUD_CTRL_FORMAT  = 0x02
GIP_AUD_CTRL_VOLUME  = 0x03

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


def pkt_audio_format(seq: int) -> bytes:
    payload = bytes([GIP_AUD_CTRL_FORMAT,
                     GIP_AUD_FORMAT_48KHZ_STEREO,
                     GIP_AUD_FORMAT_48KHZ_STEREO])
    return build_packet(GIP_CMD_AUDIO_CONTROL, GIP_OPT_INTERNAL, seq, payload)


def pkt_audio_volume(seq: int, out_vol: int = 100, in_vol: int = 100) -> bytes:
    payload = bytes([GIP_AUD_CTRL_VOLUME, 0x04, out_vol, 100, in_vol, 0x00, 0x00, 0x00])
    return build_packet(GIP_CMD_AUDIO_CONTROL, GIP_OPT_INTERNAL, seq, payload)


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

EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
ABS_X, ABS_Y, ABS_Z = 0, 1, 2
ABS_RX, ABS_RY, ABS_RZ = 3, 4, 5
ABS_HAT0X, ABS_HAT0Y = 16, 17

BTN_A, BTN_B, BTN_X, BTN_Y = 304, 305, 307, 308
BTN_TL, BTN_TR = 310, 311
BTN_SELECT, BTN_START, BTN_MODE = 314, 315, 316
BTN_THUMBL, BTN_THUMBR = 317, 318

GAMEPAD_BUTTONS = [BTN_A, BTN_B, BTN_X, BTN_Y, BTN_TL, BTN_TR,
                   BTN_SELECT, BTN_START, BTN_MODE, BTN_THUMBL, BTN_THUMBR]
GAMEPAD_AXES = [ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ, ABS_HAT0X, ABS_HAT0Y]


def create_uinput_gamepad():
    """Create a virtual Xbox gamepad via uinput."""
    import fcntl, ctypes

    fd = open(UINPUT_PATH, "wb")

    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    for btn in GAMEPAD_BUTTONS:
        fcntl.ioctl(fd, UI_SET_KEYBIT, btn)

    fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
    for ax in GAMEPAD_AXES:
        fcntl.ioctl(fd, UI_SET_ABSBIT, ax)

    # uinput_setup struct: input_id (4×u16) + name (80 bytes) + ff_effects_max (u32)
    input_id = struct.pack("HHHH", 0x03, VENDOR_ID, PRODUCT_ID, 0x0101)
    name = b"Razer Wolverine Ultimate\x00" + b"\x00" * 56
    uinput_setup = input_id + name + struct.pack("I", 0)

    # uinput_abs_setup for each axis: code(u16) + pad(u16) + input_absinfo(6×i32)
    ABS_SETUP = 0x401853C0  # _IOW('U', 0xC0, struct uinput_abs_setup)
    for ax in [ABS_X, ABS_Y, ABS_RX, ABS_RY]:
        abs_setup = struct.pack("HHiiiiii", ax, 0, 0, -32768, 32767, 16, 128, 0)
        fcntl.ioctl(fd, ABS_SETUP, abs_setup)
    for ax in [ABS_Z, ABS_RZ]:
        abs_setup = struct.pack("HHiiiiii", ax, 0, 0, 0, 255, 0, 0, 0)
        fcntl.ioctl(fd, ABS_SETUP, abs_setup)
    for ax in [ABS_HAT0X, ABS_HAT0Y]:
        abs_setup = struct.pack("HHiiiiii", ax, 0, 0, -1, 1, 0, 0, 0)
        fcntl.ioctl(fd, ABS_SETUP, abs_setup)

    # UI_DEV_SETUP
    UI_DEV_SETUP = 0x405c5503
    fcntl.ioctl(fd, UI_DEV_SETUP, uinput_setup)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    return fd


def emit_event(fd, ev_type: int, code: int, value: int) -> None:
    # struct input_event: timeval(2×i64) + type(u16) + code(u16) + value(i32)
    t = time.time()
    sec = int(t)
    usec = int((t - sec) * 1_000_000)
    fd.write(struct.pack("qqHHi", sec, usec, ev_type, code, value))


def parse_and_forward_gamepad(fd, data: bytes) -> None:
    """Parse a GIP_CMD_INPUT (0x20) packet and forward via uinput."""
    # xpad/GIP input report layout (after 4-byte header):
    # bytes 0-1: buttons bitmask
    # byte  2:   left trigger
    # byte  3:   right trigger
    # bytes 4-5: left stick X (i16 LE)
    # bytes 6-7: left stick Y (i16 LE)
    # bytes 8-9: right stick X
    # bytes 10-11: right stick Y
    if len(data) < 4 + 12:
        return
    payload = data[4:]
    buttons = struct.unpack_from("<H", payload, 0)[0]
    lt, rt = payload[2], payload[3]
    lx, ly = struct.unpack_from("<hh", payload, 4)
    rx, ry = struct.unpack_from("<hh", payload, 8)

    btn_map = [
        (0x0001, BTN_SELECT), (0x0002, BTN_MODE),  (0x0004, BTN_START),
        (0x0008, BTN_A),      (0x0010, BTN_B),     (0x0020, BTN_X),
        (0x0040, BTN_Y),      (0x0100, BTN_TL),    (0x0200, BTN_TR),
        (0x1000, BTN_THUMBL), (0x2000, BTN_THUMBR),
    ]
    for mask, btn in btn_map:
        emit_event(fd, EV_KEY, btn, 1 if (buttons & mask) else 0)

    emit_event(fd, EV_ABS, ABS_X,  lx)
    emit_event(fd, EV_ABS, ABS_Y,  ly)
    emit_event(fd, EV_ABS, ABS_Z,  lt)
    emit_event(fd, EV_ABS, ABS_RX, rx)
    emit_event(fd, EV_ABS, ABS_RY, ry)
    emit_event(fd, EV_ABS, ABS_RZ, rt)
    emit_event(fd, EV_SYN, 0, 0)
    fd.flush()


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
    print("STEP 2 — AUDIO FORMAT (48kHz stereo)")
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
    print("STEP 3 — VOLUME (unmute, 100%)")
    print("=" * 60)
    gip_send(dev, pkt_audio_volume(seq), "VOLUME")
    resp = gip_recv(dev, "VOLUME response")
    seq += 1
    time.sleep(0.05)


# ---------------------------------------------------------------------------
# Monitor threads
# ---------------------------------------------------------------------------

def monitor_gip(dev, uinput_fd, stop: threading.Event) -> None:
    """Read EP1 IN — handle GIP messages: forward gamepad, log audio/media."""
    print("[gip] Monitoring EP1 IN...")
    while not stop.is_set():
        try:
            data = bytes(dev.read(EP_GIP_IN, 64, timeout=100))
            if not data:
                continue
            cmd = data[0]
            if cmd == GIP_CMD_INPUT:
                parse_and_forward_gamepad(uinput_fd, data)
            elif cmd == GIP_CMD_AUDIO_CONTROL:
                sub = data[4] if len(data) > 4 else 0xFF
                ts = time.strftime("%H:%M:%S")
                print(f"\n[gip {ts}] AUDIO_CONTROL subcommand=0x{sub:02x}:")
                hexdump(data)
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


def monitor_audio(dev, stop: threading.Event) -> None:
    """Read EP3 IN — isochronous mic stream."""
    print("[audio] Monitoring EP3 IN (isochronous mic)...")
    count = 0
    while not stop.is_set():
        try:
            data = bytes(dev.read(EP_AUDIO_IN, 228, timeout=100))
            if data:
                count += 1
                if count <= 3:
                    ts = time.strftime("%H:%M:%S")
                    print(f"\n[audio {ts}] EP3 IN {len(data)} bytes (#{count}):")
                    hexdump(data)
                elif count == 4:
                    print("[audio] ✓ Stream active — audio is flowing!")
        except usb.core.USBTimeoutError:
            if count > 0:
                print(f"[audio] stream paused after {count} packets")
                count = 0
        except usb.core.USBError as e:
            if not stop.is_set():
                print(f"[audio] error: {e}")
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

    for iface in [1, 2]:
        try:
            dev.set_interface_altsetting(interface=iface, alternate_setting=1)
            print(f"  Interface {iface} alt setting 1 active (endpoints enabled)")
        except usb.core.USBError as e:
            print(f"  Interface {iface} alt setting: {e}")

    print("\nCreating virtual gamepad (uinput)...")
    try:
        uinput_fd = create_uinput_gamepad()
        print("  ✓ Virtual gamepad active at /dev/uinput")
    except Exception as e:
        print(f"  WARNING: uinput failed ({e}) — gamepad forwarding disabled")
        uinput_fd = None

    time.sleep(0.1)

    gip_init(dev)

    print("\n" + "=" * 60)
    print("MONITORING — press buttons, media keys, plug headset")
    print("Ctrl+C to stop")
    print("=" * 60 + "\n")

    stop = threading.Event()
    threads = [
        threading.Thread(target=monitor_gip,   args=(dev, uinput_fd, stop), daemon=True),
        threading.Thread(target=monitor_ctrl,  args=(dev, stop), daemon=True),
        threading.Thread(target=monitor_audio, args=(dev, stop), daemon=True),
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
        import fcntl
        fcntl.ioctl(uinput_fd, UI_DEV_DESTROY)
        uinput_fd.close()

    print("Done")


if __name__ == "__main__":
    main()
