//! Native PipeWire client — sink + source. Port of tools/wolverine_pw.c.
//!
//! With pipewire-rs/libspa we build SPA audio formats natively in Rust — the
//! exact thing that forced the C shim (Python ctypes couldn't reach the
//! `static inline` `spa_format_audio_raw_build`).
//!
//! Two graph nodes:
//!   - "Wolverine Headphones" (Audio/Sink,   Direction::Input)  — 48kHz stereo
//!     S16LE. The system plays into it; the sink `process` callback reads the
//!     PCM out and pushes it to the playback ring (drained to EP3 OUT by iso.rs).
//!   - "Wolverine Microphone" (Audio/Source, Direction::Output) — 24kHz mono
//!     S16LE. The source `process` callback fills buffers from the capture ring
//!     (fed by EP3 IN); the system records it.
//!
//! Threading: pipewire-rs objects are !Send, so the whole PipeWire loop lives on
//! its own std::thread. The `rtrb` ring endpoints (Send) are created here and
//! moved in; the USB-side halves are returned in `Rings` for iso.rs. A
//! `pw::channel` carries the quit signal to the loop thread.

use std::thread::JoinHandle;

use anyhow::{anyhow, Result};
use pipewire as pw;
use pw::{properties::properties, spa};
use spa::pod::Pod;

use crate::ring;

/// Sink (headphones) format.
pub const OUT_RATE: u32 = 48_000;
pub const OUT_CHANNELS: u32 = 2;

/// Source (mic) format — independent from the sink.
pub const IN_RATE: u32 = 24_000;
pub const IN_CHANNELS: u32 = 1;

/// ~1.3s at 96 KB/s; generous, like the C shim's 512 KiB.
const RING_CAPACITY: usize = 256 * 1024;

/// USB-side ring endpoints handed to the iso engine (iso.rs).
pub struct Rings {
    /// System playback audio to drain into EP3 OUT (sink `process` produces it).
    pub playback: ring::Consumer<u8>,
    /// Mic audio captured from EP3 IN (source `process` consumes it).
    pub capture: ring::Producer<u8>,
}

/// Owns the PipeWire loop thread; drop or `stop()` tears it down.
pub struct Bridge {
    quit_tx: pw::channel::Sender<()>,
    thread: Option<JoinHandle<()>>,
}

impl Bridge {
    /// Create the sink + source nodes and start the PipeWire thread loop.
    /// Returns the bridge handle plus the USB-side ring endpoints.
    pub fn start(
        out_rate: u32,
        out_channels: u32,
        in_rate: u32,
        in_channels: u32,
    ) -> Result<(Self, Rings)> {
        point_at_user_session();

        // playback: sink process (producer) -> USB OUT (consumer, returned)
        let (play_prod, play_cons) = ring::new(RING_CAPACITY);
        // capture: USB IN (producer, returned) -> source process (consumer)
        let (cap_prod, cap_cons) = ring::new(RING_CAPACITY);

        let (quit_tx, quit_rx) = pw::channel::channel::<()>();

        let thread = std::thread::Builder::new()
            .name("wolverine-audio".into())
            .spawn(move || {
                if let Err(e) = run_pw(
                    out_rate,
                    out_channels,
                    in_rate,
                    in_channels,
                    play_prod,
                    cap_cons,
                    quit_rx,
                ) {
                    log::error!("PipeWire loop exited with error: {e}");
                }
            })
            .map_err(|e| anyhow!("spawn audio thread: {e}"))?;

        log::info!(
            "PipeWire bridge started: Wolverine Headphones ({out_rate}Hz/{out_channels}ch sink) \
             + Wolverine Microphone ({in_rate}Hz/{in_channels}ch source)"
        );
        Ok((
            Self {
                quit_tx,
                thread: Some(thread),
            },
            Rings {
                playback: play_cons,
                capture: cap_prod,
            },
        ))
    }

