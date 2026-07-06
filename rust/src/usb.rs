//! USB transport: open the device, detach xpad, claim interfaces, and run the
//! GIP control handshake (IDENTIFY / AUDIO_FORMAT / POWER ON) on EP1.
//!
//! Ported from tools/gip_init.py (`main` + `gip_init`).
//!
//! USB interface map (from lsusb -v, see CONTEXT.md):
//!   Interface 0 alt 0 — EP1 OUT/IN (Interrupt, 64B)   -> Gamepad GIP (xpad)
//!   Interface 1 alt 1 — EP3 OUT/IN (Isochronous, 228B) -> Audio (see iso.rs)
//!   Interface 2 alt 1 — EP2 OUT/IN (Bulk, 64B)          -> Control / events
//!
//! Ownership split (mirrors the Python hybrid): here we claim interfaces 0 (EP1)
//! and 2 (EP2). Interface 1 (EP3 iso) is claimed separately by the iso engine
//! from a clone of the same nusb::Device — the GIP handshake on EP1 MUST happen
//! BEFORE the iso engine grabs interface 1 and flips it to alt=1.

use std::time::{Duration, Instant};

use anyhow::{anyhow, Result};
use async_io::Timer;
use futures_lite::future::{block_on, or};
use nusb::transfer::{Completion, RequestBuffer, ResponseBuffer};
use nusb::Interface;

use crate::gip;

// Endpoint addresses (bEndpointAddress from the descriptors).
pub const EP1_IN: u8 = 0x81; // interrupt, interface 0 (GIP / gamepad)
pub const EP1_OUT: u8 = 0x01;
pub const EP2_IN: u8 = 0x82; // bulk, interface 2 (control / events)
pub const EP2_OUT: u8 = 0x02;
pub const EP3_IN: u8 = 0x83; // isochronous, interface 1 (audio) — see iso.rs
pub const EP3_OUT: u8 = 0x03;

const TIMEOUT: Duration = Duration::from_millis(500);

/// Handle to the opened Wolverine, owning the control interfaces (0 and 2).
pub struct Device {
    dev: nusb::Device,
    gip: Interface,  // interface 0 — EP1 GIP
    ctrl: Interface, // interface 2 — EP2 bulk
    seq: gip::SeqCounter,
    bus: u8,
    addr: u8,
}

impl Device {
    /// Find and open the Wolverine, detach the kernel `xpad` driver from
    /// interface 0, and claim interfaces 0 and 2. Interface 1 is left for the
    /// iso engine (which claims it from `nusb_device()`).
    pub fn open() -> Result<Self> {
        let info = nusb::list_devices()?
            .find(|d| d.vendor_id() == gip::VID && d.product_id() == gip::PID)
            .ok_or_else(|| anyhow!("Wolverine ({:04x}:{:04x}) not found", gip::VID, gip::PID))?;
        let bus = info.bus_number();
        let addr = info.device_address();
        log::info!(
            "found {} {} (bus {}, addr {})",
            info.manufacturer_string().unwrap_or("?"),
            info.product_string().unwrap_or("Wolverine"),
            bus,
            addr
        );

        let dev = info.open()?;
        // detach_and_claim detaches xpad from interface 0 and claims it in one go.
        let gip = dev
            .detach_and_claim_interface(0)
            .map_err(|e| anyhow!("claim interface 0 (GIP): {e}"))?;
        let ctrl = dev
            .claim_interface(2)
            .map_err(|e| anyhow!("claim interface 2 (bulk): {e}"))?;

        log::info!("claimed interfaces 0 (EP1 GIP) and 2 (EP2 bulk)");
        Ok(Self {
            dev,
            gip,
            ctrl,
            seq: gip::SeqCounter::default(),
            bus,
            addr,
        })
    }

    /// USB bus/address, so the iso engine (libusb) can match the same device.
    pub fn bus_addr(&self) -> (u8, u8) {
        (self.bus, self.addr)
    }

