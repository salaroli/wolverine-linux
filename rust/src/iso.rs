//! Asynchronous isochronous EP3 audio engine.
//!
//! Replaces tools/iso_audio.py. Keeps N transfers in flight in each direction,
//! resubmitting them from completion callbacks, bridging the PipeWire RT thread
//! (audio.rs) via lock-free rings.
//!
//! Framing (both directions are GIP AUDIO_SAMPLES, nearly symmetric — see
//! CONTEXT.md "Aprendizado crítico: framing do EP3"):
//!
//!   OUT (headphones): [0x60][0x21][seq][len=192] + 192B PCM
//!                     = 48 frames S16LE @ 48kHz stereo, ~1000 pkt/s, 198B/pkt.
//!                     NOT raw PCM — raw PCM = robotic voice (firmware desyncs).
//!
//!   IN  (mic):        [0x60][0x21][seq][len] | [le16 length_out] | PCM
//!                     PCM is 24kHz mono. The 2-byte length_out sub-header is
//!                     the __le16 from struct gip_pkt_audio_samples (xone) and
//!                     must be skipped before pushing samples to the source.

use anyhow::Result;

use crate::gip;

/// One GIP OUT audio frame = 6B header + 192B PCM.
pub const OUT_PAYLOAD_BYTES: usize = 192;

/// options byte for audio frames: client_id | INTERNAL = 0x21.
pub const AUDIO_OPTIONS: u8 = 0x01 | gip::opt::INTERNAL;

/// Number of transfers kept in flight (OUT heavier than IN, per the Python engine).
pub const OUT_TRANSFERS: usize = 6;
pub const IN_TRANSFERS: usize = 4;
pub const PACKETS_PER_TRANSFER: usize = 8;

/// The iso engine. Owns interface 1 (alt 1) exclusively.
pub struct IsoAudio {
    seq: gip::SeqCounter,
    // TODO: nusb interface-1 handle, in-flight transfer queues, ring endpoints.
}

impl IsoAudio {
    /// Claim interface 1, set alt=1, and start the OUT/IN transfer pumps.
    ///
    /// `playback`/`capture` are the ring endpoints shared with audio.rs:
    ///   - OUT pump: drains PCM from `playback`, GIP-frames it, submits to EP3 OUT.
    ///   - IN pump:  parses GIP from EP3 IN, skips the le16, writes to `capture`.
    ///
    /// TODO: prime ~40ms of the OUT ring before draining real audio so PipeWire
    /// bursts don't underrun mid-stream (as the Python engine does).
    pub fn start(/* playback: rtrb::Consumer<u8>, capture: rtrb::Producer<u8> */) -> Result<Self> {
        anyhow::bail!("iso::IsoAudio::start not implemented yet")
    }

    /// Build one OUT frame: GIP header (60 21 <seq> <len=192>) + `pcm` (192B).
    pub fn frame_out(&mut self, pcm: &[u8; OUT_PAYLOAD_BYTES]) -> Vec<u8> {
        let seq = self.seq.next();
        let mut pkt = gip::build_header(
            gip::cmd::AUDIO_SAMPLES,
            AUDIO_OPTIONS,
            seq,
            OUT_PAYLOAD_BYTES as u32,
        );
        pkt.extend_from_slice(pcm);
        pkt
    }

    pub fn stop(&mut self) -> Result<()> {
        Ok(())
    }
}

/// Parse a received EP3 IN packet: decode the GIP header, then skip the 2-byte
/// `length_out` (le16) sub-header. Returns the raw PCM slice (24kHz mono).
pub fn parse_in<'a>(buf: &'a [u8]) -> Option<&'a [u8]> {
    let pkt = gip::decode(buf)?;
    if pkt.cmd != gip::cmd::AUDIO_SAMPLES {
        return None;
    }
    // Skip the le16 length_out that precedes the PCM on the IN direction.
    pkt.payload.get(2..)
}
