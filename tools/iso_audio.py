#!/usr/bin/env python3
"""
Asynchronous isochronous audio engine for the Wolverine's EP3 (interface 1).

The synchronous `dev.write(EP3, ...)` one-packet-at-a-time path leaves a micro-gap
between transfers (Python loop + GIL). An isochronous endpoint is unforgiving: any
1 ms frame without a queued packet is a hole, and spread across speech it sounds
robotic. The fix is to keep N transfers (each carrying several 1 ms iso packets)
permanently in flight, refilled and resubmitted from their completion callback, so
the USB host controller never runs out of data for the next frame.

We use python-libusb1 (`usb1`) for EP3 only; pyusb keeps EP1 (GIP) and EP2 (bulk).
Two independent libusb handles to the same device, each claiming different
interfaces — libusb allows this. The GIP bring-up (IDENTIFY/AUTH/FORMAT/POWER) runs
on EP1 via pyusb *before* we claim interface 1 here, so ordering matters (see the
caller in gip_init.py).

EP3 (from the descriptors): full-speed isochronous, wMaxPacketSize=228, bInterval=1
→ one packet per 1 ms frame (~1000 pkt/s), matching 48 kHz stereo S16 on OUT.

PCM framing (asymmetric — see CONTEXT.md):
  * OUT (headphones): raw S16LE 48 kHz stereo, 192 bytes/packet (48 frames), NO header.
  * IN  (mic): GIP-framed `60 21 <seq> <len> | <2B sub-header> | S16LE 24 kHz mono`.
"""

import ctypes
import threading
import time

import usb1

# EP3 endpoints (interface 1)
EP_AUDIO_OUT = 0x03
EP_AUDIO_IN  = 0x83
AUDIO_IFACE  = 1
AUDIO_ALT    = 1

# OUT: 192 PCM bytes per 1 ms frame (48 frames × 2 ch × 2 B). Each iso packet is
# a GIP AUDIO_SAMPLES frame — `60 21 <seq> <len>` header + these 192 PCM bytes —
# NOT raw PCM. xone's gip_copy_audio_samples() builds exactly this; the device
# desyncs and renders garbage (robotic voice) if the header is missing.
OUT_PCM_BYTES = 192
# IN: read up to the endpoint's max packet (228); actual_length tells the truth.
IN_PKT_BYTES  = 228

# Prime the playback ring to this depth before draining real audio, so bursty
# PipeWire delivery (one quantum at a time) never empties the ring mid-stream.
# ~40 ms cushion; rates match (192000 B/s both ways) so it stays roughly constant.
OUT_PRIME_BYTES = OUT_PCM_BYTES * 40

# How many 1 ms iso packets ride in each USB transfer, and how many transfers we
# keep permanently queued. runway = NUM * PKTS ms in flight; the underrun cushion
# is (NUM-1)*PKTS ms (when one transfer completes, NUM-1 are still queued).
#   OUT: 8 pkt × 6 transfers = 48 ms in flight (~40 ms cushion). Latency trade-off.
#   IN : 8 pkt × 4 transfers = 32 ms.
OUT_PKTS_PER_XFER = 8
OUT_NUM_XFERS     = 6
IN_PKTS_PER_XFER  = 8
IN_NUM_XFERS      = 4


