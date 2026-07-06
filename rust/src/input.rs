//! uinput layer: re-expose the gamepad and handle the media buttons.
//! Port of the uinput + forward_* logic in gip_init.py, using the `evdev` crate.
//!
//! Two virtual devices:
//!   - gamepad  : ABS + BTN, fed from GIP INPUT (0x20) reports.
//!   - keyboard : KEY_* only. MUST be separate — libinput classifies a device
//!                with ABS axes + gamepad buttons as a joystick and does NOT
//!                deliver its KEY_* events to Wayland compositors. A pure-KEY
//!                device is seen as a keyboard, so media keys reach Hyprland.
//!
//! Media buttons arrive as AUDIO_CONTROL sub 0x00 (VOLUME_CHAT) on EP1:
//!   data[5] = mic mute state (0x04 unmuted / 0x05 muted)
//!   data[6] = absolute volume (0x00..0x64 = 0..100)
//! The firmware tracks an absolute volume; a click bumps it up, hold + D-pad
//! down lowers it. We act on *changes* only (the first report is a baseline).

use anyhow::{anyhow, Result};
use evdev::{
    AbsInfo, AbsoluteAxisType, AttributeSet, BusType, EventType, InputEvent, InputId, Key,
    UinputAbsSetup,
};
use evdev::uinput::{VirtualDevice, VirtualDeviceBuilder};

use crate::gip;

/// How media buttons are surfaced.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MediaMode {
    /// Default: mirror the controller's absolute volume/mute to the system's
    /// default sink/source via `wpctl` (the knob becomes the system slider).
    Absolute,
    /// Emit relative KEY_VOLUMEUP/DOWN + KEY_MICMUTE from the keyboard device.
    Keys,
}

/// Gamepad buttons in report-bit order (mask, key). This is the STANDARD Xbox
/// One GIP layout as decoded by the kernel `xpad` driver — the source of truth,
/// since the gamepad already worked via xpad. (The old Python `btn_map` was
/// shifted by one bit, e.g. 0x08 → "A" when it is actually View/Select.)
///
/// The u16 is `data[4]` (low byte) | `data[5]` (high byte):
///   data[4]: bit2 Menu(Start), bit3 View(Select), bit4 A, bit5 B, bit6 X, bit7 Y
///   data[5]: bits0-3 dpad U/D/L/R (handled as HAT), bit4 LB, bit5 RB,
///            bit6 LS-click, bit7 RS-click
/// evdev names: A=SOUTH, B=EAST, X=WEST, Y=NORTH (Linux BTN_X=WEST, BTN_Y=NORTH).
const BTN_MAP: &[(u16, Key)] = &[
    (0x0004, Key::BTN_START),  // Menu
    (0x0008, Key::BTN_SELECT), // View
    (0x0010, Key::BTN_SOUTH),  // A
    (0x0020, Key::BTN_EAST),   // B
    (0x0040, Key::BTN_WEST),   // X
    (0x0080, Key::BTN_NORTH),  // Y
    (0x1000, Key::BTN_TL),     // LB
    (0x2000, Key::BTN_TR),     // RB
    (0x4000, Key::BTN_THUMBL), // LS click
    (0x8000, Key::BTN_THUMBR), // RS click
];

// D-pad bit masks (reported as HAT0 axes, not buttons).
const DPAD_UP: u16 = 0x0100;
const DPAD_DOWN: u16 = 0x0200;
const DPAD_LEFT: u16 = 0x0400;
const DPAD_RIGHT: u16 = 0x0800;

pub struct Uinput {
    gamepad: VirtualDevice,
    keyboard: VirtualDevice,
    mode: MediaMode,
    last_mute: Option<u8>,
    last_vol: Option<u8>,
    last_buttons: Option<u16>,
}

impl Uinput {
    /// Create both virtual devices (gamepad + media keyboard).
    pub fn create(mode: MediaMode) -> Result<Self> {
        let gamepad = build_gamepad()?;
        let keyboard = build_keyboard()?;
        log::info!("uinput devices created (gamepad + media keyboard)");
        Ok(Self {
            gamepad,
            keyboard,
            mode,
            last_mute: None,
            last_vol: None,
            last_buttons: None,
        })
    }

