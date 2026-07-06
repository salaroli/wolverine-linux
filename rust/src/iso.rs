//! Asynchronous isochronous EP3 audio engine. Faithful port of iso_audio.py.
//!
//! nusb has no public isochronous API, so this module drives libusb directly
//! (via libusb1-sys) — exactly what the Python driver does with python-libusb1.
//! It opens a SECOND handle to the same device (nusb owns interfaces 0/2 for the
//! GIP handshake; libusb owns interface 1 here). The GIP bring-up on EP1 must
//! have already run before we claim interface 1 (ordering handled by main).
//!
//! EP3 is full-speed isochronous, wMaxPacketSize=228, bInterval=1 → ~1000 pkt/s,
//! matching 48kHz stereo S16 on OUT. We keep N transfers (each carrying several
//! 1ms iso packets) permanently in flight, refilled and resubmitted from their
//! completion callback, so the host controller never starves a frame.
//!
//! Framing (asymmetric — see CONTEXT.md):
//!   OUT (headphones): [0x60 0x21 <seq> <len=192>] + 192B PCM per packet. NOT
//!     raw PCM — the device desyncs and renders robotic voice without the header.
//!   IN  (mic): GIP-framed `60 21 <seq> <len> | <2B sub-header> | S16LE 24k mono`.
//!
//! All unsafe FFI is contained here. The libusb event thread is the sole owner
//! of the ring endpoints it touches (play Consumer for OUT, cap Producer for
//! IN), so the rtrb SPSC contract holds against the PipeWire threads.

use std::os::raw::{c_int, c_uint, c_void};
use std::ptr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use anyhow::{bail, Result};
use libusb1_sys as ffi;
use libusb1_sys::constants::{
    LIBUSB_TRANSFER_CANCELLED, LIBUSB_TRANSFER_COMPLETED, LIBUSB_TRANSFER_NO_DEVICE,
};

use crate::gip;
use crate::ring;

// EP3 (interface 1).
const EP_AUDIO_OUT: u8 = 0x03;
const EP_AUDIO_IN: u8 = 0x83;
const AUDIO_IFACE: c_int = 1;
const AUDIO_ALT: c_int = 1;

/// OUT: 192 PCM bytes per 1ms frame (48 frames × 2ch × 2B).
const OUT_PCM_BYTES: usize = 192;
/// IN: read up to the endpoint's max packet (228); actual_length tells the truth.
const IN_PKT_BYTES: usize = 228;

const OUT_PKTS_PER_XFER: usize = 8;
const OUT_NUM_XFERS: usize = 6;
const IN_PKTS_PER_XFER: usize = 8;
const IN_NUM_XFERS: usize = 4;

/// Prime the playback ring to ~40ms before draining real audio, so a bursty
/// PipeWire quantum can't leave us mid-stream with an empty ring.
const OUT_PRIME_BYTES: usize = OUT_PCM_BYTES * 40;

/// Shared state reachable from the libusb completion callbacks via `user_data`.
/// After `start()`, every non-atomic field is touched ONLY by the libusb event
/// thread (callbacks run inline in `libusb_handle_events`, one at a time, and
/// the 5s diagnostic runs between those calls) — so `&mut` from the raw pointer
/// is sound. `stop()` only ever touches `running` (atomic).
struct EngineState {
    play: ring::Consumer<u8>, // system audio to send on EP3 OUT
    cap: ring::Producer<u8>,  // mic audio received on EP3 IN
    out_hdr: Vec<u8>,         // GIP AUDIO_SAMPLES header template; byte[2] = seq
    out_pkt_size: usize,      // out_hdr.len() + OUT_PCM_BYTES (iso packet stride)
    out_seq: u8,              // GIP sequence 1..=255 (never 0)
    primed: bool,
    running: AtomicBool,
    out_pkts: u64,
    out_silence: u64,
    in_bytes: u64,
}

/// The async iso engine. Owns interface 1 (alt 1) via its own libusb handle.
pub struct IsoAudio {
    ctx: *mut ffi::libusb_context,
    handle: *mut ffi::libusb_device_handle,
    engine: *mut EngineState,
    xfers: Vec<*mut ffi::libusb_transfer>,
    // Transfer buffers, kept alive (their heap ptr lives inside each transfer).
    _bufs: Vec<Vec<u8>>,
    thread: Option<JoinHandle<()>>,
}

