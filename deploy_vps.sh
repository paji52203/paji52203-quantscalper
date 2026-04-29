#!/bin/bash
# QuantScalper — Deploy to VPS
# Usage: bash deploy_vps.sh
# Requires: sshpass

set -e

VPS_HOST="159.89.207.218"
VPS_USER="root"
VPS_PASS="112233qa"
VPS_DIR="/root/quantscalper"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

SSH="sshpass -p $VPS_PASS ssh -o StrictHostKeyChecking=no $VPS_USER@$VPS_HOST"
SCP="sshpass -p $VPS_PASS scp -o StrictHostKeyChecking=no -r"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  QuantScalper Deploy → $VPS_HOST"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── 1. Stop existing PM2 process (jika ada) ──────────────────────────────────
echo "[1/6] Stopping existing bot (if running)..."
$SSH "pm2 stop quantscalper 2>/dev/null || true"

# ── 2. Create dirs on VPS ────────────────────────────────────────────────────
echo "[2/6] Preparing VPS directories..."
$SSH "mkdir -p $VPS_DIR/logs"

# ── 3. Sync source files ─────────────────────────────────────────────────────
echo "[3/6] Uploading source files..."
$SCP \
    "$LOCAL_DIR/core" \
    "$LOCAL_DIR/exchange" \
    "$LOCAL_DIR/risk" \
    "$LOCAL_DIR/data" \
    "$LOCAL_DIR/utils" \
    "$LOCAL_DIR/start.py" \
    "$LOCAL_DIR/config.ini" \
    "$LOCAL_DIR/requirements.txt" \
    "$LOCAL_DIR/ecosystem.config.js" \
    "$VPS_USER@$VPS_HOST:$VPS_DIR/"

# ── 4. Setup Python venv on VPS ──────────────────────────────────────────────
echo "[4/6] Setting up Python venv and installing deps..."
$SSH "
    cd $VPS_DIR
    if [ ! -d '.venv' ]; then
        python3 -m venv .venv
        echo 'venv created'
    else
        echo 'venv exists'
    fi
    .venv/bin/pip install -r requirements.txt -q
    echo 'deps installed'
"

# ── 5. Create .env jika belum ada ────────────────────────────────────────────
echo "[5/6] Checking .env credentials..."
$SSH "
    if [ ! -f '$VPS_DIR/.env' ]; then
        echo 'WARNING: .env not found at $VPS_DIR/.env'
        echo 'Creating empty .env — fill in your credentials!'
        cat > $VPS_DIR/.env << 'EOF'
BYBIT_API_KEY=
BYBIT_API_SECRET=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
PYTHONUNBUFFERED=1
PYTHONPATH=/root/quantscalper
EOF
        echo '.env created. Fill in credentials before starting!'
    else
        echo '.env already exists — credentials preserved'
    fi
"

# ── 6. Start with PM2 ────────────────────────────────────────────────────────
echo "[6/6] Starting QuantScalper with PM2..."
$SSH "
    cd $VPS_DIR
    pm2 start ecosystem.config.js --env production
    pm2 save
    pm2 startup 2>/dev/null || true
    echo ''
    pm2 status quantscalper
"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Deploy selesai!"
echo ""
echo "  Commands di VPS:"
echo "    pm2 logs quantscalper     → lihat log live"
echo "    pm2 status                → status bot"
echo "    pm2 stop quantscalper     → stop bot"
echo "    pm2 restart quantscalper  → restart bot"
echo ""
echo "  Edit credentials:"
echo "    nano /root/quantscalper/.env"
echo "    pm2 restart quantscalper"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
