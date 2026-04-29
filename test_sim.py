"""
Simulation test — no network needed.
Generates synthetic OHLCV that mimics:
  1. Normal trending market
  2. COMPRESSION phase (squeeze forming)
  3. EXPANSION phase (squeeze fires)
  4. EXHAUSTION scenario (blow-off blocked)
  5. FLIP scenario with fee check
"""
import sys
sys.path.insert(0, ".")
import numpy as np
import random

from core.indicator_engine import IndicatorEngine
from core.phase_detector import PhaseDetector, Phase, Direction
from core.signal_engine import SignalEngine
from core.position_manager import PositionManager, OpenPosition
from risk.risk_manager import RiskManager

random.seed(42)
np.random.seed(42)

# ── Build synthetic OHLCV ──────────────────────────────────────────────────────
def make_candle(base, vol_mult=1.0, bullish=True, squeeze=False):
    """Generate a synthetic OHLCV candle."""
    if squeeze:
        rng = base * 0.0008  # tight range during squeeze
    else:
        rng = base * 0.003   # normal range

    if bullish:
        o = base - rng * 0.3
        c = base + rng * 0.5
    else:
        o = base + rng * 0.3
        c = base - rng * 0.5

    h = max(o, c) + rng * 0.2
    l = min(o, c) - rng * 0.2
    v = 100 * vol_mult + random.uniform(-10, 10)
    return [0, o, h, l, c, v]

ohlcv = []
price = 94000.0

# Phase 1: 200 normal candles (baseline for indicators)
for i in range(200):
    bullish = random.random() > 0.48
    ohlcv.append(make_candle(price, vol_mult=1.0, bullish=bullish))
    price += random.uniform(-50, 60) if bullish else random.uniform(-60, 50)

# Phase 2: 8 squeeze candles (tight, low volume)
print("Building COMPRESSION phase...")
for i in range(8):
    ohlcv.append(make_candle(price, vol_mult=0.6, bullish=True, squeeze=True))
    price += random.uniform(-10, 15)  # tiny drift up (bullish bias)

# Phase 3: EXPANSION candle (high volume breakout)
print("Triggering EXPANSION...")
price += 150  # big breakout move up
ohlcv.append(make_candle(price, vol_mult=2.5, bullish=True, squeeze=False))

# ── Run engines ───────────────────────────────────────────────────────────────
cfg_ind = {
    "BB_PERIOD": "20", "BB_STD": "2.0",
    "KC_PERIOD": "20", "KC_ATR_MULT": "1.5",
    "ATR_PERIOD": "14", "RSI_PERIOD": "14",
    "RSI_DIVERGENCE_LOOKBACK": "5", "VOL_SMA_PERIOD": "20",
    "ATR_BLOW_MULT": "2.0", "VOL_CLIMAX_MULT": "3.0",
}
cfg_sig = {
    "SQUEEZE_MIN_CANDLES": "3", "VOL_RATIO_MIN": "1.3",
    "MIN_MOVE_PCT": "0.35", "SQUEEZE_STALE_LIMIT": "20",
    "RR_MIN": "1.5",
}
cfg_risk = {"LEVERAGE": "20", "RISK_PCT_PER_TRADE": "1.0"}

ind_eng   = IndicatorEngine(cfg_ind)
phase_det = PhaseDetector(cfg_sig)
sig_eng   = SignalEngine(cfg_sig)
rm        = RiskManager(cfg_risk)
pos_mgr   = PositionManager(cfg_sig, risk_manager=rm)

# Feed candles progressively to simulate prev squeeze state
print("\nFeeding candles to detector (building squeeze state)...")
for i in range(len(ohlcv) - 1):
    snap_i = ind_eng.compute(ohlcv[:i+1])
    if snap_i:
        phase_det.detect(snap_i)  # updates squeeze_candle counter

# Final snapshot (after expansion candle)
snap = ind_eng.compute(ohlcv)
phase_result = phase_det.detect(snap)
current_price = float(ohlcv[-1][4])  # close of last candle
signal = sig_eng.evaluate(phase_result, snap, current_price)

print("\n" + "="*55)
print("TEST 1: COMPRESSION → EXPANSION SIGNAL")
print("="*55)
print(f"  Current price:  ${current_price:,.2f}")
print(f"  BB:             ${snap.bb_lower:,.2f} — ${snap.bb_upper:,.2f}")
print(f"  KC:             ${snap.kc_lower:,.2f} — ${snap.kc_upper:,.2f}")
print(f"  Squeeze ON:     {snap.squeeze_on} ({snap.squeeze_candles}c)")
print(f"  Momentum:       {snap.momentum:.4f}")
print(f"  Vol ratio:      {snap.vol_ratio:.2f}x")
print(f"  RSI:            {snap.rsi:.1f}")
print()
print(f"  Phase:          {phase_result.phase.value}")
print(f"  Direction:      {phase_result.direction.value}")
print(f"  Signal:         {signal.action}")
if signal.action != "HOLD":
    print(f"  Entry:          ${signal.entry:,.2f}")
    print(f"  SL:             ${signal.sl:,.2f}")
    print(f"  TP:             ${signal.tp:,.2f}")
    print(f"  R/R:            1:{signal.rr}")
    print(f"  Confidence:     {signal.confidence}%")
