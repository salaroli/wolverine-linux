#!/usr/bin/env bash
# Install the Wolverine driver as a udev-activated systemd service.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo ./packaging/install.sh" >&2
    exit 1
fi

# Target the desktop user (the sudo invoker), so the driver connects to their
# PipeWire session and cargo builds with their toolchain — not root's.
USER_NAME="${SUDO_USER:-$(logname 2>/dev/null || true)}"
if [[ -z "${USER_NAME}" || "${USER_NAME}" == "root" ]]; then
    echo "Could not determine the desktop user. Run via: sudo ./packaging/install.sh" >&2
    exit 1
fi
USER_UID="$(id -u "${USER_NAME}")"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo ">> Target user: ${USER_NAME} (uid ${USER_UID})"

echo ">> Building release binary (as ${USER_NAME})…"
sudo -u "${USER_NAME}" bash -lc "cd '${REPO_DIR}/rust' && cargo build --release"

echo ">> Installing /usr/local/bin/wolverined"
install -Dm755 "${REPO_DIR}/rust/target/release/wolverined" /usr/local/bin/wolverined

echo ">> Installing /etc/systemd/system/wolverined.service"
sed "s/__UID__/${USER_UID}/g" "${REPO_DIR}/packaging/wolverined.service" \
    > /etc/systemd/system/wolverined.service

echo ">> Installing /etc/udev/rules.d/99-wolverine.rules"
install -Dm644 "${REPO_DIR}/packaging/99-wolverine.rules" /etc/udev/rules.d/99-wolverine.rules

echo ">> Reloading systemd + udev"
systemctl daemon-reload
udevadm control --reload
# Re-fire 'add' for an already-connected controller so it starts now.
udevadm trigger --action=add --subsystem-match=usb \
    --attr-match=idVendor=1532 --attr-match=idProduct=0a14 || true

cat <<EOF

Installed. The driver starts automatically when the controller is plugged in.
  status:    systemctl status wolverined
  logs:      journalctl -u wolverined -f
  stop:      systemctl stop wolverined      (hands the gamepad back to xpad)
  uninstall: rm /usr/local/bin/wolverined \\
                /etc/systemd/system/wolverined.service \\
                /etc/udev/rules.d/99-wolverine.rules && systemctl daemon-reload
EOF
