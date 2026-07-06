# wolverine-linux

Open-source Linux driver for the **Razer Wolverine Ultimate** controller — brings
up the features the kernel doesn't: the 3.5mm **audio jack**, **microphone**,
**media buttons**, and **rumble**, plus the gamepad itself.

Ships as a single native **Rust** binary (`wolverined`). A Python reference
implementation lives in [`tools/`](tools) and documents the reverse engineering.

## Status

| Feature | Status |
|---|---|
| Gamepad (buttons, sticks, triggers, d-pad, Guide) | ✅ Re-exposed via uinput (standard Xbox One mapping) |
| Headphone jack (output) | ✅ PipeWire sink **Wolverine Headphones** (48 kHz stereo) |
| Microphone (input) | ✅ PipeWire source **Wolverine Microphone** (24 kHz mono) |
| Media buttons (volume / mic mute) | ✅ Mirrored to the system default sink/source |
| Rumble / force feedback | ✅ `FF_RUMBLE` → GIP rumble command |
| Audio latency | ✅ Bounded & low — consumer-side ring trim (drop-oldest); tunable |

Everything above is validated on real hardware.

> **Audio breakthrough:** the 3.5mm jack is **not** an Xbox-only hardware limitation.
> Both output (DAC) and input (ADC) work on Linux. The missing piece was the GIP
> `POWER` command (`0x05`) with `GIP_PWR_ON` (`0x00`), sent after audio-format
> negotiation — exactly as the [xone](https://github.com/medusalix/xone) driver does
> during headset bring-up. Without it the audio subsystem stays idle and the
> isochronous endpoint only streams zeros.

## How it works

`wolverined` **detaches the kernel `xpad` driver** and takes over all three USB
interfaces itself — it does **not** run alongside xpad. This is necessary because
the GIP control channel, the media buttons, **and** the gamepad reports all ride on
the same endpoint (EP1) that xpad monopolizes. Since detaching xpad removes the
kernel gamepad, `wolverined` re-exposes the gamepad in userspace via uinput. On
`Ctrl+C`/`SIGTERM` it reattaches xpad, so the gamepad returns without a replug.

| Interface | Alt | Endpoints | Role | Owner in `wolverined` |
|---|---|---|---|---|
| 0 | 0 | EP1 IN/OUT (Interrupt, 64B) | GIP: gamepad, media buttons, handshake, rumble | `nusb` |
| 1 | 1 | EP3 IN/OUT (Isochronous, 228B) | Audio | `libusb` |
| 2 | 1 | EP2 IN/OUT (Bulk, 64B) | Control / events | `nusb` |

**Device:** Razer Wolverine Ultimate — USB ID `1532:0a14`.

See [`CONTEXT.md`](CONTEXT.md) for the full protocol notes and design decisions,
and [`docs/usb-analysis.md`](docs/usb-analysis.md) for the descriptor analysis.

## Requirements

- Linux kernel ≥ 6.x, PipeWire ≥ 1.x (with `wpctl`)
- A Rust toolchain (`rustup`) and the PipeWire development headers:
  - **Arch / CachyOS:** `rustup pipewire base-devel`
  - **Debian / Ubuntu:** `rustup.rs` + `libpipewire-0.3-dev libclang-dev pkg-config`
  - **Fedora:** `rustup.rs` + `pipewire-devel clang pkgconf-pkg-config`
- Runs as root (detaches `xpad`, claims the USB interfaces)

## Build & run

```bash
cd rust
cargo build --release
sudo ./target/release/wolverined
```

Plug your headphones into the controller's 3.5mm jack first. The gamepad, audio
devices, media buttons and rumble come up together; select **Wolverine Headphones**
/ **Wolverine Microphone** in your audio settings (or `wpctl status`). `Ctrl+C` stops
cleanly and hands the gamepad back to `xpad`.

Audio-only smoke test (brings up just the PipeWire nodes, no USB / no root):

```bash
cargo run --release -- audio
```

> **Note (runs as root):** the driver runs under `sudo`, but the PipeWire bridge
> and the media-button volume/mute target the invoking user's PipeWire session
> (via `SUDO_UID` / `XDG_RUNTIME_DIR`), so the devices show up in your normal
> audio settings.

### Audio latency

Audio is **low-latency by default** and the depth is **bounded**: the sink and
source (PipeWire) and the isochronous USB engine run off independent clocks, so
each ring is trimmed on the consumer side (drop-oldest) to keep only the freshest
audio — without this the rings drift full and add seconds of delay. The mic also
honours PipeWire's per-quantum request (`pw_buffer.requested`) so it never
over-reads and clips speech.

The defaults are tuned for real hardware. To trade latency against underrun
robustness, set these env vars (all optional):

| Env var | Default | Effect |
|---|---|---|
| `WOLVERINE_CAP_MS` | `100` | Max mic (capture) latency, ms. Lower = tighter, more underrun risk. |
| `WOLVERINE_QUANTUM` | `512` | PipeWire node quantum (frames). Lower = less buffering. |
| `WOLVERINE_PRIME_MS` | `40` | Playback ring prime/target depth, ms. |
| `WOLVERINE_OUT_XFERS` / `WOLVERINE_OUT_PKTS` | `6` / `8` | Isochronous OUT transfers in flight (ms of runway). |

```bash
sudo WOLVERINE_CAP_MS=50 WOLVERINE_QUANTUM=256 ./target/release/wolverined
```

## Install as a service (systemd + udev)

To have the driver start automatically whenever the controller is plugged in:

```bash
sudo ./packaging/install.sh
```

This builds the release binary, installs it to `/usr/local/bin/wolverined`, and
sets up a **udev-activated** systemd service targeting your user's PipeWire
session. The service starts on connect (at boot or hotplug) and the daemon exits
by itself on disconnect.

```bash
systemctl status wolverined        # is it running?
journalctl -u wolverined -f        # live logs
systemctl stop wolverined          # stop and hand the gamepad back to xpad
```

Uninstall: remove `/usr/local/bin/wolverined`,
`/etc/systemd/system/wolverined.service` and
`/etc/udev/rules.d/99-wolverine.rules`, then `systemctl daemon-reload`.

## Layout

```
wolverine-linux/
├── rust/                  # the driver (Rust)
│   └── src/
│       ├── gip.rs         # GIP wire format (framing, varints, constants)
│       ├── usb.rs         # EP1 handshake + event loop (nusb)
│       ├── iso.rs         # EP3 isochronous audio engine (libusb)
│       ├── audio.rs       # native PipeWire sink + source (pipewire-rs)
│       ├── ring.rs        # lock-free SPSC audio rings (rtrb)
│       ├── input.rs       # uinput gamepad + media keys + rumble (evdev)
│       └── main.rs        # orchestration + clean shutdown
├── packaging/             # systemd unit + udev rule + install.sh
├── tools/                 # Python reference implementation (legacy)
├── docs/usb-analysis.md
└── CONTEXT.md             # protocol knowledge base & design decisions
```

## Contributing

Protocol reverse engineering is documented in `CONTEXT.md` and `docs/usb-analysis.md`.
The Python driver in `tools/` remains as a readable reference for the GIP protocol.
