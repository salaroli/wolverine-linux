//! uinput layer: re-expose the gamepad and handle the media buttons.
//!
//! Two virtual devices (mirrors gip_init.py):
//!   - gamepad  : ABS + BTN, fed from GIP INPUT (0x20) reports.
//!   - keyboard : KEY_* only. MUST be separate — libinput classifies the
//!                gamepad (ABS+BTN) as a joystick and would swallow KEY_* events
//!                emitted from it. A pure-keyboard device is delivered as a real
//!                keyboard to the compositor.
//!
//! Media buttons arrive as AUDIO_CONTROL sub 0x00 (VOLUME_CHAT) on EP1:
//!   data[5] = mic mute state (0x04 unmuted / 0x05 muted)
//!   data[6] = absolute volume (0x00..0x64 = 0..100)
//! The firmware tracks state and reports only the resulting absolute volume, so
//! no "hold" logic is needed — just track the direction of data[6] changes.

use anyhow::Result;

/// How media buttons are surfaced.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MediaMode {
    /// Default: mirror to PipeWire (sink volume + source mute) 1:1. In Rust we
    /// set volume/mute directly on the graph via the PipeWire client (audio.rs)
    /// instead of shelling out to `wpctl` as the Python version does.
    Absolute,
    /// Emit KEY_VOLUMEUP/DOWN + KEY_MICMUTE from the keyboard device.
    Keys,
}

pub struct Uinput {
    // TODO: input-linux UInputHandle for gamepad and keyboard.
    _private: (),
}

impl Uinput {
    /// Create both virtual devices.
    pub fn create() -> Result<Self> {
        anyhow::bail!("input::Uinput::create not implemented yet")
    }

    /// Translate a GIP INPUT (0x20) report into gamepad ABS/BTN events.
    /// TODO: map the 14-byte payload (buttons, sticks, triggers, dpad, guide).
    pub fn forward_gamepad(&mut self, _payload: &[u8]) -> Result<()> {
        Ok(())
    }

    /// Handle an AUDIO_CONTROL sub-0x00 report (media buttons).
    ///
    /// Acts only on *changes*; the first report is a baseline so that merely
    /// connecting doesn't yank the system volume. `mode` decides between
    /// mirroring to PipeWire (Absolute) or emitting media keys (Keys).
    pub fn forward_media(&mut self, _data: &[u8], _mode: MediaMode) -> Result<()> {
        // data[5] = mic mute, data[6] = absolute volume (see module docs).
        Ok(())
    }
}
