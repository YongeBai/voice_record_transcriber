#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_USER="${1:-${SUDO_USER:-${USER:-}}}"
RULE_DEST="/etc/udev/rules.d/99-actions-ami-voice-memo.rules"
SERVICE_DEST="/etc/systemd/system/actions-ami-voice-memo@.service"

if [ -z "$SERVICE_USER" ]; then
    echo "Unable to determine the Linux username for the sync service."
    echo "Run: sudo $SCRIPT_DIR/install_auto_sync.sh <username>"
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo so the udev rule and systemd service can be installed."
    echo "Example: sudo $SCRIPT_DIR/install_auto_sync.sh $SERVICE_USER"
    exit 1
fi

tmp_rule="$(mktemp)"
cleanup() {
    rm -f "$tmp_rule"
}
trap cleanup EXIT

sed \
    "s/actions-ami-voice-memo@yongebai.service/actions-ami-voice-memo@${SERVICE_USER}.service/" \
    "$SCRIPT_DIR/99-actions-ami-voice-memo.rules" >"$tmp_rule"

install -m 0644 "$tmp_rule" "$RULE_DEST"
install -m 0644 "$SCRIPT_DIR/actions-ami-voice-memo@.service" "$SERVICE_DEST"

udevadm control --reload-rules
systemctl daemon-reload
systemctl enable "actions-ami-voice-memo@${SERVICE_USER}.service"

echo
echo "Automatic sync installed for user: $SERVICE_USER"
echo "Unplug and replug the recorder to test the udev trigger."
echo "To test immediately, run:"
echo "  sudo systemctl start actions-ami-voice-memo@${SERVICE_USER}.service"
