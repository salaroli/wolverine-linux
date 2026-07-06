//! Xbox GIP (Game Interface Protocol) wire format.
//!
//! Proprietary Microsoft protocol for Xbox One peripherals, published as an
//! open standard (MS-GIPUSB) in September 2024. This module owns the packet
//! framing and the protocol constants discovered during reverse engineering.
//! See ../../CONTEXT.md ("Protocolo: Xbox GIP") for the full write-up.

/// USB identity of the Razer Wolverine Ultimate.
pub const VID: u16 = 0x1532;
pub const PID: u16 = 0x0a14;

/// Our GIP client id (matches the device). Combined with the option flags.
pub const CLIENT_ID: u8 = 0x01;

/// Common options byte for host-originated internal commands: CLIENT_ID | INTERNAL.
pub const OPTS_INTERNAL: u8 = CLIENT_ID | opt::INTERNAL; // 0x21

/// GIP command IDs relevant to this device. Status column refers to the
/// Wolverine specifically (see CONTEXT.md table).
pub mod cmd {
    pub const ACKNOWLEDGE: u8 = 0x01;
    pub const ANNOUNCE: u8 = 0x02;
    pub const STATUS: u8 = 0x03;
    pub const IDENTIFY: u8 = 0x04;
    /// KEY TO AUDIO: with GIP_PWR_ON (0x00) wakes the audio subsystem.
    pub const POWER: u8 = 0x05;
    pub const AUTHENTICATE: u8 = 0x06;
    pub const AUDIO_CONTROL: u8 = 0x08;
    pub const INPUT: u8 = 0x20;
    /// Isochronous audio samples (both OUT and IN are framed with this).
    pub const AUDIO_SAMPLES: u8 = 0x60;
}

/// Options byte (byte 1) bit layout.
pub mod opt {
    pub const CLIENT_ID_MASK: u8 = 0x0f; // bits 0-3
    pub const ACK: u8 = 1 << 4;
    pub const INTERNAL: u8 = 1 << 5;
    pub const CHUNK_START: u8 = 1 << 6;
    pub const CHUNK: u8 = 1 << 7;
}

/// POWER subcommand: wake the audio subsystem. Payload is a single byte.
pub const GIP_PWR_ON: u8 = 0x00;

/// AUDIO_CONTROL subcommand seen carrying the media-button state (VOLUME_CHAT).
/// Arrives on EP1 alongside the gamepad: data[5] = mic mute, data[6] = volume 0..100.
pub const AUDIO_CTRL_VOLUME_CHAT: u8 = 0x00;
/// AUDIO_CONTROL subcommand that negotiates the stream format.
pub const AUDIO_CTRL_FORMAT: u8 = 0x02;

/// The audio-format payload negotiated on bring-up: 48kHz stereo.
/// `[format=0x02, in=0x10, out=0x10]` — device echoes it back.
pub const AUDIO_FORMAT_48K_STEREO: [u8; 3] = [0x02, 0x10, 0x10];

// --- LEB128 varint (payload length, and chunk_offset when CHUNK is set) ---

/// Append `value` as an unsigned LEB128 varint to `out`.
pub fn write_varint(out: &mut Vec<u8>, mut value: u32) {
    loop {
        let mut byte = (value & 0x7f) as u8;
        value >>= 7;
        if value != 0 {
            byte |= 0x80;
        }
        out.push(byte);
        if value == 0 {
            break;
        }
    }
}

/// Read an unsigned LEB128 varint from `buf` starting at `pos`.
/// Returns `(value, bytes_consumed)`.
pub fn read_varint(buf: &[u8], pos: usize) -> Option<(u32, usize)> {
    let mut value: u32 = 0;
    let mut shift = 0;
    let mut i = pos;
    while i < buf.len() {
        let byte = buf[i];
        value |= ((byte & 0x7f) as u32) << shift;
        i += 1;
        if byte & 0x80 == 0 {
            return Some((value, i - pos));
        }
        shift += 7;
    }
    None
}

/// Build a GIP header: `cmd, options, seq, <len varint>`.
///
/// The header must be an EVEN number of bytes. The padding is subtle and
/// getting it wrong is the framing bug that produced robotic voice (CONTEXT.md):
/// we do NOT append a raw 0x00 after the varint. Instead we extend the varint
/// itself — set the continuation bit on its last byte and append a 0x00
/// terminator — so e.g. len=192 becomes `c0 81 00` (which decodes back to 192),
/// NOT `c0 01 00` (which decodes as 192 and then leaks the 0x00 into the payload).
///
/// This is the framing used for both directions of EP3 audio:
///   OUT (headphones): 0x60 0x21 <seq> <len=192> + 192B PCM
///   IN  (mic):        0x60 0x21 <seq> <len>     | <le16 length_out> | PCM
pub fn build_header(cmd: u8, options: u8, seq: u8, payload_len: u32) -> Vec<u8> {
    let mut len_varint = Vec::new();
    write_varint(&mut len_varint, payload_len);
    if (3 + len_varint.len()) % 2 != 0 {
        let last = len_varint.len() - 1;
        len_varint[last] |= 0x80; // continuation
        len_varint.push(0x00); // terminator, keeps the decoded value unchanged
    }
    let mut hdr = Vec::with_capacity(3 + len_varint.len());
    hdr.push(cmd);
    hdr.push(options);
    hdr.push(seq);
    hdr.extend_from_slice(&len_varint);
    hdr
}