    /// Run the audio bring-up handshake on EP1 (ported from `gip_init`):
    ///
    ///   1. Drain spontaneous packets (ANNOUNCE etc.) the device queued at boot.
    ///   2. IDENTIFY, then receive + ACK the (possibly chunked) response.
    ///   3. GIP auth — SKIPPED: the Wolverine never answers and the jack path
    ///      doesn't need it (xone skips auth/battery for standalone/jack).
    ///   4. AUDIO_FORMAT — 48kHz stereo; device echoes it.
    ///   5. POWER ON — the step that actually wakes the DAC/ADC. Without it EP3
    ///      only streams zeros.
    ///   6. Activate alt=1 on interface 2 (EP2 bulk) so control events flow.
    pub fn bring_up_audio(&mut self) -> Result<()> {
        // 1. Clear anything already queued (ANNOUNCE/STATUS).
        log::debug!("pre-drain EP1");
        self.drain(Duration::from_millis(300));

        // 2. IDENTIFY.
        let seq = self.seq.next();
        let pkt = gip::build_packet(gip::cmd::IDENTIFY, gip::OPTS_INTERNAL, seq, &[]);
        self.send(pkt, "IDENTIFY")?;
        match self.receive_identify_response() {
            Ok(Some(data)) => log::info!("IDENTIFY response: {} bytes", data.len()),
            Ok(None) => log::info!("IDENTIFY: no response (device already swapped by xpad)"),
            Err(e) => log::warn!("IDENTIFY recv: {e}"),
        }

        // 3. Auth intentionally skipped.
        log::info!("skipping GIP auth (Wolverine doesn't implement it; jack path doesn't need it)");

        // 4. AUDIO_FORMAT (48kHz stereo). Payload = [0x02, in=0x10, out=0x10].
        let seq = self.seq.next();
        let pkt = gip::build_packet(
            gip::cmd::AUDIO_CONTROL,
            gip::OPTS_INTERNAL,
            seq,
            &gip::AUDIO_FORMAT_48K_STEREO,
        );
        self.send(pkt, "AUDIO_FORMAT")?;
        self.log_response("AUDIO_FORMAT");

        // 5. POWER ON — THE key step (cmd 0x05, payload [GIP_PWR_ON]).
        let seq = self.seq.next();
        let pkt = gip::build_packet(gip::cmd::POWER, gip::OPTS_INTERNAL, seq, &[gip::GIP_PWR_ON]);
        self.send(pkt, "POWER_ON")?;
        self.log_response("POWER_ON");

        // HW VOLUME (sub 0x03) is intentionally NOT sent — jack headset path,
        // xone skips it (matches SEND_HW_VOLUME=False in the Python driver).

        // 6. Bring EP2 bulk online.
        self.ctrl
            .set_alt_setting(1)
            .map_err(|e| anyhow!("interface 2 alt=1: {e}"))?;
        log::info!("audio bring-up complete (POWER ON sent); interface 2 alt=1");
        Ok(())
    }

    /// Poll EP1 IN (gamepad INPUT 0x20 + AUDIO_CONTROL media buttons) and EP2 IN
    /// (control/bulk). Dispatches to the uinput layer (input.rs).
    ///
    /// TODO(input): wire to input::Uinput once that module lands.
    pub fn run_event_loop(&mut self) -> Result<()> {
        anyhow::bail!("usb::Device::run_event_loop not implemented yet (needs input.rs)")
    }

    // --- transfer helpers (EP1) ---

    /// Send a GIP packet on EP1 OUT, bounded by TIMEOUT.
    fn send(&self, data: Vec<u8>, label: &str) -> Result<()> {
        log::trace!("→ {label} ({} bytes)", data.len());
        let fut = self.gip.interrupt_out(EP1_OUT, data);
        let res: Option<Completion<ResponseBuffer>> = block_on(or(
            async move { Some(fut.await) },
            async move {
                Timer::after(TIMEOUT).await;
                None
            },
        ));
        match res {
            Some(c) => c
                .status
                .map_err(|e| anyhow!("{label} EP1 OUT failed: {e:?}")),
            None => Err(anyhow!("{label} EP1 OUT timed out")),
        }
    }

