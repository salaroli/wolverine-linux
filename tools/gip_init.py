#!/usr/bin/env python3
"""
Razer Wolverine Ultimate — GIP driver with full auth handshake.

Detaches xpad, claims all interfaces, performs the Xbox GIP authentication
(TLS-like RSA/ECDH handshake via cmd 0x06), negotiates audio format, then
forwards gamepad events via uinput and monitors audio/control endpoints.

Protocol: github.com/medusalix/xone  |  MS-GIPUSB open spec (Sep 2024)
Run as root.
"""

import os
import sys
import ctypes
import shutil
import subprocess
import time
import hmac
import struct
import hashlib
import threading
import usb.core
import usb.util
from evdev import UInput, AbsInfo, ecodes

try:
    from cryptography.hazmat.primitives.asymmetric import padding as _rsa_pad
    from cryptography.hazmat.primitives.serialization import load_der_public_key as _load_der_pub
    from cryptography.hazmat.primitives.asymmetric.ec import (
        ECDH as _ECDH,
        generate_private_key as _gen_ec_key,
        SECP256R1 as _P256,
        EllipticCurvePublicNumbers as _ECPubNums,
    )
    from cryptography.hazmat.backends import default_backend as _crypto_backend
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False
    print("WARNING: 'cryptography' library not found — GIP auth disabled")

VENDOR_ID  = 0x1532
PRODUCT_ID = 0x0a14

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

EP_GIP_OUT   = 0x01   # interrupt, interface 0
EP_GIP_IN    = 0x81
EP_CTRL_OUT  = 0x02   # bulk, interface 2
EP_CTRL_IN   = 0x82
EP_AUDIO_OUT = 0x03   # isochronous, interface 1
EP_AUDIO_IN  = 0x83

# ---------------------------------------------------------------------------
# GIP core command IDs (from xone bus/protocol.c)
# ---------------------------------------------------------------------------

GIP_CMD_ACKNOWLEDGE   = 0x01
GIP_CMD_ANNOUNCE      = 0x02
GIP_CMD_STATUS        = 0x03
GIP_CMD_IDENTIFY      = 0x04
GIP_CMD_POWER         = 0x05
GIP_CMD_AUTHENTICATE  = 0x06
GIP_CMD_AUDIO_CONTROL = 0x08
GIP_CMD_INPUT         = 0x20
GIP_CMD_AUDIO_SAMPLES = 0x60

# GIP option flags (bits in opts byte)
GIP_OPT_ACK         = 0x10   # request/confirm delivery ACK
GIP_OPT_INTERNAL    = 0x20   # internal command
GIP_OPT_CHUNK_START = 0x40   # first chunk of a large packet
GIP_OPT_CHUNK       = 0x80   # packet is part of a chunked sequence

GIP_CLIENT_ID = 0x01         # our client id (matches device)
GIP_PKT_MAX_LEN = 58         # max data bytes per interrupt packet

# Audio control subcommands
GIP_AUD_CTRL_VOLUME_CHAT = 0x00
GIP_AUD_CTRL_FORMAT      = 0x02
GIP_AUD_CTRL_VOLUME      = 0x03

GIP_AUD_FORMAT_48KHZ_STEREO = 0x10

# Power modes (GIP_CMD_POWER payload) — from xone enum gip_power_mode
GIP_PWR_ON    = 0x00
GIP_PWR_SLEEP = 0x01
GIP_PWR_OFF   = 0x04

# Send the hardware VOLUME command (sub 0x03)? In xone this is only sent for
# non-jack (standalone chat) headsets; for a 3.5mm jack it is skipped entirely,
# which is why the Wolverine always times out on it. Kept as a flag for probing.
SEND_HW_VOLUME = False

TIMEOUT_MS = 500

# ---------------------------------------------------------------------------
# GIP AUTH constants (from xone auth/auth.h + auth/auth.c)
# ---------------------------------------------------------------------------

# AUTH context bytes
AUTH_CTX_HANDSHAKE = 0x00
AUTH_CTX_CONTROL   = 0x01

# AUTH handshake option flags (different namespace from GIP opts)
AUTH_OPT_ACK       = 0x01   # device ACK-ing our packet
AUTH_OPT_REQUEST   = 0x02   # host requesting device to send data
AUTH_OPT_FROM_HOST = 0x40   # packet originates from host

# AUTH v1 command IDs (RSA-based)
AUTH_HOST_HELLO    = 0x01
AUTH_CLIENT_HELLO  = 0x02
AUTH_CLIENT_CERT   = 0x03
AUTH_HOST_SECRET   = 0x05
AUTH_HOST_FINISH   = 0x07
AUTH_CLIENT_FINISH = 0x08

# AUTH v2 command IDs (ECDH P-256)
AUTH2_HOST_HELLO    = 0x21
AUTH2_CLIENT_HELLO  = 0x22
AUTH2_CLIENT_CERT   = 0x23
AUTH2_CLIENT_PUBKEY = 0x24
AUTH2_HOST_PUBKEY   = 0x25
AUTH2_HOST_FINISH   = 0x26
AUTH2_CLIENT_FINISH = 0x27

# AUTH sizes
AUTH_TRAILER_LEN      = 8    # trailing zeros required for v1 host packets
AUTH_RANDOM_LEN       = 32
AUTH_CERT_MAX_LEN     = 1024
AUTH_RSA_PUBKEY_LEN   = 270  # DER SubjectPublicKeyInfo in cert
AUTH_PMS_LEN          = 48   # premaster secret
AUTH_ENCRYPTED_PMS_LEN = 256  # RSA 2048-bit output
AUTH_TRANSCRIPT_LEN   = 32
AUTH2_PUBKEY_LEN      = 64   # P-256 uncompressed (X+Y, no 04 prefix)

# ASN.1 SEQUENCE marker for RSA public key inside cert (xone: gip_auth_handle_pkt_certificate)
AUTH_ASN1_SEQ = bytes([0x30, 0x82, 0x01, 0x0a])

# ---------------------------------------------------------------------------
# GIP packet encoding / decoding
# ---------------------------------------------------------------------------

def encode_varint(value: int) -> bytes:
    result = []
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def decode_gip_header(data: bytes):
    """Decode GIP wire header.
    Returns (cmd, opts, seq, hdr_len, pkt_len, chunk_offset) or None.
    chunk_offset is meaningful only when GIP_OPT_CHUNK is set in opts.
    """
    if len(data) < 4:
        return None
    cmd, opts, seq = data[0], data[1], data[2]
    pos = 3

    pkt_len, shift = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        pkt_len |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break

    chunk_offset = 0
    if opts & GIP_OPT_CHUNK:
        shift = 0
        while pos < len(data):
            b = data[pos]; pos += 1
            chunk_offset |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break

    return (cmd, opts, seq, pos, pkt_len, chunk_offset)


def build_gip_header(cmd: int, opts: int, seq: int, pkt_len: int,
                     chunk_offset: int | None = None) -> bytes:
    """Build an even-length GIP wire header (with optional chunk_offset)."""
    len_varint = encode_varint(pkt_len)

    actual = 3 + len(len_varint)
    if chunk_offset is not None:
        chunk_varint = encode_varint(chunk_offset)
        actual += len(chunk_varint)
    else:
        chunk_varint = b''

    # Pad header to even length by setting continuation bit on last length byte
    if actual % 2 != 0:
        len_varint = len_varint[:-1] + bytes([len_varint[-1] | 0x80, 0x00])

    return bytes([cmd, opts, seq]) + len_varint + chunk_varint


def build_packet(cmd: int, options: int, seq: int, payload: bytes = b"") -> bytes:
    """Build a simple (non-chunked) GIP packet."""
    return build_gip_header(cmd, options, seq, len(payload)) + payload


