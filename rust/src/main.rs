//! wolverined — native Rust userspace driver for the Razer Wolverine Ultimate.
//!
//! Port of the Python driver (tools/gip_init.py + iso_audio.py + wolverine_pw.c)
//! into a single static binary, aimed at shipping as a systemd daemon.
//!
//! Orchestration (mirrors gip_init.py's numbered steps):
//!   1. Open device, detach xpad, claim control interfaces (0, 2).      [usb]
//!   2. Create uinput gamepad + keyboard-only device.                   [input]
//!   3. GIP handshake: IDENTIFY -> AUDIO_FORMAT -> POWER ON.            [usb]
//!   4. Start the PipeWire bridge (sink + source, user session).        [audio]
//!   5. Start the async isochronous EP3 engine (claims interface 1).    [iso]
//!   6. Run the event loop: EP1 gamepad + media buttons, EP2 control.   [usb+input]
//!
//! Status: SKELETON. Every module is a stub with the reverse-engineered
//! protocol constants and the bring-up sequence documented inline. See
//! ../CONTEXT.md for the full knowledge base. Wire the TODOs bottom-up:
//! gip (done) -> usb -> iso -> audio -> input -> main.

// Temporary while the port is in progress: several modules are stubs whose
// items are wired up in later steps. Remove once all modules are implemented.
#![allow(dead_code)]

mod audio;
mod gip;
mod input;
mod iso;
mod usb;

use anyhow::Result;

fn main() -> Result<()> {
    env_logger::init();
    log::info!("wolverined starting");

    // 1. USB device + control interfaces, detach xpad.
    let mut dev = usb::Device::open()?;

    // 3. Audio bring-up on EP1 (IDENTIFY / AUDIO_FORMAT / POWER ON).
    //    This is the crown-jewel path: if POWER ON lands, the DAC/ADC wake up.
    dev.bring_up_audio()?;

    // --- Everything below is not implemented yet (stubs). Until iso.rs /
    //     audio.rs / input.rs land, `wolverined` runs the handshake and exits,
    //     which is a valid smoke-test of the bring-up sequence on real hardware.
    log::info!("handshake complete — remaining stages (audio/iso/input) not implemented yet");

    // 2. Virtual input devices.                    (input.rs — pending)
    // let _uinput = input::Uinput::create()?;
    // 4. PipeWire sink + source (user session).     (audio.rs — pending, blocked on pipewire-rs)
    // let _bridge = audio::Bridge::start()?;
    // 5. Async isochronous EP3 engine.              (iso.rs — pending)
    // let _iso = iso::IsoAudio::start()?;
    // 6. Blocking event loop: gamepad + media.      (usb.rs::run_event_loop — pending)
    // dev.run_event_loop()?;

    Ok(())
}
