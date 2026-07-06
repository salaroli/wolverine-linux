//! Xbox GIP (Game Interface Protocol) wire format.
//!
//! Proprietary Microsoft protocol for Xbox One peripherals, published as an
//! open standard (MS-GIPUSB) in September 2024. This module owns the packet
//! framing and the protocol constants discovered during reverse engineering.
//! See ../../CONTEXT.md ("Protocolo: Xbox GIP") for the full write-up.

/// USB identity of the Razer Wolverine Ultimate.
pub const VID: u16 = 0x1532;
pub const PID: u16 = 0x0a14;

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
/// The header must be an even number of bytes (pad the last length byte if
/// needed). This is the framing used for both directions of EP3 audio:
///   OUT (headphones): 0x60 0x21 <seq> <len=192> + 192B PCM
///   IN  (mic):        0x60 0x21 <seq> <len>     | <le16 length_out> | PCM
pub fn build_header(cmd: u8, options: u8, seq: u8, payload_len: u32) -> Vec<u8> {
    let mut hdr = Vec::with_capacity(6);
    hdr.push(cmd);
    hdr.push(options);
    hdr.push(seq);
    write_varint(&mut hdr, payload_len);
    if hdr.len() % 2 != 0 {
        hdr.push(0x00); // keep header even-length
    }
    hdr
}

/// Parsed view of a received GIP packet.
#[derive(Debug, Clone)]
pub struct Packet<'a> {
    pub cmd: u8,
    pub options: u8,
    pub seq: u8,
    pub payload: &'a [u8],
}

/// Decode a GIP header and return the packet with a payload slice.
///
/// TODO: handle chunking — the Wolverine sends chunks WITHOUT the CHUNK_START
/// flag, using chunk_offset=0 as the initial position (see CONTEXT.md). This
/// stub does not yet reassemble multi-chunk payloads.
pub fn decode(buf: &[u8]) -> Option<Packet<'_>> {
    if buf.len() < 3 {
        return None;
    }
    let cmd = buf[0];
    let options = buf[1];
    let seq = buf[2];
    let (len, consumed) = read_varint(buf, 3)?;
    let mut payload_start = 3 + consumed;
    // header padded to even length
    if payload_start % 2 != 0 {
        payload_start += 1;
    }
    let end = (payload_start + len as usize).min(buf.len());
    Some(Packet {
        cmd,
        options,
        seq,
        payload: buf.get(payload_start..end)?,
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
}
