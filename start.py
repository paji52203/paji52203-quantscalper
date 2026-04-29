"""
QuantScalper — Main Entry Point
Event-driven real-time scalping engine.

Flow:
  1. Init: fetch OHLCV history, compute indicators
  2. WebSocket: on price tick → check real-time breakout entry
  3. WebSocket: on candle close → recompute indicators + phase + signal
  4. If position open: check CLOSE/TRAIL/FLIP on every tick
"""

import os
import time
import logging
import configparser
from typing import Optional

from data.market_data import MarketData
from exchange.bybit_connector import BybitConnector
from risk.risk_manager import RiskManager
from core.indicator_engine import IndicatorEngine, IndicatorSnapshot
from core.phase_detector import PhaseDetector, Phase, Direction
from core.signal_engine import SignalEngine
from core.position_manager import PositionManager, OpenPosition
from utils.telegram import TelegramNotifier


# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("quantscalper.log"),
    ]
)
logger = logging.getLogger("QuantScalper")


# ── Config ────────────────────────────────────────────────────────────────────
cfg = configparser.ConfigParser()
cfg.read("config.ini")

SYMBOL    = cfg["EXCHANGE"]["SYMBOL"]
TIMEFRAME = cfg["EXCHANGE"]["TIMEFRAME"]
LEVERAGE  = int(cfg["EXCHANGE"]["LEVERAGE"])
DRY_RUN   = cfg["DRY_RUN"].getboolean("ENABLED", True)

# Telegram config
TG_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN",  cfg["TELEGRAM"].get("BOT_TOKEN", ""))
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID",    cfg["TELEGRAM"].get("CHAT_ID", ""))
TG_NOTIFY  = cfg["TELEGRAM"].getboolean("ENABLED", True)

API_KEY    = os.environ.get("BYBIT_API_KEY", "")
API_SECRET = os.environ.get("BYBIT_API_SECRET", "")

indicator_cfg = dict(cfg["INDICATOR"]) | dict(cfg["EXHAUSTION"])
signal_cfg    = dict(cfg["SIGNAL"])
risk_cfg      = dict(cfg["RISK"]) | dict(cfg["EXCHANGE"])


# ── Engine Instances ──────────────────────────────────────────────────────────
market_data    = MarketData(SYMBOL, TIMEFRAME, logger)
connector      = BybitConnector(API_KEY, API_SECRET, SYMBOL, TIMEFRAME, logger=logger)
risk_manager   = RiskManager(risk_cfg, logger)
indicator_eng  = IndicatorEngine(indicator_cfg)
phase_detector = PhaseDetector(signal_cfg)
signal_eng     = SignalEngine(signal_cfg)
position_mgr   = PositionManager(risk_cfg)
telegram       = TelegramNotifier(
    token=TG_TOKEN, chat_id=TG_CHAT_ID,
    symbol=SYMBOL, dry_run=DRY_RUN, logger=logger
) if TG_NOTIFY else None


# ── State ─────────────────────────────────────────────────────────────────────
ohlcv_cache: list = []          # Rolling OHLCV buffer
latest_snap: Optional[IndicatorSnapshot] = None
open_pos: Optional[OpenPosition] = None
recent_lows: list = []
recent_highs: list = []

OHLCV_LIMIT = 300   # Candles to keep in buffer


# ── Helper ────────────────────────────────────────────────────────────────────
def _log_signal(signal, phase_result=None):
    if signal.action == "HOLD":
        return  # Don't spam logs for HOLDs
    logger.info(
        "SIGNAL %s | Entry=%.2f SL=%.2f TP=%.2f RR=%.2f | Conf=%d | %s",
        signal.action, signal.entry, signal.sl, signal.tp,
        signal.rr, signal.confidence, signal.reason
    )


