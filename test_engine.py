"""
Quick test: fetch real OHLCV from Bybit, run IndicatorEngine → PhaseDetector → SignalEngine
No API key needed (public endpoint).
"""
import sys
sys.path.insert(0, ".")

from data.market_data import MarketData
from core.indicator_engine import IndicatorEngine
from core.phase_detector import PhaseDetector
from core.signal_engine import SignalEngine
from risk.risk_manager import RiskManager

# ── Fetch real data ──
print("Fetching BTCUSDT 15m data from Bybit...")
md = MarketData("BTCUSDT", "15")
ohlcv = md.fetch_ohlcv(300)
if not ohlcv:
    print("ERROR: Failed to fetch data")
    sys.exit(1)

price = md.get_current_price()
print(f"OK: {len(ohlcv)} candles fetched | Current price: ${price:,.2f}")
print(f"    First candle: {ohlcv[0]}")
print(f"    Last  candle: {ohlcv[-1]}")
print()

# ── Run IndicatorEngine ──
cfg_ind = {
    "BB_PERIOD": "20", "BB_STD": "2.0",
    "KC_PERIOD": "20", "KC_ATR_MULT": "1.5",
    "ATR_PERIOD": "14", "RSI_PERIOD": "14",
    "RSI_DIVERGENCE_LOOKBACK": "5", "VOL_SMA_PERIOD": "20",
    "ATR_BLOW_MULT": "2.0", "VOL_CLIMAX_MULT": "3.0",
}
eng = IndicatorEngine(cfg_ind)
snap = eng.compute(ohlcv)
if not snap:
    print("ERROR: Not enough data for indicators")
    sys.exit(1)

print("=" * 50)
print("INDICATOR SNAPSHOT")
print("=" * 50)
print(f"  Close:      ${snap.close:,.2f}")
print(f"  BB:         ${snap.bb_lower:,.2f} — ${snap.bb_upper:,.2f}")
print(f"  KC:         ${snap.kc_lower:,.2f} — ${snap.kc_upper:,.2f}")
print(f"  ATR:        ${snap.atr14:.2f}")
print(f"  Squeeze:    {'ON 🔴' if snap.squeeze_on else 'OFF ✅'} ({snap.squeeze_candles} candles)")
print(f"  Momentum:   {snap.momentum:.4f} (prev: {snap.momentum_prev:.4f})")
print(f"  RSI:        {snap.rsi:.2f}")
print(f"  Vol ratio:  {snap.vol_ratio:.2f}x")
print(f"  Blow-off:   {snap.is_blowoff}")
print(f"  Climactic:  {snap.is_climactic_volume}")
print(f"  RSI Bear Div: {snap.rsi_bearish_divergence}")
print(f"  RSI Bull Div: {snap.rsi_bullish_divergence}")
print()

# ── Run PhaseDetector ──
cfg_sig = {
    "SQUEEZE_MIN_CANDLES": "3", "VOL_RATIO_MIN": "1.3",
    "MIN_MOVE_PCT": "0.35", "SQUEEZE_STALE_LIMIT": "20",
}
phase_det = PhaseDetector(cfg_sig)
phase_result = phase_det.detect(snap)

print("=" * 50)
print("PHASE DETECTION")
print("=" * 50)
print(f"  Phase:      {phase_result.phase.value}")
print(f"  Direction:  {phase_result.direction.value}")
print(f"  Reason:     {phase_result.reason}")
if phase_result.exhaustion_flags:
    print(f"  Exhaustion flags: {phase_result.exhaustion_flags}")
print()

# ── Run SignalEngine ──
cfg_risk = {"RR_MIN": "1.5", "MIN_MOVE_PCT": "0.35"}
sig_eng = SignalEngine(cfg_risk)
signal = sig_eng.evaluate(phase_result, snap, price)

print("=" * 50)
print("SIGNAL OUTPUT")
print("=" * 50)
print(f"  Action:     {signal.action}")
print(f"  Entry:      ${signal.entry:,.2f}")
if signal.action != "HOLD":
    print(f"  SL:         ${signal.sl:,.2f}")
    print(f"  TP:         ${signal.tp:,.2f}")
    print(f"  R/R:        1:{signal.rr}")
    print(f"  Confidence: {signal.confidence}%")
print(f"  Reason:     {signal.reason}")
print()

# ── Fee check ──
rm = RiskManager({"LEVERAGE": "20", "RISK_PCT_PER_TRADE": "1.0"})
print("=" * 50)
print("FEE MATH")
print("=" * 50)
print(f"  Taker fee:       {rm.fee_pct*100:.4f}%")
print(f"  Round-trip cost: {rm.round_trip_cost*100:.4f}%")
print(f"  Flip cost:       {rm.flip_cost*100:.4f}%")
if signal.action != "HOLD":
    qty = rm.calculate_qty(1000.0, signal.entry, signal.sl)
    print(f"  Qty (on $1000):  {qty:.4f} BTC")
print()
print("Test complete ✅")