# ---------------------------------------------------------------------------
# AUTH packet builders (mirroring xone auth/auth.c)
# ---------------------------------------------------------------------------

def _auth_hs_hdr(options: int, cmd: int, data_len: int) -> bytes:
    """6-byte AUTH handshake header."""
    return bytes([AUTH_CTX_HANDSHAKE, options, 0x00, cmd]) + struct.pack('>H', data_len)


def _auth_data_hdr(cmd: int, version: int, payload_len: int) -> bytes:
    """4-byte AUTH data header."""
    return bytes([cmd, version]) + struct.pack('>H', payload_len)


def _auth_build(cmd: int, version: int, inner: bytes) -> bytes:
    """Build a host-originated auth payload (hs_hdr + data_hdr + inner + trailer)."""
    data_hdr  = _auth_data_hdr(cmd, version, len(inner))
    data_len  = len(data_hdr) + len(inner)          # what hs_hdr.length covers
    hs_hdr    = _auth_hs_hdr(AUTH_OPT_ACK | AUTH_OPT_FROM_HOST, cmd, data_len)
    return hs_hdr + data_hdr + inner + bytes(AUTH_TRAILER_LEN)


def auth_host_hello_v1(random_host: bytes) -> bytes:
    """HOST_HELLO v1 auth payload — 58 bytes."""
    inner = random_host + bytes(8)           # random(32) + unknown1(4) + unknown2(4)
    return _auth_build(AUTH_HOST_HELLO, 0x01, inner)


def auth_host_hello_v2(random_host: bytes) -> bytes:
    """HOST_HELLO v2 auth payload."""
    inner = random_host + bytes(4)           # random(32) + unknown(4)
    return _auth_build(AUTH2_HOST_HELLO, 0x02, inner)


def auth_request(cmd: int, expected_payload_len: int) -> bytes:
    """14-byte request packet — tells device to send its data."""
    data_len = expected_payload_len + 4      # device data length + data_hdr
    hs_hdr   = _auth_hs_hdr(AUTH_OPT_REQUEST | AUTH_OPT_FROM_HOST, cmd, data_len)
    return hs_hdr + bytes(AUTH_TRAILER_LEN)


def auth_host_secret_v1(encrypted_pms: bytes) -> bytes:
    """HOST_SECRET auth payload — 274 bytes, needs chunking."""
    return _auth_build(AUTH_HOST_SECRET, 0x01, encrypted_pms)


def auth_host_pubkey_v2(pubkey: bytes) -> bytes:
    """HOST_PUBKEY v2 auth payload — 78 bytes."""
    return _auth_build(AUTH2_HOST_PUBKEY, 0x02, pubkey)


def auth_host_finish(cmd: int, transcript_hash: bytes) -> bytes:
    """HOST_FINISH or HOST_FINISH v2 auth payload — 50 bytes."""
    version = 0x02 if cmd >= 0x20 else 0x01
    return _auth_build(cmd, version, transcript_hash)


def auth_complete() -> bytes:
    """AUTH COMPLETE control message — 2 bytes."""
    return bytes([AUTH_CTX_CONTROL, 0x00])


# ---------------------------------------------------------------------------
# AUTH crypto helpers (mirroring xone auth/crypto.c)
# ---------------------------------------------------------------------------

def auth_prf(key: bytes, label: str, seed: bytes, length: int) -> bytes:
    """HMAC-SHA256 based PRF used for master secret and transcript verification."""
    label_b = label.encode('ascii')

    def h(k, d):
        return hmac.new(k, d, hashlib.sha256).digest()

    a = h(key, label_b + seed)          # A(1) = HMAC(key, label+seed)
    out = b''
    while len(out) < length:
        out += h(key, a + label_b + seed)
        a = h(key, a)                   # A(i+1) = HMAC(key, A(i))

    return out[:length]


def auth_extract_rsa_pubkey(cert: bytes):
    """Scan cert for ASN.1 SEQUENCE marker, extract 270-byte DER public key."""
    idx = cert.find(AUTH_ASN1_SEQ)
    if idx == -1:
        return None
    der = cert[idx:idx + AUTH_RSA_PUBKEY_LEN]
    if len(der) < AUTH_RSA_PUBKEY_LEN:
        return None
    try:
        return _load_der_pub(der)
    except Exception as e:
        print(f"  [auth] pubkey parse error: {e}")
        return None


def auth_rsa_encrypt(pubkey, pms: bytes) -> bytes:
    """Encrypt premaster secret with device RSA pubkey (PKCS1v15)."""
    return pubkey.encrypt(pms, _rsa_pad.PKCS1v15())


def auth_ecdh_exchange(client_pubkey_bytes: bytes):
    """Generate host P-256 keypair, compute shared secret.
    Returns (host_pubkey_bytes: 64, master_secret_seed: 32).
    """
    curve = _P256()
    host_priv = _gen_ec_key(curve, _crypto_backend())

    x = int.from_bytes(client_pubkey_bytes[:32], 'big')
    y = int.from_bytes(client_pubkey_bytes[32:], 'big')
    client_pub = _ECPubNums(x, y, curve).public_key(_crypto_backend())

    shared = host_priv.exchange(_ECDH(), client_pub)
    secret_hash = hashlib.sha256(shared).digest()     # matches xone crypto.c

    nums = host_priv.public_key().public_numbers()
    host_pub = nums.x.to_bytes(32, 'big') + nums.y.to_bytes(32, 'big')
    return host_pub, secret_hash


# ---------------------------------------------------------------------------
# Low-level GIP send/receive helpers for auth
# ---------------------------------------------------------------------------

def _gip_auth_send_simple(dev, auth_payload: bytes, seq: int) -> None:
    """Send auth payload as a single non-chunked GIP AUTH packet."""
    opts = GIP_CLIENT_ID | GIP_OPT_INTERNAL | GIP_OPT_ACK
    pkt  = build_packet(GIP_CMD_AUTHENTICATE, opts, seq, auth_payload)
    dev.write(EP_GIP_OUT, pkt, timeout=TIMEOUT_MS)


def _gip_auth_send_no_ack(dev, auth_payload: bytes, seq: int) -> None:
    """Send auth payload without requesting GIP ACK (for COMPLETE message)."""
    opts = GIP_CLIENT_ID | GIP_OPT_INTERNAL
    pkt  = build_packet(GIP_CMD_AUTHENTICATE, opts, seq, auth_payload)
    dev.write(EP_GIP_OUT, pkt, timeout=TIMEOUT_MS)


