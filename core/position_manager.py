"""
PositionManager — QuantScalper
Manages open position lifecycle: trail SL, flip detection, noise filtering.
Does NOT place orders — calls order_executor for that.
"""

from dataclasses import dataclass, field
from typing import Optional
from .indicator_engine import IndicatorSnapshot
from .phase_detector import PhaseResult, Phase, Direction


@dataclass
class OpenPosition:
    side: str               # "LONG" | "SHORT"
    entry: float
    sl: float               # current (trailing) SL
    tp: float
    entry_sl: float         # original SL at entry (for reference)
    candles_held: int = 0
    max_price: float = 0.0  # highest price seen (for LONG trailing)
    min_price: float = 0.0  # lowest price seen (for SHORT trailing)


@dataclass
class PositionAction:
    action: str             # "HOLD" | "UPDATE_SL" | "CLOSE" | "FLIP"
    new_sl: Optional[float] = None
    new_side: Optional[str] = None   # for FLIP
    new_entry: Optional[float] = None
    new_tp: Optional[float] = None
    reason: str = ""


class PositionManager:
    """
    Manages an open position tick-by-tick.

    Rules:
    - TRAIL: SL moves up (LONG) or down (SHORT) using recent swing structure
    - HOLD: stay in position through noise (momentum still valid)
    - FLIP: new opposite expansion signal detected → fee-aware check before flipping
    - CLOSE: TP hit, SL hit, or momentum completely reversed
    """

    def __init__(self, config: dict, risk_manager=None):
        self.trail_method = config.get("TRAIL_METHOD", "SWING")
        self.rr_min = float(config.get("RR_MIN", 1.5))
        self.risk_manager = risk_manager  # injected for fee calculations

    def update(self,
               position: OpenPosition,
               current_price: float,
               snap: IndicatorSnapshot,
               phase_result: PhaseResult,
               recent_lows: list,
               recent_highs: list) -> PositionAction:
        """
        Called on each tick or candle close.
        Returns action to take on the position.

        recent_lows:  list of recent swing lows (sorted ascending by time)
        recent_highs: list of recent swing highs (sorted ascending by time)
        """
        position.candles_held += 1

        # ── TP Hit ──
        if position.side == "LONG" and current_price >= position.tp:
            return PositionAction(action="CLOSE", reason=f"TP hit at {position.tp:.2f}")
        if position.side == "SHORT" and current_price <= position.tp:
            return PositionAction(action="CLOSE", reason=f"TP hit at {position.tp:.2f}")

        # ── SL Hit ──
        if position.side == "LONG" and current_price <= position.sl:
            return PositionAction(action="CLOSE", reason=f"SL hit at {position.sl:.2f}")
        if position.side == "SHORT" and current_price >= position.sl:
            return PositionAction(action="CLOSE", reason=f"SL hit at {position.sl:.2f}")

        # ── FLIP Detection ──
        # Fee-aware: only flip if new direction covers flip cost (0.11%)
        # OR if current position is already in loss (stop the bleeding)
        if phase_result.phase == Phase.EXPANSION:
            if position.side == "LONG" and phase_result.direction == Direction.SHORT:
                return self._evaluate_flip(position, current_price, phase_result, recent_lows, recent_highs)
            if position.side == "SHORT" and phase_result.direction == Direction.LONG:
                return self._evaluate_flip(position, current_price, phase_result, recent_lows, recent_highs)

        # ── TRAIL SL ──
        new_sl = self._trail_sl(position, current_price, snap, recent_lows, recent_highs)
        if new_sl and new_sl != position.sl:
            return PositionAction(
                action="UPDATE_SL",
                new_sl=new_sl,
                reason=f"Trail SL moved: {position.sl:.2f} → {new_sl:.2f}"
            )

        # ── HOLD ──
        return PositionAction(
            action="HOLD",
            reason=f"Holding {position.side}. Candle {position.candles_held}. "
                   f"Price={current_price:.2f} SL={position.sl:.2f} TP={position.tp:.2f}"
        )

    def _evaluate_flip(self,
                        position: OpenPosition,
                        current_price: float,
                        phase_result: PhaseResult,
                        recent_lows: list,
                        recent_highs: list) -> PositionAction:
        """
        Fee-aware flip evaluation.
        Calculates if flipping is worth it considering:
          - Current PnL (if losing: flip immediately if new side valid)
          - New TP distance vs flip cost (0.11%)
          - Never delay when accumulating loss
        """
        new_side = "SHORT" if position.side == "LONG" else "LONG"

        # Estimate new SL/TP for the new direction
        # Use KC band as proxy (same as signal_engine)
        if new_side == "SHORT":
            # SL above KC upper, TP below at 1.5x distance
            new_sl_est = current_price * 1.003   # rough: 0.3% above (KC upper proxy)
            new_tp_est = current_price * (1 - 0.003 * 1.5)
        else:
            new_sl_est = current_price * 0.997
            new_tp_est = current_price * (1 + 0.003 * 1.5)

        # Estimate current PnL
        current_pnl = 0.0
        if self.risk_manager:
            # Notional = entry * qty (qty not stored, use leverage-based estimate)
            notional = position.entry * (1 / (position.entry / 10000)) if position.entry > 0 else 1000
            current_pnl = self.risk_manager.estimate_pnl(
                position.side, position.entry, current_price, notional
            )
            should_flip, fee_reason = self.risk_manager.is_flip_worth_it(
                entry=current_price,
                new_tp=new_tp_est,
                new_sl=new_sl_est,
                current_pnl_usdt=current_pnl,
                notional=notional,
            )
        else:
            # No risk manager injected: always flip on expansion (old behavior)
            should_flip = True
            fee_reason = f"New {new_side} expansion after {position.candles_held}c."

        if should_flip:
            return PositionAction(
                action="FLIP",
                new_side=new_side,
                reason=fee_reason,
            )
        else:
            # Fee check failed: just CLOSE, don't reopen
            return PositionAction(
                action="CLOSE",
                reason=f"Flip blocked by fee check. {fee_reason}",
            )

    def _trail_sl(self,
                  position: OpenPosition,
                  current_price: float,
                  snap: IndicatorSnapshot,
                  recent_lows: list,
                  recent_highs: list) -> Optional[float]:
        """
        Calculate new trailing SL.
        Only moves SL in favorable direction (never against position).
        Uses swing structure (recent_lows/highs) or KC band as floor/ceiling.
        """
        if position.side == "LONG":
            # Track highest price seen
            position.max_price = max(position.max_price, current_price)

            # SL candidates: latest swing low below current price
            if recent_lows:
                swing_low = max(l for l in recent_lows if l < current_price)
                # Move SL up only if swing_low is higher than current SL
                if swing_low > position.sl:
                    return round(swing_low, 2)

            # Fallback: KC lower as trailing floor
            if snap.kc_lower > position.sl:
                return round(snap.kc_lower, 2)

        else:  # SHORT
            # Track lowest price seen
            position.min_price = min(position.min_price, current_price)

            # SL candidates: latest swing high above current price
            if recent_highs:
                swing_high = min(h for h in recent_highs if h > current_price)
                # Move SL down only if swing_high is lower than current SL
                if swing_high < position.sl:
                    return round(swing_high, 2)

            # Fallback: KC upper as trailing ceiling
            if snap.kc_upper < position.sl:
                return round(snap.kc_upper, 2)

        return None  # No change
