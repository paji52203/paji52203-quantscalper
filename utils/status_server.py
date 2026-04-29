"""
StatusServer — QuantScalper
HTTP server yang serve dashboard + status JSON.
Berjalan di thread terpisah, tidak ganggu engine.
Port default: 8080
"""

import json
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from typing import Optional


# Shared state — diupdate oleh bot, dibaca oleh HTTP handler
_status: dict = {
    "updated_at": "",
    "symbol": "BTCUSDT",
    "price": 0.0,
    "phase": "STARTING",
    "direction": "NEUTRAL",
    "dry_run": True,

    # Condition lamps
    "conditions": {
        "squeeze_active": False,
        "squeeze_candles": 0,
        "volume_ok": False,
        "vol_ratio": 0.0,
        "momentum_clear": False,
        "momentum": 0.0,
        "no_exhaustion": True,
        "exhaustion_flags": [],
        "rsi_ok": False,
        "rsi": 0.0,
        "fee_gate_ok": False,
    },

    # Overall signal
    "signal": "HOLD",
    "signal_entry": 0.0,
    "signal_sl": 0.0,
    "signal_tp": 0.0,
    "signal_rr": 0.0,
    "confidence": 0,
    "reason": "Initializing...",

    # Position
    "position": None,
}
_lock = threading.Lock()