    /// Signal the loop to quit and join the thread.
    pub fn stop(&mut self) {
        let _ = self.quit_tx.send(());
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

impl Drop for Bridge {
    fn drop(&mut self) {
        self.stop();
    }
}

/// The driver runs as root but PipeWire lives in the desktop user's session.
/// Point XDG_RUNTIME_DIR at /run/user/<uid> so pw connects to the user's socket
/// instead of root's. The uid comes from SUDO_UID (when run via `sudo`) or
/// WOLVERINE_UID (set by the systemd unit).
fn point_at_user_session() {
    let uid = std::env::var("SUDO_UID")
        .ok()
        .or_else(|| std::env::var("WOLVERINE_UID").ok());
    if let Some(uid) = uid {
        let dir = format!("/run/user/{uid}");
        if std::path::Path::new(&dir).exists() {
            std::env::set_var("XDG_RUNTIME_DIR", &dir);
            log::info!("PipeWire target: user session (XDG_RUNTIME_DIR={dir})");
        }
    }
}

/// Runs entirely on the audio thread. Creates the loop, both streams, and blocks
/// in `mainloop.run()` until a quit message arrives.
fn run_pw(
    out_rate: u32,
    out_channels: u32,
    in_rate: u32,
    in_channels: u32,
    mut play_prod: ring::Producer<u8>,
    mut cap_cons: ring::Consumer<u8>,
    quit_rx: pw::channel::Receiver<()>,
) -> Result<(), pw::Error> {
    pw::init();
    let mainloop = pw::main_loop::MainLoopRc::new(None)?;
    let context = pw::context::ContextRc::new(&mainloop, None)?;

    // The user's PipeWire may not be up yet at boot (udev-started daemon racing
    // the session). Retry the connection for a few seconds before giving up.
    let core = {
        let mut attempt = 0;
        loop {
            match context.connect_rc(None) {
                Ok(core) => break core,
                Err(e) => {
                    attempt += 1;
                    if attempt >= 20 {
                        return Err(e);
                    }
                    log::warn!("PipeWire not ready (attempt {attempt}); retrying…");
                    std::thread::sleep(std::time::Duration::from_millis(500));
                }
            }
        }
    };

    let in_stride = (in_channels as usize) * std::mem::size_of::<i16>();

    // --- sink: "Wolverine Headphones" (system plays in, we read it out) ---
    let sink = pw::stream::StreamBox::new(
        &core,
        "Wolverine Headphones",
        properties! {
            *pw::keys::MEDIA_TYPE => "Audio",
            *pw::keys::MEDIA_CLASS => "Audio/Sink",
            *pw::keys::NODE_NAME => "wolverine_headphones",
            *pw::keys::NODE_DESCRIPTION => "Wolverine Headphones",
        },
    )?;
    let _sink_listener = sink
        .add_local_listener_with_user_data(())
        .process(move |stream, ()| {
            let Some(mut buffer) = stream.dequeue_buffer() else {
                return;
            };
            let datas = buffer.datas_mut();
            if datas.is_empty() {
                return;
            }
            let d = &mut datas[0];
            let size = d.chunk().size() as usize;
            let offset = d.chunk().offset() as usize;
            if let Some(slice) = d.data() {
                let end = (offset + size).min(slice.len());
                if offset < end {
                    ring::write(&mut play_prod, &slice[offset..end]);
                }
            }
        })
        .register()?;
    let sink_format = format_param(out_rate, out_channels);
    let mut sink_params = [Pod::from_bytes(&sink_format).unwrap()];
    sink.connect(
        spa::utils::Direction::Input,
        None,
        pw::stream::StreamFlags::AUTOCONNECT
            | pw::stream::StreamFlags::MAP_BUFFERS
            | pw::stream::StreamFlags::RT_PROCESS,
        &mut sink_params,
    )?;

    // --- source: "Wolverine Microphone" (we fill mic PCM, system records) ---
    let source = pw::stream::StreamBox::new(
        &core,
        "Wolverine Microphone",
        properties! {
            *pw::keys::MEDIA_TYPE => "Audio",
            *pw::keys::MEDIA_CLASS => "Audio/Source",
            *pw::keys::NODE_NAME => "wolverine_mic",
            *pw::keys::NODE_DESCRIPTION => "Wolverine Microphone",
        },
    )?;
    let _source_listener = source
        .add_local_listener_with_user_data(())
        .process(move |stream, ()| {
            let Some(mut buffer) = stream.dequeue_buffer() else {
                return;
            };
            let datas = buffer.datas_mut();
            if datas.is_empty() {
                return;
            }
            let d = &mut datas[0];
            let want = if let Some(slice) = d.data() {
                let frames = slice.len() / in_stride;
                let want = frames * in_stride;
                let got = ring::read(&mut cap_cons, &mut slice[..want]);
                for b in &mut slice[got..want] {
                    *b = 0; // underrun -> silence
                }
                want
            } else {
                0
            };
            let chunk = d.chunk_mut();
            *chunk.offset_mut() = 0;
            *chunk.stride_mut() = in_stride as i32;
            *chunk.size_mut() = want as u32;
        })
        .register()?;
    let source_format = format_param(in_rate, in_channels);
    let mut source_params = [Pod::from_bytes(&source_format).unwrap()];
    source.connect(
        spa::utils::Direction::Output,
        None,
        pw::stream::StreamFlags::AUTOCONNECT
            | pw::stream::StreamFlags::MAP_BUFFERS
            | pw::stream::StreamFlags::RT_PROCESS,
        &mut source_params,
    )?;

    // Quit signal: upgrade a weak ref inside the callback to avoid a cycle.
    let weak = mainloop.downgrade();
    let _quit = quit_rx.attach(mainloop.loop_(), move |()| {
        if let Some(ml) = weak.upgrade() {
            ml.quit();
        }
    });

    mainloop.run(); // blocks until quit
    Ok(())
}

/// Serialize a fixed S16LE format (rate/channels) into a SPA EnumFormat pod.
/// Mirrors `build_format` in wolverine_pw.c (no explicit channel positions).
fn format_param(rate: u32, channels: u32) -> Vec<u8> {
    let mut info = spa::param::audio::AudioInfoRaw::new();
    info.set_format(spa::param::audio::AudioFormat::S16LE);
    info.set_rate(rate);
    info.set_channels(channels);

    let obj = pw::spa::pod::Object {
        type_: pw::spa::utils::SpaTypes::ObjectParamFormat.as_raw(),
        id: pw::spa::param::ParamType::EnumFormat.as_raw(),
        properties: info.into(),
    };
    pw::spa::pod::serialize::PodSerializer::serialize(
        std::io::Cursor::new(Vec::new()),
        &pw::spa::pod::Value::Object(obj),
    )
    .unwrap()
    .0
    .into_inner()
}