def _gip_auth_send_chunked(dev, auth_payload: bytes, seq: int) -> None:
    """Send a large auth payload in chunks, waiting for device ACK per chunk."""
    total   = len(auth_payload)
    base_opts = GIP_CLIENT_ID | GIP_OPT_INTERNAL

    # First chunk: CHUNK_START | CHUNK | ACK, chunk_offset = total length
    first_opts = base_opts | GIP_OPT_ACK | GIP_OPT_CHUNK_START | GIP_OPT_CHUNK
    first_hdr  = build_gip_header(GIP_CMD_AUTHENTICATE, first_opts, seq,
                                  GIP_PKT_MAX_LEN, chunk_offset=total)
    dev.write(EP_GIP_OUT, first_hdr + auth_payload[:GIP_PKT_MAX_LEN],
              timeout=TIMEOUT_MS)

    offset    = GIP_PKT_MAX_LEN
    remaining = total - GIP_PKT_MAX_LEN
    chunk_opts = base_opts | GIP_OPT_CHUNK   # subsequent chunks

    while remaining > 0:
        # Wait for device ACK
        if not _wait_for_gip_ack(dev, cmd=GIP_CMD_AUTHENTICATE, timeout_sec=3.0):
            print("  [auth] chunked send: ACK timeout")
            break

        chunk_size = min(remaining, GIP_PKT_MAX_LEN)
        is_last    = (chunk_size == remaining)
        co         = chunk_opts | (GIP_OPT_ACK if is_last else 0)

        hdr = build_gip_header(GIP_CMD_AUTHENTICATE, co, seq,
                               chunk_size, chunk_offset=offset)
        dev.write(EP_GIP_OUT, hdr + auth_payload[offset:offset + chunk_size],
                  timeout=TIMEOUT_MS)
        offset    += chunk_size
        remaining -= chunk_size

    # Wait for ACK on last chunk
    _wait_for_gip_ack(dev, cmd=GIP_CMD_AUTHENTICATE, timeout_sec=3.0)

    # Empty chunk signals transfer complete
    empty_opts = chunk_opts
    empty_hdr  = build_gip_header(GIP_CMD_AUTHENTICATE, empty_opts, seq,
                                  0, chunk_offset=total)
    dev.write(EP_GIP_OUT, empty_hdr, timeout=TIMEOUT_MS)