def update_status(data: dict):
    """Called by bot on each candle close / signal."""
    with _lock:
        _status.update(data)
        _status["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_status() -> dict:
    with _lock:
        return dict(_status)


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QuantScalper — Live Dashboard</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

  :root {
    --bg: #0a0a0f;
    --card: #12121a;
    --card2: #1a1a26;
    --border: #2a2a3a;
    --green: #00ff88;
    --green-dim: #00ff8833;
    --red: #ff3366;
    --red-dim: #ff336633;
    --yellow: #ffcc00;
    --yellow-dim: #ffcc0033;
    --blue: #4488ff;
    --blue-dim: #4488ff22;
    --text: #e0e0f0;
    --muted: #6666aa;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    min-height: 100vh;
    padding: 24px;
  }

  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 28px;
  }

  .title {
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.5px;
    background: linear-gradient(90deg, #4488ff, #00ff88);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }

  .subtitle { color: var(--muted); font-size: 13px; margin-top: 2px; }

  .pulse-dot {
    width: 10px; height: 10px;
    border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 8px var(--green);
    animation: pulse 2s infinite;
    display: inline-block;
    margin-right: 8px;
  }
  .pulse-dot.offline { background: var(--red); box-shadow: 0 0 8px var(--red); animation: none; }

  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.5; transform: scale(0.8); }
  }

  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    max-width: 900px;
    margin: 0 auto;
  }

  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 20px;
  }

  .card.full { grid-column: 1 / -1; }

  .card-title {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: var(--muted);
    margin-bottom: 16px;
  }

  /* Price card */
  .price {
    font-family: 'JetBrains Mono', monospace;
    font-size: 36px;
    font-weight: 500;
    color: var(--text);
    letter-spacing: -1px;
  }

  /* Phase badge */
  .phase-badge {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 8px 16px;
    border-radius: 100px;
    font-weight: 600;
    font-size: 14px;
    margin-top: 8px;
  }

  .phase-COMPRESSION { background: var(--yellow-dim); color: var(--yellow); border: 1px solid var(--yellow); }
  .phase-EXPANSION   { background: var(--green-dim);  color: var(--green);  border: 1px solid var(--green);  }
  .phase-EXHAUSTION  { background: var(--red-dim);    color: var(--red);    border: 1px solid var(--red);    }
  .phase-NEUTRAL     { background: var(--blue-dim);   color: var(--blue);   border: 1px solid var(--blue);   }
  .phase-STARTING    { background: #222;              color: var(--muted);  border: 1px solid var(--border); }

  /* Indicator lamps */
  .lamps { display: flex; flex-direction: column; gap: 10px; }

  .lamp {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 14px;
    background: var(--card2);
    border-radius: 10px;
    border: 1px solid var(--border);
    transition: all 0.3s ease;
  }

  .lamp.on  { border-color: var(--green); background: var(--green-dim); }
  .lamp.off { border-color: var(--red);   background: var(--red-dim);   }

  .lamp-dot {
    width: 14px; height: 14px;
    border-radius: 50%;
    flex-shrink: 0;
    background: #333;
    transition: all 0.3s ease;
  }
  .lamp.on  .lamp-dot { background: var(--green); box-shadow: 0 0 12px var(--green); }
  .lamp.off .lamp-dot { background: var(--red);   box-shadow: 0 0 12px var(--red);   }

  .lamp-label { font-size: 13px; font-weight: 500; flex: 1; }
  .lamp-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    color: var(--muted);
  }
  .lamp.on .lamp-value { color: var(--green); }

  /* Signal card */
  .signal-action {
    font-size: 28px;
    font-weight: 700;
    font-family: 'JetBrains Mono', monospace;
  }
  .signal-BUY  { color: var(--green); }
  .signal-SELL { color: var(--red); }
  .signal-HOLD { color: var(--muted); }
  .signal-CLOSE { color: var(--yellow); }

  .signal-grid {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 10px;
    margin-top: 14px;
  }

  .signal-item { background: var(--card2); border-radius: 8px; padding: 10px 12px; }
  .signal-item-label { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; }
  .signal-item-val {
    font-family: 'JetBrains Mono', monospace;
    font-size: 14px;
    font-weight: 500;
    margin-top: 4px;
  }

  /* Confidence bar */
  .conf-bar-wrap { margin-top: 14px; }
  .conf-label { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
  .conf-bar {
    height: 6px;
    background: var(--card2);
    border-radius: 100px;
    overflow: hidden;
  }
  .conf-fill {
    height: 100%;
    border-radius: 100px;
    background: linear-gradient(90deg, var(--blue), var(--green));
    transition: width 0.5s ease;
  }

  /* Reason */
  .reason {
    margin-top: 12px;
    font-size: 12px;
    color: var(--muted);
    font-style: italic;
    line-height: 1.5;
    word-break: break-word;
  }

  /* Position card */
  .no-position { color: var(--muted); font-size: 13px; padding: 8px 0; }

  /* Footer */
  .footer {
    text-align: center;
    color: var(--muted);
    font-size: 11px;
    margin-top: 24px;
  }

  .updated-at {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: var(--muted);
  }

  @media (max-width: 600px) {
    .grid { grid-template-columns: 1fr; }
    .card.full { grid-column: 1; }
    .signal-grid { grid-template-columns: 1fr 1fr; }
    .price { font-size: 28px; }
  }
</style>
</head>
<body>

<div class="header">
  <div>
    <div class="title">⚡ QuantScalper</div>
    <div class="subtitle">BTCUSDT · 15m · Real-time Signal Dashboard</div>
  </div>
  <div>
    <span class="pulse-dot" id="dot"></span>
    <span class="updated-at" id="updatedAt">--</span>
  </div>
</div>

<div class="grid">

  <!-- Price + Phase -->
  <div class="card">
    <div class="card-title">Market State</div>
    <div class="price" id="price">$--,---</div>
    <div id="phaseBadge" class="phase-badge phase-STARTING">⏳ Starting...</div>
    <div style="margin-top:10px; font-size:12px; color:var(--muted)">
      Direction: <span id="direction" style="color:var(--text);font-weight:600">—</span>
    </div>
  </div>

  <!-- Signal -->
  <div class="card">
    <div class="card-title">Signal</div>
    <div class="signal-action signal-HOLD" id="signalAction">HOLD</div>

    <div class="signal-grid">
      <div class="signal-item">
        <div class="signal-item-label">Entry</div>
        <div class="signal-item-val" id="sigEntry">—</div>
      </div>
      <div class="signal-item">
        <div class="signal-item-label">SL</div>
        <div class="signal-item-val" id="sigSL">—</div>
      </div>
      <div class="signal-item">
        <div class="signal-item-label">TP</div>
        <div class="signal-item-val" id="sigTP">—</div>
      </div>
    </div>

    <div class="conf-bar-wrap">
      <div class="conf-label">Confidence: <span id="confVal">0</span>%</div>
      <div class="conf-bar"><div class="conf-fill" id="confFill" style="width:0%"></div></div>
    </div>

    <div class="reason" id="reason">Waiting for first candle...</div>
  </div>

  <!-- Condition Lamps -->
  <div class="card full">
    <div class="card-title">Entry Conditions — All must be 🟢 for signal</div>
    <div class="lamps">
      <div class="lamp" id="lamp-squeeze">
        <div class="lamp-dot"></div>
        <div class="lamp-label">🔒 Squeeze / Compression Active</div>
        <div class="lamp-value" id="val-squeeze">0 candles</div>
      </div>
      <div class="lamp" id="lamp-volume">
        <div class="lamp-dot"></div>
        <div class="lamp-label">📊 Volume Confirmation</div>
        <div class="lamp-value" id="val-volume">0.00x avg</div>
      </div>
      <div class="lamp" id="lamp-momentum">
        <div class="lamp-dot"></div>
        <div class="lamp-label">💨 Momentum Direction Clear</div>
        <div class="lamp-value" id="val-momentum">0.00</div>
      </div>
      <div class="lamp" id="lamp-exhaustion">
        <div class="lamp-dot"></div>
        <div class="lamp-label">✅ No Exhaustion (Blow-off / RSI Divergence)</div>
        <div class="lamp-value" id="val-exhaustion">—</div>
      </div>
      <div class="lamp" id="lamp-rsi">
        <div class="lamp-dot"></div>
        <div class="lamp-label">📈 RSI Healthy (no divergence)</div>
        <div class="lamp-value" id="val-rsi">0.0</div>
      </div>
      <div class="lamp" id="lamp-fee">
        <div class="lamp-dot"></div>
        <div class="lamp-label">💰 Fee Gate (move &gt; 0.35%)</div>
        <div class="lamp-value" id="val-fee">—</div>
      </div>
    </div>
  </div>

</div>

<div class="footer">
  Auto-refresh every 5s · DRY RUN mode · QuantScalper v1.0
</div>

<script>
  async function refresh() {
    try {
      const r = await fetch('/status');
      const d = await r.json();
      const c = d.conditions;

      // Header
      document.getElementById('dot').className = 'pulse-dot';
      document.getElementById('updatedAt').textContent = d.updated_at;

      // Price
      document.getElementById('price').textContent =
        '$' + parseFloat(d.price).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});

      // Phase
      const pb = document.getElementById('phaseBadge');
      const phaseIcon = {
        COMPRESSION: '🔒', EXPANSION: '⚡', EXHAUSTION: '🚫', NEUTRAL: '💤', STARTING: '⏳'
      };
      pb.className = 'phase-badge phase-' + d.phase;
      pb.textContent = (phaseIcon[d.phase] || '●') + ' ' + d.phase;

      // Direction
      const dirEl = document.getElementById('direction');
      dirEl.textContent = d.direction;
      dirEl.style.color = d.direction === 'LONG' ? 'var(--green)' :
                          d.direction === 'SHORT' ? 'var(--red)' : 'var(--muted)';

      // Signal
      const sa = document.getElementById('signalAction');
      sa.textContent = d.signal;
      sa.className = 'signal-action signal-' + d.signal;

      document.getElementById('sigEntry').textContent = d.signal_entry > 0 ?
        '$' + parseFloat(d.signal_entry).toLocaleString() : '—';
      document.getElementById('sigSL').textContent = d.signal_sl > 0 ?
        '$' + parseFloat(d.signal_sl).toLocaleString() : '—';
      document.getElementById('sigTP').textContent = d.signal_tp > 0 ?
        '$' + parseFloat(d.signal_tp).toLocaleString() : '—';

      const conf = d.confidence || 0;
      document.getElementById('confVal').textContent = conf;
      document.getElementById('confFill').style.width = conf + '%';
      document.getElementById('reason').textContent = d.reason || '';

      // Lamps
      function setLamp(id, on, val) {
        const el = document.getElementById('lamp-' + id);
        el.className = 'lamp ' + (on ? 'on' : 'off');
        const valEl = document.getElementById('val-' + id);
        if (valEl) valEl.textContent = val;
      }

      setLamp('squeeze',    c.squeeze_active,   c.squeeze_candles + ' candles');
      setLamp('volume',     c.volume_ok,        parseFloat(c.vol_ratio).toFixed(2) + 'x avg');
      setLamp('momentum',   c.momentum_clear,   parseFloat(c.momentum).toFixed(2));
      setLamp('exhaustion', c.no_exhaustion,
        c.exhaustion_flags.length > 0 ? c.exhaustion_flags[0] : 'Clean ✓');
      setLamp('rsi',        c.rsi_ok,           'RSI ' + parseFloat(c.rsi).toFixed(1));
      setLamp('fee',        c.fee_gate_ok,      c.fee_gate_ok ? 'Passed ✓' : 'Insufficient move');

    } catch(e) {
      document.getElementById('dot').className = 'pulse-dot offline';
      document.getElementById('updatedAt').textContent = 'Offline';
    }
  }

  refresh();
  setInterval(refresh, 5000);
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass  # suppress access logs

    def do_GET(self):
        if self.path == '/status':
            data = json.dumps(get_status(), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data)

        elif self.path in ('/', '/dashboard'):
            html = DASHBOARD_HTML.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(html)

        else:
            self.send_response(404)
            self.end_headers()


def start_server(port: int = 8080):
    """Start HTTP status server in background thread."""
    server = HTTPServer(('0.0.0.0', port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server
