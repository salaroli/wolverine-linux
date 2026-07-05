# wolverine-linux

Open-source Linux driver for the Razer Wolverine Ultimate controller's audio jack, microphone, and media buttons.

## Status

| Feature | Status |
|---|---|
| Gamepad (buttons, sticks, triggers) | ✅ Works (kernel xpad driver) |
| Headphone jack (output) | 🚧 In development |
| Microphone (input) | 🚧 In development |
| Media buttons | 🚧 In development |

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
│   └── probe.py       # USB interface monitor and protocol discovery
├── docs/
│   └── usb-analysis.md
└── driver/            # kernel module (future)
```

## Requirements

- Linux kernel ≥ 6.x
- Python ≥ 3.10
- `python-pyusb`
- Run as root (or with udev rule for hidraw access)

## Usage

```bash
# Monitor USB traffic from interfaces 1 and 2
sudo python3 tools/probe.py
```

## Contributing

Protocol reverse engineering in progress. See `docs/usb-analysis.md` for findings.
