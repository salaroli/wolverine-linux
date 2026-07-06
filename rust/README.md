# wolverined — Rust rewrite (WIP skeleton)

Native Rust port of the Python driver in [`../tools`](../tools), targeting a
single static binary to ship as a systemd daemon. See [`../CONTEXT.md`](../CONTEXT.md)
for the full protocol knowledge base — this crate is the *structure*; the
hard-won protocol details live there.

## Why Rust (the concrete wins)

- **Deletes the C shim + ctypes.** `pipewire-rs`/`libspa` build SPA audio
  formats natively — the exact thing that forced `tools/wolverine_pw.c`, because
  Python's ctypes couldn't reach the `static inline` `spa_format_audio_raw_build`.
  Three languages (Python + C + ctypes glue) collapse into one.
- **Single static binary → trivial systemd daemon.** No interpreter, no `make`
  of a `.so`, no runtime dependency on system `python-libusb1`/`pyusb`/`evdev`.
- **RT-audio safety.** No GIL/GC on the 1ms iso path; lock-free SPSC rings
  (`rtrb`) replace the C mutex + drop-on-overflow rings.
- **Bounds-checking kills the byte-alignment bug class** (the left-channel buzz
  was a half-frame misalignment) — GIP framing, chunk offsets and LEB128 varints
  are exactly where Rust's slices pay off.

## Layout

| File | Role | Ports from |
|---|---|---|
| `src/gip.rs`   | GIP wire format: header build/parse, varints, constants, seq | protocol in `CONTEXT.md` |
| `src/usb.rs`   | Device open, detach xpad, claim ifaces 0/2, EP1 handshake | `gip_init.py` |
| `src/iso.rs`   | Async isochronous EP3 engine (OUT GIP-framed + IN parse) | `iso_audio.py` |
| `src/audio.rs` | PipeWire sink + source, rings | `wolverine_pw.c` |
| `src/input.rs` | uinput gamepad + keyboard, media buttons | `gip_init.py` |
| `src/main.rs`  | Orchestration (the numbered bring-up sequence) | `gip_init.py` |

## Status

**Skeleton.** `src/gip.rs` has real logic (framing/varints/seq) with unit tests;
every other module is a stub with signatures, inline protocol notes, and `TODO`s.
Nothing talks to hardware yet.

## Build

Requires the Rust toolchain (not yet installed on this machine):

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh   # install rustup
cargo build --release        # from this rust/ dir
cargo test                   # runs the gip.rs unit tests
```

Runtime deps (already present here): PipeWire ≥ 1.x + headers, libspa. Runs as
root (detaches xpad, claims USB interfaces); the PipeWire bridge and media
buttons target the invoking user's session via `SUDO_UID` / `XDG_RUNTIME_DIR`.

## Suggested porting order (bottom-up)

1. `usb.rs` — open + detach + claim + the IDENTIFY/FORMAT/**POWER ON** handshake.
   Getting `POWER ON` right is what makes audio work at all.
2. `iso.rs` — async EP3 with `nusb`; OUT GIP-framed, IN parsed. Prove clean audio.
3. `audio.rs` — `pipewire-rs` sink+source, wire the `rtrb` rings to `iso.rs`.
4. `input.rs` — uinput gamepad + media buttons.
5. `main.rs` — already wires the sequence; flip stubs to real as they land.