class IsoAudio:
    """Async iso bridge between the Wolverine's EP3 and the PipeWire ring buffers.

    `pw` is the ctypes handle from gip_init.load_pipewire_bridge() (or None, in
    which case OUT streams silence and IN is discarded — keeps the DAC/ADC alive).
    `decode_header` / `cmd_audio_samples` are injected from gip_init to avoid a
    circular import and keep a single source of truth for GIP framing.
    """

    def __init__(self, pw, bus, address, decode_header, cmd_audio_samples,
                 build_out_header):
        self._pw   = pw
        self._bus  = bus
        self._addr = address
        self._decode_header = decode_header
        self._cmd_audio_samples = cmd_audio_samples
        # build_out_header(seq) -> bytes: the GIP AUDIO_SAMPLES header prefixed to
        # every OUT iso packet. Injected from gip_init so framing lives in one place.
        self._build_out_header = build_out_header

        self._ctx     = None
        self._handle  = None
        self._running = False
        self._thread  = None
        self._out_xfers = []
        self._in_xfers  = []

        # Per-OUT-transfer zero-copy ctypes view over its bytearray (kept alive)
        # plus its base address, so we can write the header and read PCM straight
        # into the transfer buffer with no intermediate copy.
        self._out_views = {}
        self._out_addr  = {}

        # GIP audio framing state (set up in start()).
        self._out_hdr      = None     # mutable header template; byte[2] is seq
        self._out_hdr_len  = 0
        self._out_pkt_size = 0        # header + OUT_PCM_BYTES, the iso packet stride
        self._out_seq      = 1        # GIP sequence, 1..255 (never 0)
        self._out_primed   = False

        # Diagnostics (updated from the event thread).
        self._out_pkts = 0
        self._out_silence = 0
        self._in_bytes = 0
        self._last_dbg = 0.0

    # ------------------------------------------------------------------ start
    def start(self):
        """Open the device via usb1, claim interface 1, prime and submit all
        transfers, and spin up the event thread. Raises on hard failure so the
        caller can fall back to the synchronous path."""
        self._ctx = usb1.USBContext()
        self._ctx.open()

        self._handle = self._open_matching_device()
        if self._handle is None:
            self._ctx.close()
            self._ctx = None
            raise RuntimeError(
                f"usb1: Wolverine not found at bus {self._bus} addr {self._addr}")

        # xpad was already detached by pyusb; interface 1 has no kernel driver.
        # Don't fight pyusb over auto-detach.
        try:
            self._handle.setAutoDetachKernelDriver(False)
        except Exception:
            pass

        self._handle.claimInterface(AUDIO_IFACE)
        self._handle.setInterfaceAltSetting(AUDIO_IFACE, AUDIO_ALT)

        # GIP AUDIO_SAMPLES header prefixed to every OUT packet. Its length is
        # constant (the packet_length varint for 192 doesn't vary), so only the
        # sequence byte (index 2) changes per packet.
        tmpl = bytes(self._build_out_header(1))
        self._out_hdr      = bytearray(tmpl)
        self._out_hdr_len  = len(tmpl)
        self._out_pkt_size = self._out_hdr_len + OUT_PCM_BYTES

        self._running = True
        self._setup_out_transfers()
        self._setup_in_transfers()

        self._last_dbg = time.time()
        self._thread = threading.Thread(target=self._event_loop, daemon=True)
        self._thread.start()

        n_out = len(self._out_xfers)
        n_in  = len(self._in_xfers)
        print(f"[iso] async EP3 engine up — OUT {n_out}×{OUT_PKTS_PER_XFER}pkt "
              f"({n_out*OUT_PKTS_PER_XFER}ms in flight), "
              f"IN {n_in}×{IN_PKTS_PER_XFER}pkt")
        return True

    def _open_matching_device(self):
        """Prefer the exact bus/address pyusb is using; fall back to VID/PID."""
        for dev in self._ctx.getDeviceIterator(skip_on_error=True):
            try:
                if (dev.getVendorID() == 0x1532 and dev.getProductID() == 0x0a14
                        and dev.getBusNumber() == self._bus
                        and dev.getDeviceAddress() == self._addr):
                    return dev.open()
            except usb1.USBError:
                continue
        # Fallback: any Wolverine.
        return self._ctx.openByVendorIDAndProductID(0x1532, 0x0a14)

    # -------------------------------------------------------------- OUT (play)
    def _setup_out_transfers(self):
        total = self._out_pkt_size * OUT_PKTS_PER_XFER
        for _ in range(OUT_NUM_XFERS):
            xfer = self._handle.getTransfer(iso_packets=OUT_PKTS_PER_XFER)
            ba   = bytearray(total)                          # starts as silence
            view = (ctypes.c_char * total).from_buffer(ba)   # zero-copy alias
            # Equal-length iso packets, each = header + OUT_PCM_BYTES.
            xfer.setIsochronous(EP_AUDIO_OUT, ba, callback=self._on_out)
            self._out_views[id(xfer)] = view
            self._out_addr[id(xfer)]  = ctypes.addressof(view)
            self._out_xfers.append(xfer)
            xfer.submit()

    def _on_out(self, xfer):
        if not self._running:
            return
        status = xfer.getStatus()
        if status in (usb1.TRANSFER_NO_DEVICE, usb1.TRANSFER_CANCELLED):
            return
        # For COMPLETED or transient errors (STALL/OVERFLOW) alike: refill and
        # resubmit. A dropped frame beats tearing the stream down.

        # Wait for a cushion before draining real audio, so a bursty PipeWire
        # quantum can't leave us mid-stream with an empty ring.
        if self._pw is not None and not self._out_primed:
            if self._pw.wpw_playback_avail() >= OUT_PRIME_BYTES:
                self._out_primed = True

        base = self._out_addr[id(xfer)]
        hlen = self._out_hdr_len
        for i in range(OUT_PKTS_PER_XFER):
            off = i * self._out_pkt_size

            # GIP header with the next sequence (1..255, never 0).
            self._out_hdr[2] = self._out_seq
            self._out_seq = self._out_seq + 1 if self._out_seq < 255 else 1
            ctypes.memmove(base + off, bytes(self._out_hdr), hlen)

            # PCM payload straight from the ring, else silence.
            pcm_at = base + off + hlen
            n = 0
            if self._pw is not None and self._out_primed:
                n = self._pw.wpw_read_playback(ctypes.c_void_p(pcm_at), OUT_PCM_BYTES)
            if n < OUT_PCM_BYTES:                         # underrun → pad silence
                ctypes.memset(pcm_at + n, 0, OUT_PCM_BYTES - n)
                self._out_silence += 1
            self._out_pkts += 1

        try:
            xfer.submit()
        except usb1.USBError:
            pass

    # --------------------------------------------------------------- IN (mic)
    def _setup_in_transfers(self):
        total = IN_PKT_BYTES * IN_PKTS_PER_XFER
        for _ in range(IN_NUM_XFERS):
            xfer = self._handle.getTransfer(iso_packets=IN_PKTS_PER_XFER)
            xfer.setIsochronous(EP_AUDIO_IN, total, callback=self._on_in)
            self._in_xfers.append(xfer)
            xfer.submit()

    def _on_in(self, xfer):
        if not self._running:
            return
        if xfer.getStatus() in (usb1.TRANSFER_NO_DEVICE, usb1.TRANSFER_CANCELLED):
            return
        for pkt_status, pkt in xfer.iterISO():
            if pkt_status != usb1.TRANSFER_COMPLETED or not pkt:
                continue
            data = bytes(pkt)
            if len(data) < 4 or data[0] != self._cmd_audio_samples:
                continue
            hdr = self._decode_header(data)
            if not hdr:
                continue
            _, _, _, hdr_len, pkt_len, _ = hdr
            payload = data[hdr_len:hdr_len + pkt_len]
            pcm = payload[2:]                         # skip 2-byte sub-header
            if pcm:
                self._in_bytes += len(pcm)
                if self._pw is not None:
                    self._pw.wpw_write_capture(pcm, len(pcm))
        try:
            xfer.submit()
        except usb1.USBError:
            pass

    # ---------------------------------------------------------- event thread
    def _event_loop(self):
        while self._running:
            try:
                self._ctx.handleEventsTimeout(0.1)
            except usb1.USBError:
                if self._running:
                    time.sleep(0.01)
            self._maybe_diag()

    def _maybe_diag(self):
        now = time.time()
        dt = now - self._last_dbg
        if dt < 5.0:
            return
        avail = self._pw.wpw_playback_avail() if self._pw else 0
        in_rate = self._in_bytes / dt
        print(f"[iso] OUT {self._out_pkts/dt:.0f} pkt/s "
              f"({self._out_silence} silent/5s, ring {avail}B) | "
              f"IN {in_rate:.0f} PCM B/s (~{in_rate/2:.0f} S16/s)")
        self._out_pkts = self._out_silence = self._in_bytes = 0
        self._last_dbg = now

    # --------------------------------------------------------------- shutdown
    def stop(self):
        if not self._running:
            return
        self._running = False

        # Cancel everything still in flight; callbacks fire with CANCELLED and,
        # because _running is now False, just return without resubmitting.
        for xfer in self._out_xfers + self._in_xfers:
            try:
                if xfer.isSubmitted():
                    xfer.cancel()
            except usb1.USBError:
                pass

        # Drain the cancellations so no submitted transfer is freed underfoot.
        deadline = time.time() + 1.0
        while time.time() < deadline:
            if not any(x.isSubmitted() for x in self._out_xfers + self._in_xfers):
                break
            try:
                self._ctx.handleEventsTimeout(0.05)
            except usb1.USBError:
                break

        if self._thread is not None:
            self._thread.join(timeout=1.0)

        # Drop zero-copy views before their bytearrays, then release/close.
        self._out_addr.clear()
        self._out_views.clear()
        self._out_xfers.clear()
        self._in_xfers.clear()

        try:
            self._handle.releaseInterface(AUDIO_IFACE)
        except Exception:
            pass
        try:
            self._handle.close()
        except Exception:
            pass
        try:
            self._ctx.close()
        except Exception:
            pass
        print("[iso] async EP3 engine stopped")
