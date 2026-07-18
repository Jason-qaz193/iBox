#!/usr/bin/env bash
# Deploy iBox qq_bot to Tencent Cloud CVM (Ubuntu 22.04/24.04).
# Run on the CVM as a normal user with sudo:
#   bash scripts/deploy_tencent_cvm.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "[1/6] System packages (adb, python3-venv)…"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq adb python3 python3-pip python3-venv git curl
else
  echo "WARN: apt-get not found; install adb + python3 manually"
fi

echo "[2/6] Python venv…"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -U pip -q
pip install -r requirements.txt -q

echo "[3/6] Config templates…"
if [[ ! -f config/config.yaml ]]; then
  cp config/config.example.yaml config/config.yaml
  echo "  created config/config.yaml — edit device_host + login.c_id"
fi
if [[ ! -f config/qq_bot.yaml ]]; then
  cp config/qq_bot.example.yaml config/qq_bot.yaml
  echo "  created config/qq_bot.yaml — edit OneBot URLs + bot_qq"
fi

mkdir -p logs config

echo "[4/6] Optional: install Tailscale (recommended for phone bridge)…"
if ! command -v tailscale >/dev/null 2>&1; then
  echo "  Tailscale not installed. To install on this CVM:"
  echo "    curl -fsSL https://tailscale.com/install.sh | sh"
  echo "    sudo tailscale up"
else
  echo "  Tailscale already installed."
fi

echo "[5/6] systemd service (optional)…"
SERVICE_SRC="deploy/tencent-cvm/ibox-qqbot.service"
SERVICE_DST="/etc/systemd/system/ibox-qqbot.service"
if [[ -f "$SERVICE_SRC" ]] && [[ "${INSTALL_SYSTEMD:-0}" == "1" ]]; then
  sudo sed "s|@IBOX_ROOT@|$ROOT|g; s|@IBOX_USER@|$USER|g" "$SERVICE_SRC" | sudo tee "$SERVICE_DST" >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable ibox-qqbot
  echo "  installed $SERVICE_DST (enable with: sudo systemctl start ibox-qqbot)"
else
  echo "  skip (set INSTALL_SYSTEMD=1 to install systemd unit)"
fi

echo "[6/6] Bridge check…"
if python run.py bridge-check; then
  echo ""
  echo "OK: RPC + adb reachable. Start bot:"
  echo "  source .venv/bin/activate && python qq_bot.py"
else
  echo ""
  echo "Bridge check failed — expected before phone/Tailscale/frp is ready."
  echo "After configuring device_host in config/config.yaml, run:"
  echo "  source .venv/bin/activate && python run.py bridge-check"
fi