print(f"  Reason:         {signal.reason}")

# ── Test 2: Exhaustion Block ──────────────────────────────────────────────────
print("\n" + "="*55)
print("TEST 2: EXHAUSTION BLOCK (blow-off candle)")
print("="*55)

ohlcv2 = ohlcv[:]
# Add a massive blow-off candle
price2 = current_price + 500
ohlcv2.append([0,
    price2 - 400, price2 + 200, price2 - 450, price2 + 100,
    1000  # climactic volume (10x average)
])

ind2 = IndicatorEngine(cfg_ind)
phase2 = PhaseDetector(cfg_sig)
for i in range(len(ohlcv2)-1):
    s = ind2.compute(ohlcv2[:i+1])
    if s: phase2.detect(s)

snap2 = ind2.compute(ohlcv2)
phase_result2 = phase2.detect(snap2)
signal2 = sig_eng.evaluate(phase_result2, snap2, float(ohlcv2[-1][4]))

print(f"  Blow-off candle: range=${snap2.candle_range:.0f} | ATR=${snap2.atr14:.0f} | ratio={snap2.candle_range/snap2.atr14:.1f}x")
print(f"  Vol ratio:       {snap2.vol_ratio:.1f}x")
print(f"  Phase:           {phase_result2.phase.value}")
print(f"  Signal:          {signal2.action}  {'✅ Blocked correctly' if signal2.action == 'HOLD' else '❌ Should be HOLD'}")
print(f"  Exhaustion flags: {phase_result2.exhaustion_flags}")

# ── Test 3: Flip Fee Logic ────────────────────────────────────────────────────
print("\n" + "="*55)
print("TEST 3: FLIP FEE CALCULATION")
print("="*55)

entry_price = current_price
notional = 1000.0  # $1000 notional

# Scenario A: Losing position
current_pnl_loss = -15.0
should_flip_a, reason_a = rm.is_flip_worth_it(
    entry=entry_price,
    new_tp=entry_price * 0.998,   # 0.2% below entry (SHORT TP)
    new_sl=entry_price * 1.003,   # 0.3% above entry (SHORT SL)
    current_pnl_usdt=current_pnl_loss,
    notional=notional,
)
print(f"\n  Scenario A — Position LOSING (PnL={current_pnl_loss:+.2f} USDT):")
print(f"  Flip decision:  {'FLIP ✅' if should_flip_a else 'BLOCK ❌'}")
print(f"  Reason:         {reason_a}")

# Scenario B: Profitable position, weak new signal
current_pnl_win = +5.0
should_flip_b, reason_b = rm.is_flip_worth_it(
    entry=entry_price,
    new_tp=entry_price * 0.9995,  # only 0.05% TP (too thin)
    new_sl=entry_price * 1.002,
    current_pnl_usdt=current_pnl_win,
    notional=notional,
)
print(f"\n  Scenario B — Position WINNING (PnL={current_pnl_win:+.2f}) + weak signal:")
print(f"  Flip decision:  {'FLIP ✅' if should_flip_b else 'BLOCK ❌  (correct — TP too thin)'}")
print(f"  Reason:         {reason_b}")

# Scenario C: Profitable position, strong new signal
should_flip_c, reason_c = rm.is_flip_worth_it(
    entry=entry_price,
    new_tp=entry_price * 0.997,   # 0.3% TP (covers flip fee)
    new_sl=entry_price * 1.003,
    current_pnl_usdt=current_pnl_win,
    notional=notional,
)
print(f"\n  Scenario C — Position WINNING (PnL={current_pnl_win:+.2f}) + strong signal:")
print(f"  Flip decision:  {'FLIP ✅' if should_flip_c else 'BLOCK ❌'}")
print(f"  Reason:         {reason_c}")

print(f"\n  Fee math:")
print(f"  Taker fee:     {rm.fee_pct*100:.4f}%")
print(f"  Flip cost:     {rm.flip_cost*100:.4f}% (2x taker)")
print(f"  Round-trip:    {rm.round_trip_cost*100:.4f}% (open+close)")

print("\n" + "="*55)
print("ALL TESTS COMPLETE ✅")
print("="*55)
