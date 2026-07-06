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

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use anyhow::Result;

/// Set by SIGINT/SIGTERM so the main thread can unwind and run Drop (which
/// stops the iso engine + PipeWire bridge and lets nusb reattach xpad).
static STOP: AtomicBool = AtomicBool::new(false);

extern "C" fn on_signal(_sig: libc::c_int) {
    STOP.store(true, Ordering::SeqCst);
}

fn install_signal_handlers() {
    let handler = on_signal as *const () as usize;
    unsafe {
        libc::signal(libc::SIGINT, handler);
        libc::signal(libc::SIGTERM, handler);
    }
}

/// Block until SIGINT/SIGTERM, then return so Drop impls run.
fn park_until_signal() {
    install_signal_handlers();
    while !STOP.load(Ordering::SeqCst) {
        std::thread::sleep(Duration::from_millis(100));
    }
    log::info!("signal received — shutting down");
}

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
    let mut dev = match usb::Device::open() {
        Ok(d) => d,
        Err(e) if e.to_string().contains("not found") => {
            // Device absent: exit cleanly (0). Under systemd the udev rule
            // restarts us when the controller is plugged in.
            log::info!("Wolverine not connected — exiting (udev will start us on plug)");
            return Ok(());
        }
        Err(e) => return Err(e),
    };

    // 3. Audio bring-up on EP1 (IDENTIFY / AUDIO_FORMAT / POWER ON).
    //    This is the crown-jewel path: if POWER ON lands, the DAC/ADC wake up.
    dev.bring_up_audio()?;
    let (bus, addr) = dev.bus_addr();

    // 4. PipeWire sink + source in the invoking user's session.
    let (_bridge, rings) = audio::Bridge::start(
        audio::OUT_RATE,
        audio::OUT_CHANNELS,
        audio::IN_RATE,
        audio::IN_CHANNELS,
    )?;

    // 5. Async isochronous EP3 engine (libusb; claims interface 1 AFTER the EP1
    //    handshake). Bridges the PipeWire rings to EP3 OUT/IN.
    let _iso = iso::IsoAudio::start(bus, addr, rings.playback, rings.capture)?;

    // 2. Virtual input devices (gamepad + media keyboard).
    let mut uinput = input::Uinput::create(input::MediaMode::Absolute)?;

    log::info!("running — select 'Wolverine Headphones' / 'Wolverine Microphone'. Ctrl+C to stop.");

    // 6. Blocking EP1 event loop: gamepad + media buttons, until SIGINT/SIGTERM.
    //    On return, `_iso` and `_bridge` drop (iso stops, bridge stops) and nusb
    //    reattaches xpad — the gamepad comes back without a replug.
    install_signal_handlers();
    dev.run_event_loop(&mut uinput, &STOP)?;
    log::info!("shutting down");
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
    park_until_signal();
    Ok(())
}