    /// Translate a GIP INPUT (0x20) report into gamepad ABS/BTN events, using
    /// the standard Xbox One layout (matches the kernel `xpad` driver).
    ///
    /// Payload (14 bytes, after the 4-byte GIP header = data[4..18]):
    ///   [0..2]   buttons (u16 LE)
    ///   [2..4]   LT (u16 LE, 0..1023)   [4..6]  RT
    ///   [6..8]   LX (s16 LE)  [8..10] LY (inverted)
    ///   [10..12] RX (s16 LE)  [12..14] RY (inverted)
    pub fn forward_gamepad(&mut self, data: &[u8]) -> Result<()> {
        if data.len() < 18 {
            return Ok(());
        }
        let p = &data[4..];
        let buttons = u16::from_le_bytes([p[0], p[1]]);

        // Ground-truth aid for verifying/extending the mapping (e.g. the Guide
        // button): logs the raw bitmask whenever it changes. `RUST_LOG=debug`.
        if self.last_buttons != Some(buttons) {
            log::debug!("gamepad buttons = {buttons:#06x}");
            self.last_buttons = Some(buttons);
        }

        let lt = u16::from_le_bytes([p[2], p[3]]) as i32;
        let rt = u16::from_le_bytes([p[4], p[5]]) as i32;
        let lx = i16::from_le_bytes([p[6], p[7]]) as i32;
        // Xbox reports Y up-positive; evdev wants up-negative → invert (like xpad).
        let ly = (!i16::from_le_bytes([p[8], p[9]])) as i32;
        let rx = i16::from_le_bytes([p[10], p[11]]) as i32;
        let ry = (!i16::from_le_bytes([p[12], p[13]])) as i32;

        let hat_x = if buttons & DPAD_LEFT != 0 {
            -1
        } else if buttons & DPAD_RIGHT != 0 {
            1
        } else {
            0
        };
        let hat_y = if buttons & DPAD_UP != 0 {
            -1
        } else if buttons & DPAD_DOWN != 0 {
            1
        } else {
            0
        };

        let mut events = Vec::with_capacity(BTN_MAP.len() + 8);
        for &(mask, key) in BTN_MAP {
            let pressed = i32::from(buttons & mask != 0);
            events.push(InputEvent::new(EventType::KEY, key.code(), pressed));
        }
        for (axis, val) in [
            (AbsoluteAxisType::ABS_X, lx),
            (AbsoluteAxisType::ABS_Y, ly),
            (AbsoluteAxisType::ABS_Z, lt),
            (AbsoluteAxisType::ABS_RX, rx),
            (AbsoluteAxisType::ABS_RY, ry),
            (AbsoluteAxisType::ABS_RZ, rt),
            (AbsoluteAxisType::ABS_HAT0X, hat_x),
            (AbsoluteAxisType::ABS_HAT0Y, hat_y),
        ] {
            events.push(InputEvent::new(EventType::ABSOLUTE, axis.0, val));
        }
        self.gamepad.emit(&events)?; // auto-appends SYN_REPORT
        Ok(())
    }

    /// Emit the Guide/Xbox button (GIP VIRTUAL_KEY 0x07 packet, data[4] bit0).
    pub fn forward_guide(&mut self, pressed: bool) -> Result<()> {
        self.gamepad.emit(&[InputEvent::new(
            EventType::KEY,
            Key::BTN_MODE.code(),
            i32::from(pressed),
        )])?;
        Ok(())
    }