    /// Read one packet (≤64B) from EP1 IN, up to `timeout`. `None` on timeout.
    fn recv(&self, timeout: Duration) -> Option<Vec<u8>> {
        let fut = self.gip.interrupt_in(EP1_IN, RequestBuffer::new(64));
        let res: Option<Completion<Vec<u8>>> = block_on(or(
            async move { Some(fut.await) },
            async move {
                Timer::after(timeout).await;
                None
            },
        ));
        match res {
            Some(c) if c.status.is_ok() => Some(c.data),
            _ => None,
        }
    }

    /// Read and discard everything pending on EP1 for up to `total`.
    fn drain(&self, total: Duration) {
        let deadline = Instant::now() + total;
        while Instant::now() < deadline {
            let to = (deadline - Instant::now()).min(Duration::from_millis(100));
            if self.recv(to).is_none() {
                break;
            }
        }
    }

    /// Best-effort: read one response and log its command, for diagnostics.
    fn log_response(&self, label: &str) {
        if let Some(raw) = self.recv(TIMEOUT) {
            if let Some(pkt) = gip::decode(&raw) {
                log::info!("{label} ← cmd=0x{:02x} seq={} ({}B)", pkt.cmd, pkt.seq, raw.len());
            }
        } else {
            log::debug!("{label}: no response");
        }
    }

    /// Receive the device's IDENTIFY response, ACKing each chunk. The Wolverine
    /// uses CHUNK (0x80) WITHOUT CHUNK_START (0x40); chunk order comes from
    /// `chunk_offset`. Ported from `_receive_identify_response`.
    fn receive_identify_response(&mut self) -> Result<Option<Vec<u8>>> {
        let mut buf = vec![0u8; 512];
        let total = 0usize; // remaining is reported relative to this; stays 0 (unknown)
        let mut recvd = 0usize;
        let deadline = Instant::now() + Duration::from_millis(2500);

        while Instant::now() < deadline {
            let to = (deadline - Instant::now()).min(Duration::from_millis(300));
            let raw = match self.recv(to) {
                Some(r) => r,
                None => {
                    if recvd > 0 {
                        break; // got data, timeout means transfer done
                    }
                    continue;
                }
            };
            let pkt = match gip::decode(&raw) {
                Some(p) => p,
                None => continue,
            };
            if pkt.cmd != gip::cmd::IDENTIFY {
                continue;
            }
            let data = &raw[pkt.hdr_len..(pkt.hdr_len + pkt.pkt_len).min(raw.len())];

            if !pkt.is_chunked() {
                return Ok(Some(data.to_vec())); // single non-chunked response
            }
            if pkt.pkt_len == 0 {
                break; // empty chunk = transfer complete
            }

            let end = pkt.chunk_offset + data.len();
            if end > buf.len() {
                buf.resize(end + 64, 0);
            }
            buf[pkt.chunk_offset..end].copy_from_slice(data);
            recvd = recvd.max(pkt.chunk_offset + pkt.pkt_len);

            if pkt.wants_ack() {
                self.send_chunk_ack(pkt.seq, gip::cmd::IDENTIFY, recvd, total)?;
            }
        }

        Ok((recvd > 0).then(|| buf[..recvd.min(buf.len())].to_vec()))
    }

    /// ACK a received chunk (ported from `_send_gip_ack_for_chunk`).
    /// Payload: unknown(1) + cmd(1) + opts(1) + le16(received) + pad(2) + le16(remaining).
    fn send_chunk_ack(&self, recv_seq: u8, recv_cmd: u8, received: usize, total: usize) -> Result<()> {
        let remaining = (total.saturating_sub(received)) as u16;
        let mut payload = vec![0x00, recv_cmd, gip::OPTS_INTERNAL];
        payload.extend_from_slice(&(received as u16).to_le_bytes());
        payload.extend_from_slice(&[0x00, 0x00]);
        payload.extend_from_slice(&remaining.to_le_bytes());
        let pkt = gip::build_packet(gip::cmd::ACKNOWLEDGE, gip::OPTS_INTERNAL, recv_seq, &payload);
        self.send(pkt, "CHUNK_ACK")
    }
}
