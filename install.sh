#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is required."
    exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required."
    echo "Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

cd "$SCRIPT_DIR"
uv sync

if [ ! -f "$SCRIPT_DIR/.env" ]; then
    cp "$SCRIPT_DIR/env.example" "$SCRIPT_DIR/.env"
    echo "Created $SCRIPT_DIR/.env from env.example"
fi

chmod +x "$SCRIPT_DIR/voice_memo_sync.py"
chmod +x "$SCRIPT_DIR/install.sh"

echo
echo "Install complete."
echo
echo "Next steps:"
echo "1. Edit $SCRIPT_DIR/.env and set your Simplenote and Soniox credentials."
echo "2. Optional but recommended: seed the cache with the current recorder contents:"
echo "   uv run voice_memo_sync.py --mark-existing"
echo "3. Plug in the recorder and test manually:"
echo "   uv run voice_memo_sync.py"
echo "4. Optional automatic sync:"
echo "   sudo cp $SCRIPT_DIR/99-actions-ami-voice-memo.rules /etc/udev/rules.d/"
echo "   sudo cp $SCRIPT_DIR/actions-ami-voice-memo@.service /etc/systemd/system/"
echo "   sudo udevadm control --reload-rules"
echo "   sudo systemctl daemon-reload"
echo "   sudo systemctl enable actions-ami-voice-memo@$USER.service"