# ── Event: Real-time price tick ───────────────────────────────────────────────
def on_price_tick(price: float):
    """
    Called on every WebSocket ticker update.
    Checks real-time breakout entry (Y option: enter during candle, not at close).
    Also manages open position trail/close.
    """
    global open_pos, latest_snap

    if latest_snap is None:
        return

    # ── Manage open position ──
    if open_pos:
        action = position_mgr.update(
            position=open_pos,
            current_price=price,
            snap=latest_snap,
            phase_result=phase_detector.detect(latest_snap),
            recent_lows=recent_lows,
            recent_highs=recent_highs,
        )
        _handle_position_action(action, price)
        return

    # ── Real-time breakout entry (entry during candle on KC break) ──
    snap = latest_snap
    phase_result = phase_detector.detect(snap)

    # Only attempt real-time entry if phase was COMPRESSION last candle
    # and price is now breaking KC band
    if phase_result.phase == Phase.COMPRESSION:
        long_breakout  = price > snap.kc_upper
        short_breakout = price < snap.kc_lower

        if long_breakout or short_breakout:
            # Re-evaluate signal with live price
            signal = signal_eng.evaluate(phase_result, snap, price)
            if signal.action in ("BUY", "SELL"):
                _execute_entry(signal, price)


# ── Event: Candle close ───────────────────────────────────────────────────────
def on_candle_close(candle: dict):
    """
    Called on every 15m candle CLOSE.
    Recomputes all indicators and generates signal for next entry window.
    """
    global ohlcv_cache, latest_snap, recent_lows, recent_highs

    # Append closed candle to buffer
    ohlcv_cache.append([
        candle["ts"], candle["open"], candle["high"],
        candle["low"], candle["close"], candle["volume"]
    ])
    # Keep buffer rolling
    if len(ohlcv_cache) > OHLCV_LIMIT:
        ohlcv_cache = ohlcv_cache[-OHLCV_LIMIT:]

    # Update swing structure
    if len(ohlcv_cache) >= 5:
        recent_lows  = sorted([float(c[3]) for c in ohlcv_cache[-10:]])
        recent_highs = sorted([float(c[2]) for c in ohlcv_cache[-10:]])

    # Compute indicators
    snap = indicator_eng.compute(ohlcv_cache)
    if snap is None:
        logger.warning("Not enough candles for indicator computation.")
        return
    latest_snap = snap

    # Detect phase
    phase_result = phase_detector.detect(snap)
    logger.info(
        "CANDLE CLOSE | Phase=%s Dir=%s | Squeeze=%dc | Mom=%.2f | Vol=%.2fx | RSI=%.1f",
        phase_result.phase.value, phase_result.direction.value,
        phase_result.squeeze_candles, snap.momentum,
        snap.vol_ratio, snap.rsi
    )

    # Check CLOSE if position open
    if open_pos:
        signal = signal_eng.evaluate(
            phase_result, snap,
            open_position={"side": open_pos.side}
        )
        if signal.action == "CLOSE":
            _execute_close(signal.close_reason)
        return

    # Check EXPANSION for new entry (candle-close confirmation entry)
    if phase_result.phase == Phase.EXPANSION:
        signal = signal_eng.evaluate(phase_result, snap)
        _log_signal(signal, phase_result)
        if signal.action in ("BUY", "SELL"):
            _execute_entry(signal, snap.close)