impl IsoAudio {
    /// Open interface 1 on the same device (matched by `bus`/`addr`), claim it,
    /// set alt=1, prime and submit all transfers, and spin up the event thread.
    ///
    /// `playback` is drained into EP3 OUT (fed by the PipeWire sink); `capture`
    /// receives mic PCM from EP3 IN (consumed by the PipeWire source).
    pub fn start(
        bus: u8,
        addr: u8,
        playback: ring::Consumer<u8>,
        capture: ring::Producer<u8>,
    ) -> Result<Self> {
        unsafe {
            let mut ctx: *mut ffi::libusb_context = ptr::null_mut();
            if ffi::libusb_init(&mut ctx) < 0 {
                bail!("libusb_init failed");
            }

            let handle = match open_interface(ctx, bus, addr) {
                Ok(h) => h,
                Err(e) => {
                    ffi::libusb_exit(ctx);
                    return Err(e);
                }
            };

            // xpad is already detached (by nusb); interface 1 has no kernel
            // driver. Don't let libusb fight nusb over auto-detach.
            ffi::libusb_set_auto_detach_kernel_driver(handle, 0);
            if ffi::libusb_claim_interface(handle, AUDIO_IFACE) < 0 {
                ffi::libusb_close(handle);
                ffi::libusb_exit(ctx);
                bail!("libusb claim interface 1 (iso audio) failed");
            }
            ffi::libusb_set_interface_alt_setting(handle, AUDIO_IFACE, AUDIO_ALT);

            // GIP AUDIO_SAMPLES header prefixed to every OUT packet (only byte[2],
            // the sequence, changes per packet). Same framing the mic uses on IN.
            let out_hdr =
                gip::build_header(gip::cmd::AUDIO_SAMPLES, gip::OPTS_INTERNAL, 1, OUT_PCM_BYTES as u32);
            let out_pkt_size = out_hdr.len() + OUT_PCM_BYTES;

            let engine = Box::into_raw(Box::new(EngineState {
                play: playback,
                cap: capture,
                out_hdr,
                out_pkt_size,
                out_seq: 1,
                primed: false,
                running: AtomicBool::new(true),
                out_pkts: 0,
                out_silence: 0,
                in_bytes: 0,
            }));

            let mut xfers = Vec::new();
            let mut bufs = Vec::new();

            // OUT transfers (start as silence).
            let out_total = out_pkt_size * OUT_PKTS_PER_XFER;
            for _ in 0..OUT_NUM_XFERS {
                let mut buf = vec![0u8; out_total];
                let xfer = ffi::libusb_alloc_transfer(OUT_PKTS_PER_XFER as c_int);
                if xfer.is_null() {
                    bail!("libusb_alloc_transfer (OUT) failed");
                }
                ffi::libusb_fill_iso_transfer(
                    xfer,
                    handle,
                    EP_AUDIO_OUT,
                    buf.as_mut_ptr(),
                    out_total as c_int,
                    OUT_PKTS_PER_XFER as c_int,
                    on_out,
                    engine as *mut c_void,
                    0,
                );
                ffi::libusb_set_iso_packet_lengths(xfer, out_pkt_size as c_uint);
                ffi::libusb_submit_transfer(xfer);
                xfers.push(xfer);
                bufs.push(buf);
            }

            // IN transfers.
            let in_total = IN_PKT_BYTES * IN_PKTS_PER_XFER;
            for _ in 0..IN_NUM_XFERS {
                let mut buf = vec![0u8; in_total];
                let xfer = ffi::libusb_alloc_transfer(IN_PKTS_PER_XFER as c_int);
                if xfer.is_null() {
                    bail!("libusb_alloc_transfer (IN) failed");
                }
                ffi::libusb_fill_iso_transfer(
                    xfer,
                    handle,
                    EP_AUDIO_IN,
                    buf.as_mut_ptr(),
                    in_total as c_int,
                    IN_PKTS_PER_XFER as c_int,
                    on_in,
                    engine as *mut c_void,
                    0,
                );
                ffi::libusb_set_iso_packet_lengths(xfer, IN_PKT_BYTES as c_uint);
                ffi::libusb_submit_transfer(xfer);
                xfers.push(xfer);
                bufs.push(buf);
            }

            // Event thread. Raw pointers are !Send, so ferry them as usize.
            let ctx_addr = ctx as usize;
            let engine_addr = engine as usize;
            let thread = std::thread::Builder::new()
                .name("wolverine-iso".into())
                .spawn(move || event_loop(ctx_addr as *mut _, engine_addr as *mut _))
                .map_err(|e| anyhow::anyhow!("spawn iso thread: {e}"))?;

            log::info!(
                "iso EP3 engine up — OUT {OUT_NUM_XFERS}×{OUT_PKTS_PER_XFER}pkt \
                 ({}ms in flight), IN {IN_NUM_XFERS}×{IN_PKTS_PER_XFER}pkt",
                OUT_NUM_XFERS * OUT_PKTS_PER_XFER
            );
            Ok(Self {
                ctx,
                handle,
                engine,
                xfers,
                _bufs: bufs,
                thread: Some(thread),
            })
        }
    }

