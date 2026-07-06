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

mod audio;
mod gip;
mod input;
mod iso;
mod usb;

use anyhow::Result;

fn main() -> Result<()> {
    env_logger::init();
    log::info!("wolverined starting (skeleton)");

    // 1. USB device + control interfaces, detach xpad.
    let mut dev = usb::Device::open()?;

    // 2. Virtual input devices.
    let _uinput = input::Uinput::create()?;

    // 3. Audio bring-up on EP1 (IDENTIFY / AUDIO_FORMAT / POWER ON).
    dev.bring_up_audio()?;

    // 4. PipeWire sink + source in the invoking user's session.
    let _bridge = audio::Bridge::start()?;

    // 5. Async isochronous EP3 engine (claims interface 1 AFTER the EP1 handshake).
    let _iso = iso::IsoAudio::start()?;

    // 6. Blocking event loop: gamepad + media buttons on EP1, control on EP2.
    dev.run_event_loop()?;

    Ok(())
}
