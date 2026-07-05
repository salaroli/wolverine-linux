#!/usr/bin/env python3
"""
Razer Wolverine Ultimate — USB interface probe
Monitors interfaces 1 (audio) and 2 (control/media) for traffic.
Run as root.
"""

import sys
import time
import threading
import usb.core
import usb.util

VENDOR_ID  = 0x1532
PRODUCT_ID = 0x0a14

EP_AUDIO_OUT = 0x03   # isochronous, interface 1 alt 1
EP_AUDIO_IN  = 0x83   # isochronous, interface 1 alt 1
EP_CTRL_OUT  = 0x02   # bulk, interface 2 alt 1
EP_CTRL_IN   = 0x82   # bulk, interface 2 alt 1
EP_HID_OUT   = 0x01   # interrupt, interface 0
EP_HID_IN    = 0x81   # interrupt, interface 0

TIMEOUT_MS = 100


def find_device():
    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        print("ERROR: Razer Wolverine Ultimate not found. Is it plugged in?")
        sys.exit(1)
    print(f"Found: {dev.manufacturer} {dev.product} (bus {dev.bus}, addr {dev.address})")
    return dev


def claim_interfaces(dev):
    for iface_num in [1, 2]:
        try:
            if dev.is_kernel_driver_active(iface_num):
                dev.detach_kernel_driver(iface_num)
                print(f"  Detached kernel driver from interface {iface_num}")
            usb.util.claim_interface(dev, iface_num)
            print(f"  Claimed interface {iface_num}")
        except usb.core.USBError as e:
            print(f"  WARNING: Could not claim interface {iface_num}: {e}")


def set_alt_settings(dev):
    for iface_num in [1, 2]:
        try:
            dev.set_interface_altsetting(interface=iface_num, alternate_setting=1)
            print(f"  Interface {iface_num} → alt setting 1 (endpoints active)")
        except usb.core.USBError as e:
            print(f"  WARNING: Could not set alt setting on interface {iface_num}: {e}")


def hexdump(data, prefix=""):
    if not data:
        return
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        asc_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"{prefix}{i:04x}  {hex_part:<48}  {asc_part}")


def monitor_ctrl_in(dev, stop_event):
    """Read bulk EP2 IN — media buttons and device events."""
    print("\n[ctrl] Monitoring EP2 IN (bulk) for media button events...")
    while not stop_event.is_set():
        try:
            data = dev.read(EP_CTRL_IN, 64, timeout=TIMEOUT_MS)
            if data:
                ts = time.strftime("%H:%M:%S")
                print(f"\n[ctrl {ts}] EP2 IN — {len(data)} bytes:")
                hexdump(bytes(data), prefix="  ")
        except usb.core.USBTimeoutError:
            pass
        except usb.core.USBError as e:
            if stop_event.is_set():
                break
            print(f"[ctrl] read error: {e}")
            time.sleep(0.1)


def monitor_audio_in(dev, stop_event):
    """Read isochronous EP3 IN — microphone input."""
    print("[audio] Monitoring EP3 IN (isochronous) for mic data...")
    packet_count = 0
    while not stop_event.is_set():
        try:
            data = dev.read(EP_AUDIO_IN, 228, timeout=TIMEOUT_MS)
            if data:
                packet_count += 1
                if packet_count <= 5:
                    ts = time.strftime("%H:%M:%S")
                    print(f"\n[audio {ts}] EP3 IN — {len(data)} bytes (packet #{packet_count}):")
                    hexdump(bytes(data), prefix="  ")
                elif packet_count == 6:
                    print("[audio] Stream active — suppressing further dumps (audio flowing)")
        except usb.core.USBTimeoutError:
            if packet_count > 0:
                print(f"[audio] Stream stopped after {packet_count} packets")
                packet_count = 0
        except usb.core.USBError as e:
            if stop_event.is_set():
                break
            print(f"[audio] read error: {e}")
            time.sleep(0.1)


def probe_hid_in(dev):
    """Read a few packets from the main HID interface (xpad) to see if media buttons appear there."""
    print("\n[hid] Sampling EP1 IN (xpad gamepad reports) for 2 seconds...")
    deadline = time.time() + 2.0
    seen = set()
    while time.time() < deadline:
        try:
            data = bytes(dev.read(EP_HID_IN, 64, timeout=TIMEOUT_MS))
            sig = data[:4]
            if sig not in seen:
                seen.add(sig)
                print(f"  New report type: {data[:8].hex()}")
        except usb.core.USBTimeoutError:
            pass
        except usb.core.USBError:
            pass
    print(f"[hid] Observed {len(seen)} distinct report type(s) on EP1 IN")


def main():
    if not hasattr(sys, 'real_prefix') and __import__('os').geteuid() != 0:
        print("WARNING: not running as root — USB claiming may fail")

    print("=== Razer Wolverine Ultimate — USB Probe ===\n")

    dev = find_device()

    print("\n[setup] Claiming interfaces...")
    claim_interfaces(dev)

    print("\n[setup] Activating alternate settings...")
    set_alt_settings(dev)

    probe_hid_in(dev)

    stop_event = threading.Event()

    t_ctrl  = threading.Thread(target=monitor_ctrl_in,  args=(dev, stop_event), daemon=True)
    t_audio = threading.Thread(target=monitor_audio_in, args=(dev, stop_event), daemon=True)

    t_ctrl.start()
    t_audio.start()

    print("\n[probe] Running — press Ctrl+C to stop\n")
    print("  → Press the media buttons on the controller now")
    print("  → Plug/unplug a headset into the controller jack\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[probe] Stopping...")
        stop_event.set()

    t_ctrl.join(timeout=1)
    t_audio.join(timeout=1)

    usb.util.release_interface(dev, 1)
    usb.util.release_interface(dev, 2)
    print("[probe] Done")


if __name__ == "__main__":
    main()