    /// Handle an AUDIO_CONTROL sub-0x00 report (media buttons). Acts on changes
    /// only. Ported from `forward_media`.
    pub fn forward_media(&mut self, data: &[u8]) -> Result<()> {
        if data.len() < 7 {
            return Ok(());
        }
        let mute = data[5];
        let vol = data[6];
        let muted = mute == 0x05;

        if let Some(prev) = self.last_mute {
            if mute != prev {
                match self.mode {
                    MediaMode::Absolute => {
                        wpctl(&["set-mute", "@DEFAULT_AUDIO_SOURCE@", if muted { "1" } else { "0" }]);
                    }
                    MediaMode::Keys => self.tap_key(Key::KEY_MICMUTE),
                }
                log::info!("media: MIC {}", if muted { "muted" } else { "unmuted" });
            }
        }
        self.last_mute = Some(mute);

        if let Some(prev) = self.last_vol {
            if vol != prev {
                match self.mode {
                    MediaMode::Absolute => {
                        // Snap the default sink to the controller's absolute level.
                        wpctl(&["set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", &format!("{:.2}", vol as f32 / 100.0)]);
                    }
                    MediaMode::Keys => {
                        let key = if vol > prev { Key::KEY_VOLUMEUP } else { Key::KEY_VOLUMEDOWN };
                        self.tap_key(key);
                    }
                }
                log::info!("media: VOL {}→{} ({}%)", prev, vol, vol);
            }
        }
        self.last_vol = Some(vol);
        Ok(())
    }

    fn tap_key(&mut self, key: Key) {
        let _ = self
            .keyboard
            .emit(&[InputEvent::new(EventType::KEY, key.code(), 1)]);
        let _ = self
            .keyboard
            .emit(&[InputEvent::new(EventType::KEY, key.code(), 0)]);
    }
}

fn build_gamepad() -> Result<VirtualDevice> {
    let mut keys = AttributeSet::<Key>::new();
    for &(_, k) in BTN_MAP {
        keys.insert(k);
    }
    keys.insert(Key::BTN_MODE); // Guide/Xbox button (separate 0x07 packet)
    let stick = AbsInfo::new(0, -32768, 32767, 16, 128, 0);
    let trigger = AbsInfo::new(0, 0, 1023, 0, 0, 0); // Xbox One triggers are 10-bit
    let hat = AbsInfo::new(0, -1, 1, 0, 0, 0);

    let mut builder = VirtualDeviceBuilder::new()?
        .name("Razer Wolverine Ultimate")
        .input_id(InputId::new(BusType::BUS_USB, gip::VID, gip::PID, 0x0101))
        .with_keys(&keys)?;
    for (axis, info) in [
        (AbsoluteAxisType::ABS_X, stick),
        (AbsoluteAxisType::ABS_Y, stick),
        (AbsoluteAxisType::ABS_Z, trigger),
        (AbsoluteAxisType::ABS_RX, stick),
        (AbsoluteAxisType::ABS_RY, stick),
        (AbsoluteAxisType::ABS_RZ, trigger),
        (AbsoluteAxisType::ABS_HAT0X, hat),
        (AbsoluteAxisType::ABS_HAT0Y, hat),
    ] {
        builder = builder.with_absolute_axis(&UinputAbsSetup::new(axis, info))?;
    }
    builder.build().map_err(|e| anyhow!("build gamepad uinput: {e}"))
}

fn build_keyboard() -> Result<VirtualDevice> {
    let mut keys = AttributeSet::<Key>::new();
    for k in [
        Key::KEY_VOLUMEUP,
        Key::KEY_VOLUMEDOWN,
        Key::KEY_MUTE,
        Key::KEY_MICMUTE,
    ] {
        keys.insert(k);
    }
    VirtualDeviceBuilder::new()?
        .name("Razer Wolverine Ultimate Media Keys")
        .input_id(InputId::new(BusType::BUS_USB, gip::VID, gip::PID, 0x0101))
        .with_keys(&keys)?
        .build()
        .map_err(|e| anyhow!("build media keyboard uinput: {e}"))
}

/// Run `wpctl` as the invoking user — PipeWire lives in that user's session, not
/// root's. Uses SUDO_USER / SUDO_UID (set by sudo). Best-effort. Blocking, but
/// media-button events are rare so this doesn't hold up the gamepad path.
fn wpctl(args: &[&str]) {
    let euid = unsafe { libc::geteuid() };
    let mut cmd = match (euid == 0, std::env::var("SUDO_USER"), std::env::var("SUDO_UID")) {
        (true, Ok(user), Ok(uid)) => {
            let mut c = std::process::Command::new("sudo");
            c.arg("-u").arg(user).arg("env").arg(format!("XDG_RUNTIME_DIR=/run/user/{uid}")).arg("wpctl");
            c
        }
        _ => std::process::Command::new("wpctl"),
    };
    cmd.args(args);
    cmd.stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null());
    let _ = cmd.status();
}
