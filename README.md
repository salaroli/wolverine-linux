# wolverine-linux

Open-source Linux driver for the Razer Wolverine Ultimate controller's audio jack, microphone, and media buttons.

## Status

| Feature | Status |
|---|---|
| Gamepad (buttons, sticks, triggers) | ✅ Works (kernel xpad driver, re-exposed via uinput) |
| Headphone jack (output) | ✅ Works — exposed as PipeWire sink **Wolverine Headphones** |
| Microphone (input) | ✅ Works — exposed as PipeWire source **Wolverine Microphone** |
| Media buttons (volume / mic mute) | ✅ Works — mirrored to PipeWire (volume) and mic mute |

> **Audio breakthrough:** the 3.5mm jack is **not** an Xbox-only hardware limitation.
> Both output (DAC) and input (ADC) work on Linux. The missing piece was the GIP
> `POWER` command (`0x05`) with `GIP_PWR_ON` (`0x00`), sent after audio-format
> negotiation — exactly as the [xone](https://github.com/medusalix/xone) driver does
> during headset bring-up. Without it the audio subsystem stays idle and the
> isochronous endpoint only streams zeros.

## Device

**Razer Wolverine Ultimate** — USB ID `1532:0a14`

### USB Interface Map

| Interface | Alternate | Endpoints | Role | Driver |
|---|---|---|---|---|
| 0 | 0 | EP1 IN/OUT (Interrupt, 64B) | Gamepad HID | `xpad` (kernel) |
| 1 | 0 | — | Idle | none |
| 1 | 1 | EP3 IN/OUT (Isochronous, 228B) | **Audio** | none |
| 2 | 0 | — | Idle | none |
| 2 | 1 | EP2 IN/OUT (Bulk, 64B) | Control / events | none |

> Media buttons (volume / mic mute) do **not** ride on EP2 — they arrive as GIP
> `AUDIO_CONTROL` sub `0x00` (VOLUME_CHAT) reports on EP1, the same channel as the
> gamepad. `data[5]` = mic mute state, `data[6]` = absolute volume (0–100).

## Architecture

```
wolverine-linux/
├── tools/
│   ├── probe.py         # USB interface monitor (early passive discovery)
│   ├── probe_gip.py     # systematic GIP command-ID probe
│   ├── gip_init.py      # userspace driver: GIP init + uinput gamepad + audio + media
│   ├── wolverine_pw.c   # native PipeWire bridge (compiled to wolverine_pw.so)
│   └── Makefile         # builds wolverine_pw.so
├── docs/
│   └── usb-analysis.md
└── driver/            # kernel module (future)
```

## Requirements

- Linux kernel ≥ 6.x
- Python ≥ 3.10, `python-pyusb`, `python-evdev`
- PipeWire + `wpctl` (for audio and media-button volume/mute)
- A C toolchain and the PipeWire development headers, to build the audio bridge:
  - **Arch / CachyOS:** `base-devel` + `pipewire` (headers included)
  - **Debian / Ubuntu:** `build-essential pkg-config libpipewire-0.3-dev`
  - **Fedora:** `gcc make pkgconf-pkg-config pipewire-devel`
- Run as root (detaches `xpad` and claims the USB interfaces)

## Build

The native PipeWire bridge (`wolverine_pw.so`) must be compiled once:

```bash
make -C tools
```

This creates the virtual **Wolverine Headphones** (sink) and **Wolverine Microphone**
(source) nodes at runtime. Without it, the gamepad and media buttons still work but the
audio devices are disabled.

## Usage

```bash
# Plug your headphones into the controller's 3.5mm jack, then:
sudo python3 tools/gip_init.py
```

This detaches `xpad`, re-exposes the gamepad via uinput, brings up the headset audio, maps
the media buttons, and registers the PipeWire audio devices. Select **Wolverine Headphones**
/ **Wolverine Microphone** in your audio settings (or `wpctl status`). Ctrl+C to stop.

```bash
# Early passive USB monitor (historical)
sudo python3 tools/probe.py
```

> **Note (audio runs as root):** the driver runs under `sudo`, but the audio bridge and
> the media-button volume/mute are pointed at the invoking user's PipeWire session (via
> `SUDO_UID` / `XDG_RUNTIME_DIR`), so the devices show up in your normal audio settings.

## Contributing

Protocol reverse engineering in progress. See `docs/usb-analysis.md` for findings.
