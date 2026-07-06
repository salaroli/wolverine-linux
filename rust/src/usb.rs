//! USB transport: open the device, detach xpad, claim interfaces, and run the
//! GIP control handshake (IDENTIFY / AUDIO_FORMAT / POWER ON) on EP1.
//!
//! USB interface map (from lsusb -v, see CONTEXT.md):
//!   Interface 0 alt 0 — EP1 OUT/IN (Interrupt, 64B)   -> Gamepad GIP (xpad)
//!   Interface 1 alt 1 — EP3 OUT/IN (Isochronous, 228B) -> Audio (see iso.rs)
//!   Interface 2 alt 1 — EP2 OUT/IN (Bulk, 64B)          -> Control / events
//!
//! Ownership split (mirrors the Python hybrid): the control/handshake handle
//! claims interfaces 0 and 2; the isochronous engine (iso.rs) claims interface
//! 1 separately. The GIP negotiation on EP1 MUST happen BEFORE the iso engine
//! grabs interface 1 — ordering matters.

use anyhow::Result;

use crate::gip;

/// Endpoint addresses (to be confirmed against the descriptors at runtime).
pub const EP1_IN: u8 = 0x81;
pub const EP1_OUT: u8 = 0x01;
pub const EP2_IN: u8 = 0x82;
pub const EP2_OUT: u8 = 0x02;
pub const EP3_IN: u8 = 0x83;
pub const EP3_OUT: u8 = 0x03;

/// Handle to the opened Wolverine, owning the control interfaces (0 and 2).
pub struct Device {
    // TODO: hold the nusb::Device / Interface handles here.
    _private: (),
}

impl Device {
    /// Find and open the Wolverine (VID/PID), detach the kernel `xpad` driver
    /// from every interface, and claim interfaces 0 and 2.
    ///
    /// TODO: nusb — enumerate, match gip::VID/gip::PID, `detach_kernel_driver`,
    /// `claim_interface(0)` and `claim_interface(2)`, `set_alt_setting(2, 1)`.
    pub fn open() -> Result<Self> {
        anyhow::bail!("usb::Device::open not implemented yet")
    }

    /// Run the audio bring-up handshake on EP1. This is the sequence that was
    /// reverse-engineered against the xone driver (CONTEXT.md "Sequência de
    /// bring-up de áudio que funciona"):
    ///
    ///   1. IDENTIFY (+ ACK the chunks the device replies with, even without
    ///      the CHUNK_START flag).
    ///   2. AUDIO_FORMAT — cmd 0x08 sub 0x02, payload gip::AUDIO_FORMAT_48K_STEREO.
    ///      Device echoes it back.
    ///   3. POWER ON — cmd 0x05, payload [gip::GIP_PWR_ON]. THE missing step.
    ///      Device answers with AUDIO_CONTROL sub 0x00 (volume/mute report).
    ///   4. (caller) activate alt=1 on interfaces 1 and 2 so the iso endpoints
    ///      open and actually carry audio.
    ///
    /// GIP auth (cmd 0x06) is intentionally skipped: the device never answers
    /// and the jack path does not require it.
    pub fn bring_up_audio(&mut self) -> Result<()> {
        let _ = gip::GIP_PWR_ON; // referenced by the real implementation
        anyhow::bail!("usb::Device::bring_up_audio not implemented yet")
    }

    /// Poll EP1 IN for GIP reports (gamepad INPUT 0x20 and AUDIO_CONTROL 0x00
    /// media-button events) and EP2 IN for control/bulk events. Feeds the
    /// uinput layer (input.rs).
    ///
    /// TODO: async read loop; decode with gip::decode; dispatch on cmd.
    pub fn run_event_loop(&mut self) -> Result<()> {
        anyhow::bail!("usb::Device::run_event_loop not implemented yet")
    }
}
