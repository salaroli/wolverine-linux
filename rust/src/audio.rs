//! Native PipeWire client — sink + source.
//!
//! Replaces the C shim (tools/wolverine_pw.c). With `pipewire-rs`/`libspa` we
//! build SPA audio formats natively in Rust — the exact thing that forced the C
//! shim, because Python's ctypes could not reach the `static inline`
//! `spa_format_audio_raw_build`.
//!
//! Two graph nodes:
//!   - "Wolverine Headphones" (Audio/Sink)   — 48kHz stereo S16LE
//!   - "Wolverine Microphone" (Audio/Source) — 24kHz mono   S16LE
//!
//! Two lock-free SPSC rings bridge the PipeWire RT thread and the USB iso
//! engine (iso.rs), replacing the C mutex + drop-on-overflow rings:
//!   - playback ring: sink `process` (producer) -> iso OUT pump (consumer)
//!   - capture  ring: iso IN pump (producer)    -> source `process` (consumer)

use anyhow::Result;

/// Sink (headphones) format.
pub const OUT_RATE: u32 = 48_000;
pub const OUT_CHANNELS: u32 = 2;

/// Source (mic) format — independent from the sink.
pub const IN_RATE: u32 = 24_000;
pub const IN_CHANNELS: u32 = 1;

/// Ring endpoints handed to the iso engine.
pub struct Bridge {
    // TODO: rtrb::Consumer<u8> for playback, rtrb::Producer<u8> for capture,
    // plus the pw_thread_loop / stream handles to keep them alive.
    _private: (),
}

impl Bridge {
    /// Create the two nodes and start the PipeWire thread loop.
    ///
    /// IMPORTANT: connect to the *invoking user's* PipeWire session, not root's.
    /// The daemon runs under sudo, so point PIPEWIRE_RUNTIME_DIR /
    /// XDG_RUNTIME_DIR at the SUDO_UID before connecting, or the nodes land in
    /// root's session and never show up in the user's audio settings.
    pub fn start() -> Result<Self> {
        anyhow::bail!("audio::Bridge::start not implemented yet")
    }

    pub fn stop(self) -> Result<()> {
        Ok(())
    }
}