def _wait_for_gip_ack(dev, cmd: int, timeout_sec: float = 2.0) -> bool:
    """Wait for a GIP-level ACK (cmd=0x01) for the given command."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            raw = bytes(dev.read(EP_GIP_IN, 64, timeout=200))
        except usb.core.USBTimeoutError:
            continue
        except usb.core.USBError:
            continue

        if not raw:
            continue
        h = decode_gip_header(raw)
        if not h:
            continue
        rcmd, ropts, rseq, hdr_len, pkt_len, _ = h

        if rcmd == GIP_CMD_ACKNOWLEDGE:
            payload = raw[hdr_len:hdr_len + pkt_len]
            # payload[1] = command being ACK'd
            if len(payload) >= 2 and payload[1] == cmd:
                return True
        # Other packets (INPUT, STATUS, AUTH…): ignore and keep waiting
    return False


def _send_gip_ack_for_chunk(dev, recv_seq: int, recv_cmd: int,
                             received_so_far: int, total_len: int) -> None:
    """Send GIP ACK in response to a received chunk (from device to us)."""
    remaining = total_len - received_so_far
    # gip_pkt_acknowledge: unknown(1) + cmd(1) + opts(1) + length_le16 + pad(2) + remaining_le16
    payload = (bytes([0x00, recv_cmd, GIP_CLIENT_ID | GIP_OPT_INTERNAL]) +
               struct.pack('<H', received_so_far) +
               bytes([0x00, 0x00]) +
               struct.pack('<H', remaining))
    opts = GIP_CLIENT_ID | GIP_OPT_INTERNAL
    pkt  = build_packet(GIP_CMD_ACKNOWLEDGE, opts, recv_seq, payload)
    dev.write(EP_GIP_OUT, pkt, timeout=TIMEOUT_MS)


def _recv_auth_pkt(dev, timeout_sec: float = 5.0):
    """Receive an AUTH payload from the device (handles chunking transparently).
    Returns (auth_payload: bytes, recv_seq: int) or (None, None) on timeout.
    """
    deadline   = time.time() + timeout_sec
    chunk_buf  = None
    chunk_total  = 0
    chunk_recvd  = 0
    chunk_seq    = 0

    while time.time() < deadline:
        ms_left = max(50, int((deadline - time.time()) * 1000))
        try:
            raw = bytes(dev.read(EP_GIP_IN, 64, timeout=min(ms_left, 300)))
        except usb.core.USBTimeoutError:
            continue
        except usb.core.USBError:
            continue

        if not raw:
            continue

        h = decode_gip_header(raw)
        if not h:
            continue
        rcmd, ropts, rseq, hdr_len, pkt_len, chunk_offset = h

        if rcmd != GIP_CMD_AUTHENTICATE:
            # Log everything non-AUTH so we can see the device's true response
            print(f"  [auth] non-AUTH pkt: cmd=0x{rcmd:02x} opts=0x{ropts:02x} "
                  f"seq={rseq} pkt_len={pkt_len}B chunk_off={chunk_offset}")
            if pkt_len > 0:
                _hexdump(raw[hdr_len:hdr_len + min(pkt_len, 32)])
        else:
            # AUTH packet
            chunk_data = raw[hdr_len:hdr_len + pkt_len]

            if ropts & GIP_OPT_CHUNK_START:
                # First chunk: chunk_offset = total expected length
                chunk_total = chunk_offset
                chunk_buf   = bytearray(chunk_total)
                chunk_seq   = rseq
                chunk_buf[0:pkt_len] = chunk_data
                chunk_recvd = pkt_len
                # ACK: chunk_offset was reset to 0 before ACK in xone
                if ropts & GIP_OPT_ACK:
                    _send_gip_ack_for_chunk(dev, rseq, GIP_CMD_AUTHENTICATE,
                                            chunk_recvd, chunk_total)

            elif ropts & GIP_OPT_CHUNK:
                if chunk_buf is None:
                    continue  # missed start
                if pkt_len == 0:
                    # Empty chunk = transfer complete
                    return bytes(chunk_buf[:chunk_recvd]), chunk_seq
                end = chunk_offset + pkt_len
                chunk_buf[chunk_offset:end] = chunk_data
                chunk_recvd = end
                if ropts & GIP_OPT_ACK:
                    _send_gip_ack_for_chunk(dev, rseq, GIP_CMD_AUTHENTICATE,
                                            chunk_recvd, chunk_total)
                if chunk_recvd >= chunk_total:
                    # All data received (device may or may not send empty chunk)
                    return bytes(chunk_buf), chunk_seq

            else:
                # Non-chunked AUTH packet
                return chunk_data, rseq

    return None, None


# ---------------------------------------------------------------------------
# GIP authentication state machine
# ---------------------------------------------------------------------------

class _AuthState:
    def __init__(self):
        self.v2             = False
        self.random_host    = os.urandom(AUTH_RANDOM_LEN)
        self.random_client  = None
        self.transcript     = hashlib.sha256()
        self.master_secret  = None
        self.complete       = False
        self.last_sent_cmd  = None

    def transcript_sent(self, auth_payload: bytes) -> None:
        """Update transcript with a host-originated auth payload (skip 6-byte hs_hdr and trailer)."""
        # data = everything between handshake header and trailer
        data_len = len(auth_payload) - 6 - AUTH_TRAILER_LEN
        if data_len > 0:
            self.transcript.update(auth_payload[6:6 + data_len])

    def transcript_recv(self, auth_payload: bytes) -> None:
        """Update transcript with a device-originated auth payload (skip 6-byte hs_hdr only)."""
        if len(auth_payload) > 6:
            self.transcript.update(auth_payload[6:])

    def get_transcript_hash(self) -> bytes:
        return self.transcript.copy().digest()


def _hexdump(data: bytes, prefix: str = "  ") -> None:
    for i in range(0, min(len(data), 96), 16):
        chunk   = data[i:i + 16]
        hex_p   = " ".join(f"{b:02x}" for b in chunk)
        asc_p   = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"{prefix}{i:04x}  {hex_p:<48}  {asc_p}")


def gip_auth_handshake(dev, seq_ref: list) -> bool:
    """Perform the full GIP authentication handshake.

    Auth is a custom TLS-like protocol over GIP cmd 0x06:
      v1 (RSA):  HOST_HELLO → CLIENT_HELLO → CLIENT_CERT → HOST_SECRET → FINISH
      v2 (ECDH): HOST_HELLO_v2 → CLIENT_HELLO_v2 → CLIENT_CERT_v2 →
                 CLIENT_PUBKEY_v2 → HOST_PUBKEY_v2 → FINISH

    Returns True if auth completed successfully.
    """
    if not _CRYPTO_OK:
        print("  [auth] skipping auth — cryptography library missing")
        return False

    state = _AuthState()

    # ------------------------------------------------------------------
    # STEP A — send HOST_HELLO v1 (device may upgrade to v2)
    # ------------------------------------------------------------------
    print("\n[auth] → HOST_HELLO v1")
    hello_v1 = auth_host_hello_v1(state.random_host)
    _gip_auth_send_simple(dev, hello_v1, seq_ref[0])
    seq_ref[0] += 1
    state.transcript_sent(hello_v1)
    state.last_sent_cmd = AUTH_HOST_HELLO

    # ------------------------------------------------------------------
    # STEP B — wait for device AUTH-level ACK (cmd 0x06 with AUTH_OPT_ACK)
    # ------------------------------------------------------------------
    auth_ack, ack_seq = _recv_auth_pkt(dev, timeout_sec=8.0)
    if auth_ack is None:
        print("  [auth] timeout waiting for AUTH ACK (device sent nothing)")
        return False

    print(f"  [auth] ← AUTH response ({len(auth_ack)} bytes):")
    _hexdump(auth_ack)

    if len(auth_ack) < 6:
        print("  [auth] response too short")
        return False

    hs_opts = auth_ack[1]
    hs_cmd  = auth_ack[3]

    # Check for AUTH v2 upgrade: device sends response where handshake.command != data.command
    # (xone: gip_auth_process_pkt_data checks `hdr->handshake.command != hdr->data.command`)
    data_cmd = auth_ack[7] if len(auth_ack) > 7 else hs_cmd
    if hs_cmd != data_cmd:
        print("  [auth] device requests AUTH v2 (ECDH) — restarting")
        state.v2 = True
        state.transcript = hashlib.sha256()   # reset transcript for v2
        state.random_host = os.urandom(AUTH_RANDOM_LEN)

        hello_v2 = auth_host_hello_v2(state.random_host)
        _gip_auth_send_simple(dev, hello_v2, seq_ref[0])
        seq_ref[0] += 1
        state.transcript_sent(hello_v2)
        state.last_sent_cmd = AUTH2_HOST_HELLO

        auth_ack, ack_seq = _recv_auth_pkt(dev, timeout_sec=4.0)
        if auth_ack is None:
            print("  [auth] v2: timeout waiting for AUTH ACK")
            return False
        hs_opts = auth_ack[1]

    if not (hs_opts & AUTH_OPT_ACK):
        print("  [auth] expected AUTH-level ACK from device")

    # ------------------------------------------------------------------
    # STEP C — request CLIENT_HELLO
    # ------------------------------------------------------------------
    if state.v2:
        client_hello_cmd  = AUTH2_CLIENT_HELLO
        client_hello_size = 32 + 108 + 32   # random + unknown1 + unknown2
        client_cert_cmd   = AUTH2_CLIENT_CERT
        client_cert_size  = 768             # gip_auth2_pkt_client_cert
    else:
        client_hello_cmd  = AUTH_CLIENT_HELLO
        client_hello_size = AUTH_RANDOM_LEN + 48
        client_cert_cmd   = AUTH_CLIENT_CERT
        client_cert_size  = AUTH_CERT_MAX_LEN

    print(f"\n[auth] → REQUEST CLIENT_HELLO")
    req = auth_request(client_hello_cmd, client_hello_size)
    _gip_auth_send_simple(dev, req, seq_ref[0])
    seq_ref[0] += 1

    # ------------------------------------------------------------------
    # STEP D — receive CLIENT_HELLO
    # ------------------------------------------------------------------
    ch_data, _ = _recv_auth_pkt(dev, timeout_sec=5.0)
    if ch_data is None:
        print("  [auth] timeout waiting for CLIENT_HELLO")
        return False
    print(f"  [auth] ← CLIENT_HELLO ({len(ch_data)} bytes)")

    # Extract client random from data portion (after 6-byte hs_hdr + 4-byte data_hdr)
    if len(ch_data) < 10 + AUTH_RANDOM_LEN:
        print("  [auth] CLIENT_HELLO too short")
        return False
    state.random_client = ch_data[10:10 + AUTH_RANDOM_LEN]
    state.transcript_recv(ch_data)
    print(f"  [auth] client random: {state.random_client[:8].hex()}…")

    # ------------------------------------------------------------------
    # STEP E — request CLIENT_CERTIFICATE
    # ------------------------------------------------------------------
    print(f"\n[auth] → REQUEST CLIENT_CERTIFICATE")
    req = auth_request(client_cert_cmd, client_cert_size)
    _gip_auth_send_simple(dev, req, seq_ref[0])
    seq_ref[0] += 1

    cert_data, _ = _recv_auth_pkt(dev, timeout_sec=8.0)
    if cert_data is None:
        print("  [auth] timeout waiting for CLIENT_CERTIFICATE")
        return False
    print(f"  [auth] ← CLIENT_CERTIFICATE ({len(cert_data)} bytes):")
    _hexdump(cert_data[:64])
    state.transcript_recv(cert_data)

    # Certificate payload starts at offset 10 (6 hs_hdr + 4 data_hdr)
    cert_payload = cert_data[10:]

    if state.v2:
        # ------------------------------------------------------------------
        # AUTH v2: request CLIENT_PUBKEY (ECDH)
        # ------------------------------------------------------------------
        print(f"\n[auth] → REQUEST CLIENT_PUBKEY (v2)")
        req = auth_request(AUTH2_CLIENT_PUBKEY, AUTH2_PUBKEY_LEN + 64)
        _gip_auth_send_simple(dev, req, seq_ref[0])
        seq_ref[0] += 1

        pk_data, _ = _recv_auth_pkt(dev, timeout_sec=5.0)
        if pk_data is None:
            print("  [auth] v2: timeout waiting for CLIENT_PUBKEY")
            return False
        print(f"  [auth] ← CLIENT_PUBKEY v2 ({len(pk_data)} bytes)")
        state.transcript_recv(pk_data)

        client_pubkey_bytes = pk_data[10:10 + AUTH2_PUBKEY_LEN]

        print(f"\n[auth] computing ECDH P-256 key exchange…")
        host_pubkey, ecdh_secret = auth_ecdh_exchange(client_pubkey_bytes)

        rand_seed = state.random_host + state.random_client
        state.master_secret = auth_prf(ecdh_secret, "Master Secret",
                                       rand_seed, AUTH_PMS_LEN)
        print(f"  [auth] master secret computed (v2 ECDH)")

        # Send HOST_PUBKEY v2
        print(f"\n[auth] → HOST_PUBKEY v2")
        hpk_pkt = auth_host_pubkey_v2(host_pubkey)
        _gip_auth_send_simple(dev, hpk_pkt, seq_ref[0])
        seq_ref[0] += 1
        state.transcript_sent(hpk_pkt)
        state.last_sent_cmd = AUTH2_HOST_PUBKEY

        # Wait for ACK
        ack2, _ = _recv_auth_pkt(dev, timeout_sec=4.0)
        if ack2:
            print(f"  [auth] ← ACK for HOST_PUBKEY")

        finish_cmd        = AUTH2_HOST_FINISH
        client_finish_cmd = AUTH2_CLIENT_FINISH

    else:
        # ------------------------------------------------------------------
        # AUTH v1: RSA encrypt premaster secret
        # ------------------------------------------------------------------
        print(f"\n[auth] extracting RSA public key from certificate…")
        pubkey = auth_extract_rsa_pubkey(cert_payload)
        if pubkey is None:
            print("  [auth] ERROR: RSA pubkey not found in certificate")
            return False
        print(f"  [auth] RSA pubkey found ({pubkey.key_size} bit)")

        pms = os.urandom(AUTH_PMS_LEN)
        encrypted_pms = auth_rsa_encrypt(pubkey, pms)
        print(f"  [auth] PMS encrypted ({len(encrypted_pms)} bytes)")

        rand_seed = state.random_host + state.random_client
        state.master_secret = auth_prf(pms, "Master Secret", rand_seed, AUTH_PMS_LEN)
        print(f"  [auth] master secret computed (v1 RSA)")

        # Send HOST_SECRET (274 bytes, needs chunking)
        print(f"\n[auth] → HOST_SECRET (chunked)")
        secret_pkt = auth_host_secret_v1(encrypted_pms)
        _gip_auth_send_chunked(dev, secret_pkt, seq_ref[0])
        seq_ref[0] += 1
        state.transcript_sent(secret_pkt)
        state.last_sent_cmd = AUTH_HOST_SECRET

        # Wait for AUTH-level ACK on HOST_SECRET
        ack2, _ = _recv_auth_pkt(dev, timeout_sec=4.0)
        if ack2:
            print(f"  [auth] ← ACK for HOST_SECRET")

        finish_cmd        = AUTH_HOST_FINISH
        client_finish_cmd = AUTH_CLIENT_FINISH

    # ------------------------------------------------------------------
    # STEP F — HOST_FINISH
    # ------------------------------------------------------------------
    print(f"\n[auth] → HOST_FINISH")
    transcript_hash = state.get_transcript_hash()
    finish_transcript = auth_prf(state.master_secret, "Host Finished",
                                 transcript_hash, AUTH_TRANSCRIPT_LEN)
    finish_pkt = auth_host_finish(finish_cmd, finish_transcript)
    _gip_auth_send_simple(dev, finish_pkt, seq_ref[0])
    seq_ref[0] += 1
    state.transcript_sent(finish_pkt)

    # Wait for ACK
    ack3, _ = _recv_auth_pkt(dev, timeout_sec=4.0)
    if ack3:
        print(f"  [auth] ← ACK for HOST_FINISH")

    # ------------------------------------------------------------------
    # STEP G — request and verify CLIENT_FINISH
    # ------------------------------------------------------------------
    print(f"\n[auth] → REQUEST CLIENT_FINISH")
    req = auth_request(client_finish_cmd, AUTH_TRANSCRIPT_LEN + 32)
    _gip_auth_send_simple(dev, req, seq_ref[0])
    seq_ref[0] += 1

    cf_data, _ = _recv_auth_pkt(dev, timeout_sec=5.0)
    if cf_data is None:
        print("  [auth] timeout waiting for CLIENT_FINISH")
        return False
    print(f"  [auth] ← CLIENT_FINISH ({len(cf_data)} bytes)")

    # Verify transcript: compute expected "Device Finished" using transcript AFTER HOST_FINISH
    transcript_hash2 = state.get_transcript_hash()   # now includes HOST_FINISH
    expected_finish = auth_prf(state.master_secret, "Device Finished",
                               transcript_hash2, AUTH_TRANSCRIPT_LEN)
    client_transcript = cf_data[10:10 + AUTH_TRANSCRIPT_LEN]  # after hs+data headers

    if client_transcript == expected_finish:
        print("  [auth] ✓ CLIENT_FINISH transcript verified!")
    else:
        print("  [auth] ✗ CLIENT_FINISH transcript MISMATCH — auth may still work")
        print(f"    expected: {expected_finish.hex()}")
        print(f"    received: {client_transcript.hex()}")

    # ------------------------------------------------------------------
    # STEP H — AUTH COMPLETE control message
    # ------------------------------------------------------------------
    print(f"\n[auth] → AUTH COMPLETE")
    complete_pkt = auth_complete()
    _gip_auth_send_no_ack(dev, complete_pkt, seq_ref[0])
    seq_ref[0] += 1

    state.complete = True
    print("  [auth] ✓ Handshake complete!")
    time.sleep(0.1)
    return True


# ---------------------------------------------------------------------------
# Existing GIP packet helpers (unchanged)
# ---------------------------------------------------------------------------

def pkt_identify(seq: int) -> bytes:
    return build_packet(GIP_CMD_IDENTIFY, GIP_CLIENT_ID | GIP_OPT_INTERNAL, seq)


def pkt_status(seq: int, status: int = 0x80) -> bytes:
    return build_packet(GIP_CMD_STATUS, GIP_CLIENT_ID | GIP_OPT_INTERNAL, seq,
                        bytes([status, 0x00, 0x00, 0x00]))


AUD_CLIENT = 0x01

def pkt_power(seq: int, mode: int = GIP_PWR_ON) -> bytes:
    """GIP_CMD_POWER (0x05). xone sends GIP_PWR_ON right after negotiating the
    audio format to actually bring the audio subsystem online. Payload is a
    single mode byte. Without this the DAC/ADC stays idle and EP3 returns zeros.
    """
    return build_packet(GIP_CMD_POWER, GIP_CLIENT_ID | GIP_OPT_INTERNAL, seq,
                        bytes([mode]))


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
        chunk   = data[i:i + 16]
        hex_p   = " ".join(f"{b:02x}" for b in chunk)
        asc_p   = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"{prefix}{i:04x}  {hex_p:<48}  {asc_p}")


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


def create_uinput_media() -> UInput:
    """Separate keyboard-only device for the media buttons. Kept apart from the
    gamepad because libinput classifies a device with ABS axes + gamepad buttons
    as a joystick and does NOT deliver its KEY_* events to Wayland compositors.
    A pure EV_KEY device is seen as a keyboard, so the media keys reach Hyprland.
    """
    return UInput(
        events={
            ecodes.EV_KEY: [
                ecodes.KEY_VOLUMEUP, ecodes.KEY_VOLUMEDOWN,
                ecodes.KEY_MUTE, ecodes.KEY_MICMUTE,
            ],
        },
        name="Razer Wolverine Ultimate Media Keys",
        vendor=VENDOR_ID,
        product=PRODUCT_ID,
        version=0x0101,
    )


def parse_and_forward_gamepad(ui: UInput, data: bytes) -> None:
    if len(data) < 4 + 12:
        return
    payload = data[4:]
    buttons = struct.unpack_from("<H", payload, 0)[0]
    lt, rt  = payload[2], payload[3]
    lx, ly  = struct.unpack_from("<hh", payload, 4)
    rx, ry  = struct.unpack_from("<hh", payload, 8)

    # Log extra bytes that might be media buttons
    if len(payload) >= 14:
        extra = payload[12:14]
        if any(extra):
            ts = time.strftime("%H:%M:%S")
            print(f"\n[input {ts}] EXTRA bytes 12-13: {extra.hex()} (media buttons?)")

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
# Media buttons (headset audio controls)
# ---------------------------------------------------------------------------
# The Wolverine's physical mute/volume buttons are NOT in the INPUT report —
# they arrive as AUDIO_CONTROL sub 0x00 (VOLUME_CHAT) reports:
#   data[5] = mute state  (GIP_AUD_VOLUME_UNMUTED 0x04 / MIC_MUTED 0x05)
#   data[6] = volume level (absolute, 0x00..0x64 = 0..100)
#
# The controller keeps an ABSOLUTE volume (0-100): a single click bumps it up,
# hold + D-pad down lowers it. Two mirror modes:
#   MEDIA_MODE_ABSOLUTE  → sync PipeWire to the exact level via wpctl (knob == slider)
#   MEDIA_MODE_KEYS      → emit relative KEY_VOLUMEUP/DOWN + KEY_MICMUTE via uinput
MEDIA_MODE_ABSOLUTE = True

# Absolute mode targets (wpctl). The mute button mutes the mic on Xbox, so it
# maps to the default source; volume maps to the default sink.
MEDIA_SINK   = "@DEFAULT_AUDIO_SINK@"
MEDIA_SOURCE = "@DEFAULT_AUDIO_SOURCE@"

# Keys mode fallback: the controller mute button mutes the mic.
MEDIA_MUTE_KEY = ecodes.KEY_MICMUTE

_media_state = {"mute": None, "vol": None}


def _wpctl(*args: str) -> None:
    """Run wpctl as the invoking user — PipeWire lives in that user's session,
    not root's. Uses SUDO_USER / SUDO_UID set by sudo. Best-effort, non-blocking."""
    if shutil.which("wpctl") is None:
        return
    cmd = ["wpctl", *args]
    user = os.environ.get("SUDO_USER")
    uid  = os.environ.get("SUDO_UID")
    if os.geteuid() == 0 and user and uid:
        cmd = ["sudo", "-u", user, "env",
               f"XDG_RUNTIME_DIR=/run/user/{uid}", *cmd]
    try:
        subprocess.run(cmd, timeout=1.0,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _tap_key(ui: UInput, key: int) -> None:
    ui.write(ecodes.EV_KEY, key, 1)
    ui.syn()
    ui.write(ecodes.EV_KEY, key, 0)
    ui.syn()


def forward_media(ui: UInput | None, data: bytes) -> None:
    """Mirror an AUDIO_CONTROL sub 0x00 (VOLUME_CHAT) report to the system.
    First report only establishes a baseline, so connecting the controller never
    yanks the system volume — we act on changes only."""
    if len(data) < 7:
        return
    mute, vol = data[5], data[6]
    muted = (mute == 0x05)

    prev_mute = _media_state["mute"]
    if prev_mute is not None and mute != prev_mute:
        if MEDIA_MODE_ABSOLUTE:
            _wpctl("set-mute", MEDIA_SOURCE, "1" if muted else "0")
        elif ui is not None:
            _tap_key(ui, MEDIA_MUTE_KEY)
        print(f"  → media: MIC {'muted' if muted else 'unmuted'}")
    _media_state["mute"] = mute

    prev_vol = _media_state["vol"]
    if prev_vol is not None and vol != prev_vol:
        up = vol > prev_vol
        if MEDIA_MODE_ABSOLUTE:
            # Snap the sink to the controller's absolute level (0-100 → fraction).
            _wpctl("set-volume", "-l", "1.0", MEDIA_SINK, f"{vol / 100:.2f}")
        elif ui is not None:
            _tap_key(ui, ecodes.KEY_VOLUMEUP if up else ecodes.KEY_VOLUMEDOWN)
        print(f"  → media: VOL {'+' if up else '-'} ({prev_vol}→{vol} = {vol}%)")
    _media_state["vol"] = vol


# ---------------------------------------------------------------------------
# IDENTIFY response receiver
# ---------------------------------------------------------------------------

def _drain_buffer(dev, timeout_ms: int = 200) -> list[bytes]:
    """Drain all pending incoming packets from EP1, print them, return list."""
    packets = []
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            raw = bytes(dev.read(EP_GIP_IN, 64, timeout=min(100, timeout_ms)))
        except usb.core.USBTimeoutError:
            break
        except usb.core.USBError:
            break
        if not raw:
            break
        h = decode_gip_header(raw)
        tag = f"cmd=0x{h[0]:02x} opts=0x{h[1]:02x} seq={h[2]}" if h else "???"
        print(f"  [drain] ← {tag} ({len(raw)}B)")
        hexdump(raw[:min(len(raw), 32)])
        packets.append(raw)
    return packets


def _receive_identify_response(dev, our_seq: int) -> bytes | None:
    """Receive the device's IDENTIFY response, sending proper ACKs for each chunk.

    The Wolverine uses CHUNK (0x80) without CHUNK_START (0x40).
    Chunk order is determined by chunk_offset (=position in buffer).
    First chunk has chunk_offset=0; subsequent chunks have increasing offsets.
    Total length is unknown until the empty chunk arrives.
    """
    # Accumulate into a bytearray that grows as needed
    chunk_buf   = bytearray(512)
    chunk_total = 0       # 0 = unknown until empty chunk
    chunk_recvd = 0
    last_seq    = None
    deadline    = time.time() + 2.5

    while time.time() < deadline:
        ms = max(50, int((deadline - time.time()) * 1000))
        try:
            raw = bytes(dev.read(EP_GIP_IN, 64, timeout=min(ms, 300)))
        except usb.core.USBTimeoutError:
            if chunk_recvd > 0:
                break   # got some data, timeout = transfer done
            continue
        except usb.core.USBError:
            break

        if not raw:
            continue

        h = decode_gip_header(raw)
        if not h:
            continue
        rcmd, ropts, rseq, hdr_len, pkt_len, chunk_offset = h

        print(f"  ← cmd=0x{rcmd:02x} opts=0x{ropts:02x} seq={rseq} "
              f"pkt_len={pkt_len} chunk_off={chunk_offset}")
        hexdump(raw[:min(len(raw), 32)])

        if rcmd != GIP_CMD_IDENTIFY:
            continue

        last_seq   = rseq
        chunk_data = raw[hdr_len:hdr_len + pkt_len]

        if not (ropts & GIP_OPT_CHUNK):
            # Non-chunked single response
            chunk_buf   = bytearray(chunk_data)
            chunk_recvd = pkt_len
            print(f"  IDENTIFY response (non-chunked, {chunk_recvd}B)")
            break

        # Chunked packet (with or without CHUNK_START flag)
        if pkt_len == 0:
            # Empty chunk = transfer complete. chunk_offset might encode total length.
            if chunk_offset > 0:
                chunk_total = chunk_offset
            print(f"  IDENTIFY response complete ({chunk_recvd}B total, declared={chunk_total})")
            break

        # Grow buffer if needed
        end = chunk_offset + pkt_len
        if end > len(chunk_buf):
            chunk_buf.extend(bytearray(end - len(chunk_buf) + 64))
        chunk_buf[chunk_offset:end] = chunk_data
        chunk_recvd = max(chunk_recvd, end)

        if ropts & GIP_OPT_ACK:
            _send_gip_ack_for_chunk(dev, rseq, GIP_CMD_IDENTIFY,
                                    chunk_recvd, chunk_total)

    if chunk_recvd > 0:
        data = bytes(chunk_buf[:chunk_recvd])
        print(f"\n  Full IDENTIFY payload ({chunk_recvd}B):")
        hexdump(data)
        return data
    return None


# ---------------------------------------------------------------------------
# GIP init sequence
# ---------------------------------------------------------------------------

def gip_init(dev) -> list:
    """Run GIP init: IDENTIFY → AUTH → AUDIO FORMAT → POWER ON → (VOLUME).
    Order follows xone's headset bring-up: format is negotiated first, then
    GIP_PWR_ON wakes the audio subsystem. HW volume is skipped for jack headsets.
    Returns seq_ref list for use by monitor threads.
    """
    seq_ref = [1]

    print("\n" + "=" * 60)
    print("STEP 1 — IDENTIFY")
    print("=" * 60)
    # Drain any spontaneous packets (ANNOUNCE etc.) already queued by device
    print("  [pre-drain] clearing buffer...")
    _drain_buffer(dev, timeout_ms=300)

    gip_send(dev, pkt_identify(seq_ref[0]), "IDENTIFY")
    seq_ref[0] += 1
    # Receive and ACK the device's IDENTIFY response (may be chunked).
    # Without proper chunk ACKs the device stays stuck and won't respond to AUTH.
    _receive_identify_response(dev, seq_ref[0])

    print("\n" + "=" * 60)
    print("STEP 2 — GIP AUTH HANDSHAKE")
    print("=" * 60)
    auth_ok = gip_auth_handshake(dev, seq_ref)
    if not auth_ok:
        print("  WARNING: auth failed or skipped — audio may not work")

    time.sleep(0.15)

    print("\n" + "=" * 60)
    print("STEP 3 — AUDIO FORMAT (48kHz stereo)")
    print("=" * 60)
    gip_send(dev, pkt_audio_format(seq_ref[0]), "AUDIO_FORMAT")
    seq_ref[0] += 1
    resp = gip_recv(dev, "AUDIO_FORMAT response")
    if resp:
        if resp[0] == GIP_CMD_AUDIO_CONTROL:
            sub = resp[4] if len(resp) > 4 else 0xFF
            print(f"  ✓ Audio control response, subcommand: 0x{sub:02x}")
        elif resp[0] == GIP_CMD_ACKNOWLEDGE:
            print("  ✓ ACK")
    time.sleep(0.05)

    print("\n" + "=" * 60)
    print("STEP 4 — POWER ON (bring audio subsystem online)")
    print("=" * 60)
    # xone sends GIP_PWR_ON right after the format is negotiated. This is the
    # step that was missing entirely — the ADC/DAC stays idle without it.
    gip_send(dev, pkt_power(seq_ref[0], GIP_PWR_ON), "POWER_ON")
    seq_ref[0] += 1
    gip_recv(dev, "POWER_ON response")
    time.sleep(0.05)

    if SEND_HW_VOLUME:
        print("\n" + "=" * 60)
        print("STEP 5 — VOLUME (unmute, 100%)")
        print("=" * 60)
        gip_send(dev, pkt_audio_volume(seq_ref[0]), "VOLUME")
        seq_ref[0] += 1
        gip_recv(dev, "VOLUME response")
        time.sleep(0.05)

        print("\n" + "=" * 60)
        print("STEP 6 — VOLUME_CHAT")
        print("=" * 60)
        gip_send(dev, pkt_audio_volume_chat(seq_ref[0]), "VOLUME_CHAT")
        seq_ref[0] += 1
        gip_recv(dev, "VOLUME_CHAT response")
        time.sleep(0.05)
    else:
        print("\n  [skip] HW VOLUME (sub 0x03) — jack headset path, "
              "xone skips it (see SEND_HW_VOLUME)")

    return seq_ref


# ---------------------------------------------------------------------------
# Monitor threads
# ---------------------------------------------------------------------------

def monitor_gip(dev, ui: UInput | None, ui_media: UInput | None,
                stop: threading.Event, seq_ref: list) -> None:
    print("[gip] Monitoring EP1 IN...")
    while not stop.is_set():
        try:
            data = bytes(dev.read(EP_GIP_IN, 64, timeout=100))
            if not data:
                continue
            cmd  = data[0]
            opts = data[1] if len(data) > 1 else 0
            seq  = data[2] if len(data) > 2 else 0

            if cmd == GIP_CMD_INPUT:
                if ui is not None:
                    parse_and_forward_gamepad(ui, data)
            elif cmd == GIP_CMD_STATUS:
                s = seq_ref[0]; seq_ref[0] += 1
                try:
                    status_val = data[4] if len(data) > 4 else 0x80
                    dev.write(EP_GIP_OUT, pkt_status(s, status_val), timeout=TIMEOUT_MS)
                except usb.core.USBError:
                    pass
            elif cmd == GIP_CMD_AUDIO_CONTROL:
                sub = data[4] if len(data) > 4 else 0xFF
                ts  = time.strftime("%H:%M:%S")
                print(f"\n[gip {ts}] AUDIO_CONTROL subcommand=0x{sub:02x}:")
                hexdump(data)
                if sub == 0x00 and len(data) >= 9:
                    state_b = data[5]
                    # Media buttons: mute state + volume level ride on this report
                    forward_media(ui_media, data)
                    s = seq_ref[0]; seq_ref[0] += 1
                    try:
                        dev.write(EP_GIP_OUT,
                                  pkt_audio_volume_chat(s, state_b, 0x64, 0x64, 0x64),
                                  timeout=TIMEOUT_MS)
                        print(f"  → sent VOLUME_CHAT max volumes")
                    except usb.core.USBError as e:
                        print(f"  → VOLUME_CHAT send failed: {e}")
                if opts & GIP_OPT_ACK:
                    try:
                        dev.write(EP_GIP_OUT, pkt_ack(cmd, opts, seq), timeout=TIMEOUT_MS)
                    except usb.core.USBError:
                        pass
            elif cmd == GIP_CMD_AUTHENTICATE:
                ts = time.strftime("%H:%M:%S")
                print(f"\n[gip {ts}] AUTHENTICATE ({len(data)}B) — unexpected post-auth:")
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


# ---------------------------------------------------------------------------
# PipeWire bridge (native, via tools/wolverine_pw.so)
# ---------------------------------------------------------------------------

# Output (headphones): 48 kHz stereo, 192 PCM bytes per EP3 OUT packet (48 frames).
OUT_RATE       = 48000
OUT_CHANNELS   = 2
EP3_OUT_PCM    = 192

# Input (mic): the controller streams ~48000 bytes/s, i.e. 24 kHz mono (not the
# 48 kHz stereo of the output). PipeWire resamples to whatever consumers want.
# Confirm with the "[audio-in] … bytes/s" diagnostic while speaking.
IN_RATE        = 24000
IN_CHANNELS    = 1
IN_STRIDE      = IN_CHANNELS * 2


def load_pipewire_bridge():
    """Load the compiled native bridge. Returns the ctypes lib, or None if the
    .so is missing (run `make -C tools`)."""
    # The bridge connects to PipeWire from this (root, under sudo) process.
    # Point libpipewire at the invoking user's session so the virtual nodes land
    # in the user's audio graph — not root's (which usually has none). Root can
    # open the user's socket, so this just works.
    uid = os.environ.get("SUDO_UID")
    if os.geteuid() == 0 and uid:
        os.environ["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"

    here = os.path.dirname(os.path.abspath(__file__))
    so   = os.path.join(here, "wolverine_pw.so")
    if not os.path.exists(so):
        print(f"[pw] {so} not found — build it with `make -C tools`. "
              "Audio devices disabled (raw passthrough only).")
        return None
    lib = ctypes.CDLL(so)
    lib.wpw_start.argtypes          = [ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int]
    lib.wpw_start.restype           = ctypes.c_int
    lib.wpw_stop.argtypes           = []
    lib.wpw_stop.restype            = None
    lib.wpw_playback_avail.argtypes = []
    lib.wpw_playback_avail.restype  = ctypes.c_int
    lib.wpw_read_playback.argtypes  = [ctypes.c_void_p, ctypes.c_int]
    lib.wpw_read_playback.restype   = ctypes.c_int
    lib.wpw_write_capture.argtypes  = [ctypes.c_char_p, ctypes.c_int]
    lib.wpw_write_capture.restype   = ctypes.c_int
    return lib


# Buffer this much before we start draining, so jitter/burstiness never empties
# the ring (~20 ms cushion at 192 B/ms). Larger = smoother but more latency.
PLAY_PRIME_BYTES = EP3_OUT_PCM * 20


def stream_audio_out(dev, stop: threading.Event, pw) -> None:
    """Drain the PipeWire sink ring to EP3 OUT. The device plays raw 192-byte S16
    stereo PCM (48 frames); the earlier <u16 len> prefix was actually played as a
    sample, causing a left-channel buzz and a half-frame misalignment (crackle) —
    so we send raw PCM by default (WOLV_OUT_HEADER=1 restores the old prefix).

    Once primed, a transient dip sends a single silence packet (1 ms) but keeps
    the stream primed, so we never punch an 8 ms re-prime gap (robotic artifact)."""
    prefix  = struct.pack("<H", EP3_OUT_PCM) if os.environ.get("WOLV_OUT_HEADER") else b""
    silence = prefix + bytes(EP3_OUT_PCM)
    rbuf    = ctypes.create_string_buffer(EP3_OUT_PCM)
    errors  = 0
    primed  = False
    pending = None
    is_sil  = True
    # diagnostics
    sent = under = 0
    avail_min = avail_max = 0
    last_dbg = time.time()
    print(f"[audio-out] EP3 OUT header={'u16 len' if prefix else 'NONE (raw PCM)'} "
          f"← PipeWire sink 'Wolverine Headphones'"
          if pw else "[audio-out] EP3 OUT (silence — no PipeWire bridge)")

    while not stop.is_set():
        if pending is None:
            if pw is None:
                pending = silence
                is_sil  = True
            else:
                avail = pw.wpw_playback_avail()
                avail_min = min(avail_min, avail)
                avail_max = max(avail_max, avail)
                if not primed:
                    primed  = avail >= PLAY_PRIME_BYTES
                    pending = silence
                    is_sil  = True
                elif avail >= EP3_OUT_PCM:
                    pw.wpw_read_playback(rbuf, EP3_OUT_PCM)
                    pending = prefix + rbuf.raw[:EP3_OUT_PCM]
                    is_sil  = False
                else:
                    pending = silence          # transient underrun: 1 ms gap only
                    is_sil  = True
        try:
            dev.write(EP_AUDIO_OUT, pending, timeout=5)
            sent += 1
            if is_sil and primed:
                under += 1
            pending = None
            errors  = 0
        except usb.core.USBTimeoutError:
            pass                                # keep pending, retry same packet
        except usb.core.USBError as e:
            errors += 1
            pending = None
            if errors == 1:
                print(f"[audio-out] write error: {e}")
            if errors > 100:
                print("[audio-out] too many errors, stopping")
                break

        now = time.time()
        if now - last_dbg >= 5.0:
            dt = now - last_dbg
            print(f"[audio-out] {sent/dt:.0f} pkt/s, {under} underruns/5s, "
                  f"ring {avail_min}-{avail_max}B")
            sent = under = 0
            avail_min = avail_max = pw.wpw_playback_avail() if pw else 0
            last_dbg = now


def monitor_audio(dev, stop: threading.Event, pw) -> None:
    """Read EP3 IN (GIP AUDIO_SAMPLES), extract PCM and feed the PipeWire source.
    Packet layout: GIP header, 2-byte sub-header, then S16LE PCM."""
    print("[audio-in]  EP3 IN → PipeWire source 'Wolverine Microphone'"
          if pw else "[audio-in]  Monitoring EP3 IN (no PipeWire bridge)")
    reads    = 0
    pcm_bytes = 0
    last_sz  = 0
    last_dbg = time.time()
    while not stop.is_set():
        try:
            data = bytes(dev.read(EP_AUDIO_IN, 228, timeout=100))
            reads += 1
        except usb.core.USBTimeoutError:
            continue
        except usb.core.USBError as e:
            if not stop.is_set():
                print(f"[audio-in] error: {e}")
            time.sleep(0.1)
            continue

        if len(data) >= 4 and data[0] == GIP_CMD_AUDIO_SAMPLES:
            hdr = decode_gip_header(data)
            if hdr:
                _, _, _, hdr_len, pkt_len, _ = hdr
                payload = data[hdr_len:hdr_len + pkt_len]
                pcm = payload[2:]                 # skip 2-byte sub-header
                if pcm:
                    last_sz = len(pcm)
                    pcm_bytes += len(pcm)
                    if pw is not None:
                        pw.wpw_write_capture(pcm, len(pcm))

        now = time.time()
        if now - last_dbg >= 5.0:
            rate = pcm_bytes / (now - last_dbg)
            # bytes/s ÷ 2 (S16) = samples/s; ÷ channels = frame rate
            print(f"[audio-in] {reads} reads/5s — {rate:.0f} PCM bytes/s "
                  f"(~{rate/2:.0f} S16 samples/s), last pkt {last_sz}B")
            reads = pcm_bytes = 0
            last_dbg = now


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if os.geteuid() != 0:
        print("ERROR: run as root (sudo)")
        sys.exit(1)

    print("=== Razer Wolverine Ultimate — GIP Driver with Auth ===\n")

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

    print("Creating virtual media-key keyboard (uinput)...")
    try:
        uinput_media = create_uinput_media()
        print(f"  ✓ Virtual media keyboard active (fd={uinput_media.fd})")
    except Exception as e:
        print(f"  WARNING: media uinput failed ({e}) — media buttons disabled")
        uinput_media = None

    # GIP init (IDENTIFY + AUTH + AUDIO FORMAT)
    # Auth and audio format negotiation happen with alt=0 (isochronous endpoints idle).
    seq_ref = gip_init(dev)

    # Activate alt=1 — isochronous endpoints come alive after format negotiation
    print("\nActivating isochronous endpoints (alt setting 1)...")
    for iface in [1, 2]:
        try:
            dev.set_interface_altsetting(interface=iface, alternate_setting=1)
            print(f"  Interface {iface} alt=1 active")
        except usb.core.USBError as e:
            print(f"  Interface {iface} alt setting: {e}")

    time.sleep(0.1)

    stop = threading.Event()

    print("\nStarting PipeWire bridge...")
    pw = load_pipewire_bridge()
    if pw is not None:
        rc = pw.wpw_start(OUT_RATE, OUT_CHANNELS, IN_RATE, IN_CHANNELS)
        if rc == 0:
            print(f"  ✓ PipeWire devices: 'Wolverine Headphones' "
                  f"({OUT_RATE//1000}kHz/{OUT_CHANNELS}ch sink) + "
                  f"'Wolverine Microphone' ({IN_RATE//1000}kHz/{IN_CHANNELS}ch source)")
        else:
            print(f"  WARNING: wpw_start failed (rc={rc}) — audio devices disabled")
            pw = None

    print("Starting EP3 audio streams...")
    t_audio_out = threading.Thread(target=stream_audio_out, args=(dev, stop, pw), daemon=True)
    t_audio_in  = threading.Thread(target=monitor_audio,   args=(dev, stop, pw), daemon=True)
    t_audio_out.start()
    t_audio_in.start()

    print("\n" + "=" * 60)
    print("MONITORING — press buttons, media keys, plug headset")
    print("Ctrl+C to stop")
    print("=" * 60 + "\n")

    threads = [
        threading.Thread(target=monitor_gip,  args=(dev, uinput_fd, uinput_media, stop, seq_ref), daemon=True),
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

    for t in [*threads, t_audio_out, t_audio_in]:
        t.join(timeout=1)

    if pw is not None:
        pw.wpw_stop()

    for iface in [0, 1, 2]:
        try:
            usb.util.release_interface(dev, iface)
        except Exception:
            pass

    if uinput_fd:
        uinput_fd.close()
    if uinput_media:
        uinput_media.close()

    print("Done")


if __name__ == "__main__":
    main()