/// Build a complete (non-chunked) GIP packet: header + payload.
pub fn build_packet(cmd: u8, options: u8, seq: u8, payload: &[u8]) -> Vec<u8> {
    let mut pkt = build_header(cmd, options, seq, payload.len() as u32);
    pkt.extend_from_slice(payload);
    pkt
}

/// Parsed view of a received GIP packet. Mirrors Python's `decode_gip_header`
/// plus a slice over the payload bytes actually present in this USB frame.
#[derive(Debug, Clone)]
pub struct Packet<'a> {
    pub cmd: u8,
    pub options: u8,
    pub seq: u8,
    /// Length of the header (offset where the payload starts).
    pub hdr_len: usize,
    /// Declared payload length from the varint.
    pub pkt_len: usize,
    /// Chunk offset — meaningful only when `opt::CHUNK` is set in `options`.
    pub chunk_offset: usize,
    /// Payload bytes present in *this* frame (may be shorter than `pkt_len` for
    /// chunked transfers; the caller reassembles by `chunk_offset`).
    pub payload: &'a [u8],
}

impl Packet<'_> {
    pub fn is_chunked(&self) -> bool {
        self.options & opt::CHUNK != 0
    }
    pub fn wants_ack(&self) -> bool {
        self.options & opt::ACK != 0
    }
}

/// Decode a GIP header. Matches Python `decode_gip_header`: read the length
/// varint (no even-length adjustment on receive — the even padding is only a
/// send-side concern), then the chunk_offset varint iff the CHUNK flag is set.
pub fn decode(buf: &[u8]) -> Option<Packet<'_>> {
    if buf.len() < 4 {
        return None;
    }
    let cmd = buf[0];
    let options = buf[1];
    let seq = buf[2];
    let (pkt_len, consumed) = read_varint(buf, 3)?;
    let mut pos = 3 + consumed;

    let mut chunk_offset = 0usize;
    if options & opt::CHUNK != 0 {
        let (off, c) = read_varint(buf, pos)?;
        chunk_offset = off as usize;
        pos += c;
    }

    let end = (pos + pkt_len as usize).min(buf.len());
    Some(Packet {
        cmd,
        options,
        seq,
        hdr_len: pos,
        pkt_len: pkt_len as usize,
        chunk_offset,
        payload: buf.get(pos..end)?,
    })
}

/// Sequence numbers increment 1..=255 and never take the value 0.
#[derive(Default)]
pub struct SeqCounter(u8);

impl SeqCounter {
    pub fn next(&mut self) -> u8 {
        self.0 = self.0.wrapping_add(1);
        if self.0 == 0 {
            self.0 = 1;
        }
        self.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn varint_roundtrip() {
        for v in [0u32, 1, 127, 128, 192, 255, 16384, 1_000_000] {
            let mut out = Vec::new();
            write_varint(&mut out, v);
            let (got, n) = read_varint(&out, 0).unwrap();
            assert_eq!(got, v);
            assert_eq!(n, out.len());
        }
    }

    #[test]
    fn seq_never_zero() {
        let mut s = SeqCounter(254);
        assert_eq!(s.next(), 255);
        assert_eq!(s.next(), 1); // wraps past 0
    }

    #[test]
    fn audio_out_header_framing() {
        // The exact bytes that fixed the robotic voice: 60 21 <seq> c0 81 00.
        let hdr = build_header(cmd::AUDIO_SAMPLES, OPTS_INTERNAL, 7, 192);
        assert_eq!(hdr, vec![0x60, 0x21, 0x07, 0xc0, 0x81, 0x00]);
        // ...and it must decode back to a 192-byte payload length with an
        // even (6-byte) header, so no 0x00 leaks into the PCM.
        let mut frame = hdr.clone();
        frame.extend_from_slice(&[0xAB; 192]);
        let pkt = decode(&frame).unwrap();
        assert_eq!(pkt.cmd, cmd::AUDIO_SAMPLES);
        assert_eq!(pkt.pkt_len, 192);
        assert_eq!(pkt.hdr_len, 6);
        assert_eq!(pkt.payload[0], 0xAB);
    }

    #[test]
    fn header_even_length() {
        // Headers are always even-length regardless of payload size.
        for len in [0u32, 1, 63, 64, 127, 128, 192, 255, 4096] {
            let hdr = build_header(cmd::AUDIO_SAMPLES, OPTS_INTERNAL, 1, len);
            assert_eq!(hdr.len() % 2, 0, "len={len} header not even");
            let mut frame = hdr.clone();
            frame.extend(std::iter::repeat(0u8).take(len as usize));
            let pkt = decode(&frame).unwrap();
            assert_eq!(pkt.pkt_len, len as usize, "roundtrip len={len}");
        }
    }
}
