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
mod ring;
mod usb;

use anyhow::Result;

fn main() -> Result<()> {
    env_logger::init();

    // `wolverined audio` — audio-only smoke test: bring up the PipeWire nodes
    // without touching USB (no root / no xpad detach needed). Verify with
    // `wpctl status`, `pw-play --target wolverine_headphones <file>`, etc.
    if std::env::args().nth(1).as_deref() == Some("audio") {
        return audio_smoke();
    }

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

/// Audio-only smoke test (no USB): start the PipeWire bridge and park.
fn audio_smoke() -> Result<()> {
    log::info!("audio-only smoke test");
    let (_bridge, _rings) = audio::Bridge::start(
        audio::OUT_RATE,
        audio::OUT_CHANNELS,
        audio::IN_RATE,
        audio::IN_CHANNELS,
    )?;
    log::info!(
        "nodes up: 'Wolverine Headphones' + 'Wolverine Microphone'. \
         Check `wpctl status`. Nothing drains the sink yet (no controller), \
         so playback just fills the ring and the mic outputs silence. Ctrl+C to exit."
    );
    loop {
        std::thread::sleep(std::time::Duration::from_secs(3600));
    }
}