    /// Stop the engine: signal quit, join the event thread, then (single-threaded)
    /// cancel + drain + free every transfer and release the device.
    pub fn stop(&mut self) {
        if self.engine.is_null() {
            return;
        }
        unsafe {
            (*self.engine).running.store(false, Ordering::Relaxed);
        }
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
        // Event thread is gone; we're the only one touching libusb now.
        unsafe {
            for &x in &self.xfers {
                ffi::libusb_cancel_transfer(x);
            }
            // Drain cancellations so no submitted transfer is freed underfoot.
            let tv = libc::timeval {
                tv_sec: 0,
                tv_usec: 50_000,
            };
            let deadline = Instant::now() + Duration::from_secs(1);
            while Instant::now() < deadline {
                ffi::libusb_handle_events_timeout(self.ctx, &tv);
            }
            for &x in &self.xfers {
                ffi::libusb_free_transfer(x);
            }
            self.xfers.clear();
            ffi::libusb_release_interface(self.handle, AUDIO_IFACE);
            ffi::libusb_close(self.handle);
            ffi::libusb_exit(self.ctx);
            drop(Box::from_raw(self.engine)); // reclaim engine (drops ring ends)
            self.engine = ptr::null_mut();
        }
        log::info!("iso EP3 engine stopped");
    }
}

impl Drop for IsoAudio {
    fn drop(&mut self) {
        self.stop();
    }
}

// IsoAudio owns raw pointers into libusb; it is used from a single thread (main)
// plus the event thread it manages internally. The pointers never escape.
unsafe impl Send for IsoAudio {}

/// libusb OUT completion: refill every packet (GIP header + PCM/silence) and
/// resubmit. Ported from `_on_out`.
extern "system" fn on_out(transfer: *mut ffi::libusb_transfer) {
    unsafe {
        let st = &mut *((*transfer).user_data as *mut EngineState);
        if !st.running.load(Ordering::Relaxed) {
            return;
        }
        let status = (*transfer).status;
        if status == LIBUSB_TRANSFER_NO_DEVICE || status == LIBUSB_TRANSFER_CANCELLED {
            return;
        }

        // Wait for a cushion before draining real audio.
        if !st.primed && ring::avail(&st.play) >= OUT_PRIME_BYTES {
            st.primed = true;
        }

        let base = (*transfer).buffer;
        let hlen = st.out_hdr.len();
        for i in 0..OUT_PKTS_PER_XFER {
            let off = i * st.out_pkt_size;

            // GIP header with the next sequence (1..=255, never 0).
            st.out_hdr[2] = st.out_seq;
            st.out_seq = if st.out_seq < 255 { st.out_seq + 1 } else { 1 };
            ptr::copy_nonoverlapping(st.out_hdr.as_ptr(), base.add(off), hlen);

            // PCM straight into the transfer buffer, else silence.
            let pcm_ptr = base.add(off + hlen);
            let pcm = std::slice::from_raw_parts_mut(pcm_ptr, OUT_PCM_BYTES);
            let n = if st.primed { ring::read(&mut st.play, pcm) } else { 0 };
            if n < OUT_PCM_BYTES {
                ptr::write_bytes(pcm_ptr.add(n), 0, OUT_PCM_BYTES - n);
                st.out_silence += 1;
            }
            st.out_pkts += 1;
        }

        ffi::libusb_submit_transfer(transfer);
    }
}

