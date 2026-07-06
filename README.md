# wolverine-linux

Open-source Linux driver for the Razer Wolverine Ultimate controller's audio jack, microphone, and media buttons.

## Status

| Feature | Status |
|---|---|
| Gamepad (buttons, sticks, triggers) | ✅ Works (kernel xpad driver, re-exposed via uinput) |
| Headphone jack (output) | ✅ Works at protocol level — raw playback confirmed audible; PipeWire/ALSA integration pending |
| Microphone (input) | ✅ Works at protocol level — raw capture confirmed flowing; PipeWire/ALSA integration pending |
| Media buttons | 🚧 Under investigation |

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
| 2 | 1 | EP2 IN/OUT (Bulk, 64B) | **Control / Media buttons** | none |

## Architecture

```
wolverine-linux/
├── tools/
│   ├── probe.py       # USB interface monitor (early passive discovery)
│   ├── probe_gip.py   # systematic GIP command-ID probe
│   └── gip_init.py    # userspace driver: GIP init + uinput gamepad + audio bring-up
├── docs/
│   └── usb-analysis.md
└── driver/            # kernel module (future)
```

## Requirements

- Linux kernel ≥ 6.x
- Python ≥ 3.10
- `python-pyusb`, `python-evdev`
- Run as root (detaches `xpad` and claims the USB interfaces)

## Usage

```bash
# Full userspace driver: detach xpad, re-expose gamepad via uinput,
# bring up audio (plug headphones into the 3.5mm jack first).
sudo python3 tools/gip_init.py

# Early passive USB monitor (historical)
sudo python3 tools/probe.py
```

## Contributing

Protocol reverse engineering in progress. See `docs/usb-analysis.md` for findings.
