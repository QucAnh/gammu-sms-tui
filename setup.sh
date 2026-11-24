#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_USER="${SUDO_USER:-${USER:-$(id -un)}}"
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
GAMMURC="$TARGET_HOME/.gammurc"
UDEV_RULE="/etc/udev/rules.d/70-xmm7360.rules"
UDEV_CONTENT='KERNEL=="ttyXMM*", TAG+="uaccess", GROUP="uucp", MODE="0660"'
XMM_OPEN="/usr/lib/xmm7360-pci-spat/rpc/open_xdatachannel.py"

WITH_LTE=0
CHECK_ONLY=0

usage() {
  cat <<'EOF'
Usage: ./setup.sh [OPTIONS]

One-shot setup for gammu-sms-tui:
  1. Checks python3 (>=3.11) and gammu
  2. Grants uucp + uaccess permissions on /dev/ttyXMM*
  3. Writes ~/.gammurc if missing
  4. Installs `gammu-sms-tui` into ~/.local/bin

Options:
  --with-lte   Also install /usr/local/bin/lte fix for xmm7360 mobile data
  --check      Check current setup status without modifying anything
  -h, --help   Show this help
EOF
}

for arg in "$@"; do
  case "$arg" in
    --with-lte) WITH_LTE=1 ;;
    --check) CHECK_ONLY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

as_target_user() {
  if [[ "$EUID" -eq 0 && "$TARGET_USER" != "root" ]]; then
    sudo -u "$TARGET_USER" HOME="$TARGET_HOME" "$@"
  else
    "$@"
  fi
}

echo "==> Checking prerequisites"
command -v gammu >/dev/null 2>&1 || { echo "Error: gammu not found. Install it first (e.g. sudo pacman -S gammu)." >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "Error: python3 not found." >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "Error: Python >= 3.11 required." >&2
  exit 1
}

if systemctl is-active --quiet ModemManager 2>/dev/null; then
  echo "Warning: ModemManager is active and may lock /dev/ttyXMM1."
  echo "         Consider: sudo systemctl disable --now ModemManager"
fi

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  echo "==> Status check (--check)"
  echo "User:        $TARGET_USER (groups: $(id -Gn "$TARGET_USER"))"
  echo "gammurc:     $([[ -f "$GAMMURC" ]] && echo "present ($GAMMURC)" || echo "missing")"
  echo "udev rule:   $([[ -f "$UDEV_RULE" ]] && echo "present ($UDEV_RULE)" || echo "missing")"
  echo "CLI binary:  $(command -v gammu-sms-tui || echo "not in PATH")"
  echo "LTE helper:  $([[ -x /usr/local/bin/lte ]] && echo "present (/usr/local/bin/lte)" || echo "not installed")"
  exit 0
fi

echo "==> Configuring /dev/ttyXMM1 permissions for $TARGET_USER"
if ! id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx "uucp"; then
  sudo usermod -aG uucp "$TARGET_USER"
fi

if [[ ! -f "$UDEV_RULE" ]] || [[ "$(cat "$UDEV_RULE" 2>/dev/null || true)" != "$UDEV_CONTENT" ]]; then
  printf '%s\n' "$UDEV_CONTENT" | sudo tee "$UDEV_RULE" >/dev/null
  sudo udevadm control --reload-rules
  if [[ -e /dev/ttyXMM1 ]]; then
    sudo udevadm trigger /dev/ttyXMM1
  fi
fi

echo "==> Checking $GAMMURC"
if [[ ! -f "$GAMMURC" ]]; then
  as_target_user tee "$GAMMURC" >/dev/null <<'EOF'
[gammu]
device = /dev/ttyXMM1
connection = at115200
EOF
  echo "Created $GAMMURC"
else
  echo "Keeping existing $GAMMURC"
fi

echo "==> Installing gammu-sms-tui command"
if command -v uv >/dev/null 2>&1; then
  if ! as_target_user uv tool install --offline -e "$REPO_DIR" >/dev/null 2>&1; then
    as_target_user uv tool install -e "$REPO_DIR"
  fi
else
  as_target_user mkdir -p "$TARGET_HOME/.local/bin"
  as_target_user tee "$TARGET_HOME/.local/bin/gammu-sms-tui" >/dev/null <<EOF
#!/usr/bin/env bash
PYTHONPATH="$REPO_DIR/src\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m sms_tui "\$@"
EOF
  as_target_user chmod +x "$TARGET_HOME/.local/bin/gammu-sms-tui"
fi

if [[ "$WITH_LTE" -eq 1 ]]; then
  if [[ -f "$XMM_OPEN" ]]; then
    echo "==> Installing /usr/local/bin/lte"
    sudo tee /usr/local/bin/lte >/dev/null <<'EOF'
#!/bin/bash
if [[ "$1" == "down" ]]; then
  exec /usr/bin/lte down
elif [[ "$1" == "up" ]]; then
  python3 /usr/lib/xmm7360-pci-spat/rpc/open_xdatachannel.py -c /etc/xmm7360 -n
  ip link set wwan0 up
  ip route replace default dev wwan0 metric 1000
else
  echo "Usage: sudo lte up|down"
  exit 1
fi
EOF
    sudo chmod +x /usr/local/bin/lte
  else
    echo "Skipping --with-lte ($XMM_OPEN not found)."
  fi
fi

echo
echo "Setup complete! Run:"
echo "  gammu-sms-tui"
if [[ ":$PATH:" != *":$TARGET_HOME/.local/bin:"* ]]; then
  echo "(Note: add $TARGET_HOME/.local/bin to your PATH if 'gammu-sms-tui' is not found.)"
fi
