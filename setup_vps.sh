#!/bin/bash
# QuantScalper — VPS Setup via GitHub
# Jalankan ini di console VPS:
#   curl -sSL https://raw.githubusercontent.com/paji52203/quantscalper/main/setup_vps.sh | bash

set -e

REPO="https://github.com/paji52203/paji52203-quantscalper.git"
VPS_DIR="/root/quantscalper"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  QuantScalper — VPS Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── 1. Dependencies ───────────────────────────────────────────────────────────
echo "[1/5] Installing system deps..."
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip git curl 2>/dev/null

# Install PM2 if not present
if ! command -v pm2 &>/dev/null; then
    echo "Installing Node.js + PM2..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - 2>/dev/null
    apt-get install -y -qq nodejs
    npm install -g pm2 --silent
    echo "PM2 installed: $(pm2 --version)"
else
    echo "PM2 already installed: $(pm2 --version)"
fi

# ── 2. Clone / Pull repo ──────────────────────────────────────────────────────
echo "[2/5] Fetching code from GitHub..."
if [ -d "$VPS_DIR/.git" ]; then
    echo "Repo exists — pulling latest..."
    cd "$VPS_DIR"
    git pull origin main
else
    echo "Cloning repo..."
    git clone "$REPO" "$VPS_DIR"
    cd "$VPS_DIR"
fi

# ── 3. Python venv ────────────────────────────────────────────────────────────
echo "[3/5] Setting up Python venv..."
if [ ! -d "$VPS_DIR/.venv" ]; then
    python3 -m venv "$VPS_DIR/.venv"
fi
"$VPS_DIR/.venv/bin/pip" install -r "$VPS_DIR/requirements.txt" -q
echo "Python deps installed"

# ── 4. Create .env if not exists ─────────────────────────────────────────────
echo "[4/5] Setting up .env..."
mkdir -p "$VPS_DIR/logs"
if [ ! -f "$VPS_DIR/.env" ]; then
    cat > "$VPS_DIR/.env" << 'EOF'
# QuantScalper Credentials — ISI SEBELUM START BOT
BYBIT_API_KEY=
BYBIT_API_SECRET=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
PYTHONUNBUFFERED=1
PYTHONPATH=/root/quantscalper
EOF
    echo ""
    echo "  ⚠️  .env dibuat. WAJIB isi credentials sebelum start:"
    echo "  nano /root/quantscalper/.env"
    echo ""
else
    echo ".env sudah ada — credentials dipertahankan"
fi

# ── 5. PM2 setup ─────────────────────────────────────────────────────────────
echo "[5/5] Configuring PM2..."
cd "$VPS_DIR"
pm2 stop quantscalper 2>/dev/null || true
pm2 start ecosystem.config.js
pm2 save
pm2 startup systemd -u root --hp /root 2>/dev/null | tail -1 | bash 2>/dev/null || true

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Setup selesai!"
echo ""
echo "  Isi credentials dulu:"
echo "    nano /root/quantscalper/.env"
echo "    pm2 restart quantscalper"
echo ""
echo "  Monitor:"
echo "    pm2 logs quantscalper --lines 50"
echo "    pm2 status"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