/// libusb IN completion: parse each iso packet's GIP frame and push PCM to the
/// capture ring, then resubmit. Ported from `_on_in`.
extern "system" fn on_in(transfer: *mut ffi::libusb_transfer) {
    unsafe {
        let st = &mut *((*transfer).user_data as *mut EngineState);
        if !st.running.load(Ordering::Relaxed) {
            return;
        }
        let status = (*transfer).status;
        if status == LIBUSB_TRANSFER_NO_DEVICE || status == LIBUSB_TRANSFER_CANCELLED {
            return;
        }

        let n = (*transfer).num_iso_packets;
        for i in 0..n {
            let desc = &*(*transfer).iso_packet_desc.as_ptr().add(i as usize);
            if desc.status != LIBUSB_TRANSFER_COMPLETED || desc.actual_length == 0 {
                continue;
            }
            let pkt_ptr = ffi::libusb_get_iso_packet_buffer_simple(transfer, i as c_uint);
            if pkt_ptr.is_null() {
                continue;
            }
            let data = std::slice::from_raw_parts(pkt_ptr, desc.actual_length as usize);
            if let Some(pcm) = parse_in(data) {
                if !pcm.is_empty() {
                    st.in_bytes += pcm.len() as u64;
                    ring::write(&mut st.cap, pcm);
                }
            }
        }

        ffi::libusb_submit_transfer(transfer);
    }
}

/// The libusb event loop (own thread). Pumps completions until `running` clears,
/// printing a diagnostic every 5s.
fn event_loop(ctx: *mut ffi::libusb_context, engine: *mut EngineState) {
    let mut last = Instant::now();
    let tv = libc::timeval {
        tv_sec: 0,
        tv_usec: 100_000,
    };
    loop {
        if !unsafe { (*engine).running.load(Ordering::Relaxed) } {
            break;
        }
        unsafe {
            ffi::libusb_handle_events_timeout(ctx, &tv);
        }
        let dt = last.elapsed();
        if dt >= Duration::from_secs(5) {
            unsafe {
                let st = &mut *engine;
                let secs = dt.as_secs_f64();
                log::info!(
                    "[iso] OUT {:.0} pkt/s ({} silent/5s, ring {}B) | IN {:.0} PCM B/s",
                    st.out_pkts as f64 / secs,
                    st.out_silence,
                    ring::avail(&st.play),
                    st.in_bytes as f64 / secs,
                );
                st.out_pkts = 0;
                st.out_silence = 0;
                st.in_bytes = 0;
            }
            last = Instant::now();
        }
    }
}

/// Parse a received EP3 IN packet: decode the GIP header, then skip the 2-byte
/// `length_out` (le16) sub-header. Returns the raw PCM slice (24kHz mono).
pub fn parse_in(buf: &[u8]) -> Option<&[u8]> {
    let pkt = gip::decode(buf)?;
    if pkt.cmd != gip::cmd::AUDIO_SAMPLES {
        return None;
    }
    pkt.payload.get(2..)
}

/// Open a libusb handle to the Wolverine at `bus`/`addr` (verifying VID/PID).
unsafe fn open_interface(
    ctx: *mut ffi::libusb_context,
    bus: u8,
    addr: u8,
) -> Result<*mut ffi::libusb_device_handle> {
    let mut list: *const *mut ffi::libusb_device = ptr::null();
    let n = ffi::libusb_get_device_list(ctx, &mut list);
    if n < 0 {
        bail!("libusb_get_device_list failed");
    }

    let mut chosen: *mut ffi::libusb_device = ptr::null_mut();
    for i in 0..n as isize {
        let dev = *list.offset(i);
        if ffi::libusb_get_bus_number(dev) != bus || ffi::libusb_get_device_address(dev) != addr {
            continue;
        }
        let mut desc: ffi::libusb_device_descriptor = std::mem::zeroed();
        if ffi::libusb_get_device_descriptor(dev, &mut desc) == 0
            && desc.idVendor == gip::VID
            && desc.idProduct == gip::PID
        {
            chosen = dev;
            break;
        }
    }

    let mut handle: *mut ffi::libusb_device_handle = ptr::null_mut();
    let rc = if chosen.is_null() {
        -1
    } else {
        ffi::libusb_open(chosen, &mut handle)
    };
    ffi::libusb_free_device_list(list, 1);

    if rc < 0 || handle.is_null() {
        bail!("libusb: Wolverine not found / open failed at bus {bus} addr {addr}");
    }
    Ok(handle)
}
