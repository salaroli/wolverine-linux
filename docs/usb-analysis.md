# USB Protocol Analysis — Razer Wolverine Ultimate (1532:0a14)

## Device Descriptor

- **Speed:** Full Speed (12 Mbps)
- **Device Class:** 255 (Vendor Specific)
- **Configurations:** 1
- **Max Power:** 500mA
- **Firmware version:** 1.01

## Interface Map

### Interface 0 — Gamepad (claimed by `xpad`)

- Class: Vendor Specific (255), SubClass: 71, Protocol: 208
- EP1 OUT — Interrupt, 64 bytes, interval 4ms
- EP1 IN  — Interrupt, 64 bytes, interval 4ms
- Exposes: `Generic X-Box pad` at `/dev/input/eventX`
- Buttons mapped: A, B, X, Y, LB, RB, Back, Start, Guide, LS, RS
- Axes: ABS_X/Y (left stick), ABS_RX/RY (right stick), ABS_Z/RZ (triggers), ABS_HAT0X/Y (d-pad)

### Interface 1 — Audio

- Class: Vendor Specific (255), SubClass: 71, Protocol: 208
- **Alt 0:** no endpoints (idle/disabled state)
- **Alt 1:** active audio
  - EP3 OUT — Isochronous, 228 bytes, interval 1ms → **playback (headphone out)**
  - EP3 IN  — Isochronous, 228 bytes, interval 1ms → **capture (microphone in)**

**Audio format hypothesis:** 228 bytes @ 1ms = 228,000 bytes/sec.
Candidate: 48kHz stereo 16-bit = 192 bytes/ms audio + 36 bytes header/padding.
To be confirmed via traffic capture.

### Interface 2 — Control / Media Buttons

- Class: Vendor Specific (255), SubClass: 71, Protocol: 208
- **Alt 0:** no endpoints (idle)
- **Alt 1:** active
  - EP2 OUT — Bulk, 64 bytes → **commands to device**
  - EP2 IN  — Bulk, 64 bytes → **events from device (media buttons, headset state)**

## Known Issues

- Interfaces 1 and 2 have no driver bound on Linux.
- `xpad` claims only interface 0.
- The audio interface uses isochronous transfers with Vendor Specific class — not compatible
  with `snd_usb_audio` without a custom driver.
- A custom initialization sequence (sent via EP1 OUT or EP2 OUT) may be required to activate
  the audio path before the device starts streaming on EP3.

## Next Steps

1. Claim interfaces 1 and 2 via libusb/pyusb (detaching xpad is not needed).
2. Set alternate setting 1 on interfaces 1 and 2.
3. Monitor EP2 IN (bulk) for media button events.
4. Send candidate init sequences on EP1 OUT / EP2 OUT and observe EP3 behavior.
5. Confirm audio data format (sample rate, bit depth, framing).