# ── Execution ─────────────────────────────────────────────────────────────────
def _execute_entry(signal, current_price: float):
    global open_pos

    if open_pos:
        logger.info("Already in position. Skipping entry.")
        return

    balance = connector.get_balance() if not DRY_RUN else 1000.0
    qty = risk_manager.calculate_qty(balance, current_price, signal.sl)
    if qty <= 0:
        logger.warning("Qty=0. Skipping entry.")
        return

    bybit_side = "Buy" if signal.action == "BUY" else "Sell"
    result = connector.place_order(
        side=bybit_side, qty=qty,
        sl=signal.sl, tp=signal.tp,
        dry_run=DRY_RUN,
    )

    if result:
        side = "LONG" if signal.action == "BUY" else "SHORT"
        open_pos = OpenPosition(
            side=side,
            entry=current_price,
            sl=signal.sl,
            tp=signal.tp,
            entry_sl=signal.sl,
            max_price=current_price,
            min_price=current_price,
        )
        logger.info(
            "ENTERED %s | Entry=%.2f SL=%.2f TP=%.2f | Qty=%.4f | %s",
            side, current_price, signal.sl, signal.tp,
            qty, "DRY-RUN" if DRY_RUN else "LIVE"
        )
        if telegram:
            telegram.entry(
                side=side, entry=current_price,
                sl=signal.sl, tp=signal.tp,
                rr=signal.rr, confidence=signal.confidence,
                reason=signal.reason, qty=qty,
            )


def _handle_position_action(action, price: float):
    global open_pos

    if action.action == "CLOSE":
        _execute_close(action.reason)

    elif action.action == "UPDATE_SL" and action.new_sl:
        old_sl = open_pos.sl
        connector.modify_sl(action.new_sl, dry_run=DRY_RUN)
        open_pos.sl = action.new_sl
        logger.info("TRAIL SL → %.2f | %s", action.new_sl, action.reason)
        if telegram:
            telegram.trail_sl(
                side=open_pos.side, old_sl=old_sl,
                new_sl=action.new_sl, current_price=price
            )

    elif action.action == "FLIP":
        logger.info("FLIP detected: %s", action.reason)
        old_side = open_pos.side if open_pos else "?"
        _execute_close(f"Flip: {action.reason}")
        if telegram:
            new_side = "SHORT" if old_side == "LONG" else "LONG"
            telegram.flip(from_side=old_side, to_side=new_side,
                          price=price, reason=action.reason)


def _execute_close(reason: str):
    global open_pos
    if not open_pos:
        return

    result = connector.close_position(
        side=open_pos.side,
        qty=0.0,
        dry_run=DRY_RUN,
    )
    if telegram:
        telegram.close(
            side=open_pos.side,
            entry=open_pos.entry,
            close_price=connector.latest_price or open_pos.entry,
            reason=reason,
        )
    logger.info("CLOSED %s | Reason: %s | %s",
                open_pos.side, reason,
                "DRY-RUN" if DRY_RUN else "LIVE")
    open_pos = None


# ── Bootstrap ─────────────────────────────────────────────────────────────────
def main():
    global ohlcv_cache, latest_snap

    logger.info("QuantScalper starting | %s %sm | DRY_RUN=%s", SYMBOL, TIMEFRAME, DRY_RUN)

    # Telegram startup notification
    if telegram:
        telegram.startup()

    # Set leverage
    if not DRY_RUN:
        connector.set_leverage(LEVERAGE)

    # Fetch initial OHLCV history
    logger.info("Fetching initial OHLCV (%d candles)...", OHLCV_LIMIT)
    ohlcv_cache = market_data.fetch_ohlcv(OHLCV_LIMIT)
    if not ohlcv_cache:
        logger.error("Failed to fetch initial OHLCV. Exiting.")
        return

    # Initial indicator snapshot
    latest_snap = indicator_eng.compute(ohlcv_cache)
    if latest_snap:
        logger.info(
            "Initial state | Squeeze=%s/%dc | Mom=%.2f | RSI=%.1f",
            latest_snap.squeeze_on, latest_snap.squeeze_candles,
            latest_snap.momentum, latest_snap.rsi
        )

    # Register WebSocket callbacks
    connector.on_price_tick(on_price_tick)
    connector.on_candle_close(on_candle_close)

    # Start WebSocket
    connector.start()

    logger.info("Engine live. Listening for signals...")

    # Keep alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutdown signal received.")
        connector.stop()
        if telegram:
            telegram.stopped()
        logger.info("QuantScalper stopped.")


if __name__ == "__main__":
    main()
