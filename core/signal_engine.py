"""
SignalEngine — QuantScalper
Pure rule-based decision: BUY / SELL / HOLD / CLOSE
Outputs entry price, SL, TP — no LLM, no AI.

Signal types:
  BUY   — open LONG position
  SELL  — open SHORT position
  HOLD  — no action (no open position)
  CLOSE — exit current open position immediately
"""

from dataclasses import dataclass
from typing import Optional
from .phase_detector import Phase, Direction, PhaseResult
from .indicator_engine import IndicatorSnapshot


@dataclass
class TradeSignal:
    action: str          # "BUY" | "SELL" | "HOLD" | "CLOSE"
    entry: float
    sl: float
    tp: float
    rr: float            # actual R/R ratio
    confidence: int      # 0-100 based on conditions met
    reason: str
    phase: str
    direction: str
    close_reason: str = ""  # populated when action == CLOSE


class SignalEngine:
    """
    Converts PhaseResult + IndicatorSnapshot into executable TradeSignal.

    Entry logic:
      - Only act on EXPANSION phase
      - SL from KC band at time of entry
      - TP = entry ± (SL_dist * RR_MIN)
      - Fee gate: minimum move must cover Bybit fees

    HOLD conditions:
      - Phase != EXPANSION
      - Direction == NEUTRAL
      - Fee gate fails
      - RR < minimum
    """

    def __init__(self, config: dict):
        self.rr_min = float(config.get("RR_MIN", 1.5))
        self.min_move_pct = float(config.get("MIN_MOVE_PCT", 0.35)) / 100.0

    def evaluate(self,
                 phase_result: PhaseResult,
                 snap: IndicatorSnapshot,
                 current_price: Optional[float] = None,
                 open_position: Optional[dict] = None) -> TradeSignal:
        """
        Generate trade signal.
        current_price: live tick price (for real-time entry during candle).
                       If None, uses snap.close.
        open_position: dict with keys 'side', 'entry', 'sl', 'tp'
                       If provided, evaluate CLOSE conditions first.
        """
        price = current_price if current_price is not None else snap.close

        # ── CLOSE evaluation (if position is open) ──
        if open_position:
            close_signal = self._check_close(open_position, price, snap, phase_result)
            if close_signal:
                return close_signal

        # Only act on EXPANSION for new entries
        if phase_result.phase != Phase.EXPANSION:
            return self._hold(price, phase_result.reason, phase_result)

        # Direction must be clear
        if phase_result.direction == Direction.NEUTRAL:
            return self._hold(price, "Momentum direction unclear at expansion.", phase_result)

        # Calculate SL/TP based on KC bands
        if phase_result.direction == Direction.LONG:
            sl = snap.kc_lower          # SL: below KC lower (structure)
            sl_dist = price - sl
            tp = price + self.rr_min * sl_dist
            action = "BUY"
        else:  # SHORT
            sl = snap.kc_upper          # SL: above KC upper (structure)
            sl_dist = sl - price
            tp = price - self.rr_min * sl_dist
            action = "SELL"

        # Validate SL is on correct side
        if action == "BUY" and sl >= price:
            return self._hold(price, f"SL inversion: SL {sl:.2f} >= entry {price:.2f}", phase_result)
        if action == "SELL" and sl <= price:
            return self._hold(price, f"SL inversion: SL {sl:.2f} <= entry {price:.2f}", phase_result)

        if sl_dist <= 0:
            return self._hold(price, "SL distance zero or negative.", phase_result)

        # Fee gate: TP profit must cover fees
        expected_move_pct = abs(tp - price) / price
        if expected_move_pct < self.min_move_pct:
            return self._hold(
                price,
                f"Fee gate: expected move {expected_move_pct*100:.3f}% < {self.min_move_pct*100:.2f}%",
                phase_result
            )

        # RR calculation
        tp_dist = abs(tp - price)
        rr = round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0.0

        if rr < self.rr_min:
            return self._hold(price, f"RR {rr} < minimum {self.rr_min}", phase_result)

        # Confidence scoring
        confidence = self._score_confidence(snap, phase_result)

        reason = (
            f"EXPANSION after {phase_result.squeeze_candles}c squeeze. "
            f"Momentum={snap.momentum:.2f} Vol={snap.vol_ratio:.2f}x "
            f"RSI={snap.rsi:.1f} RR={rr}"
        )

        return TradeSignal(
            action=action,
            entry=round(price, 2),
            sl=round(sl, 2),
            tp=round(tp, 2),
            rr=rr,
            confidence=confidence,
            reason=reason,
            phase=phase_result.phase.value,
            direction=phase_result.direction.value,
        )

    def _hold(self, price: float, reason: str, phase_result: PhaseResult) -> TradeSignal:
        return TradeSignal(
            action="HOLD",
            entry=price,
            sl=0.0,
            tp=0.0,
            rr=0.0,
            confidence=0,
            reason=reason,
            phase=phase_result.phase.value,
            direction=phase_result.direction.value,
        )

    def _close(self, price: float, close_reason: str, phase_result: PhaseResult) -> TradeSignal:
        return TradeSignal(
            action="CLOSE",
            entry=price,
            sl=0.0,
            tp=0.0,
            rr=0.0,
            confidence=0,
            reason=f"EXIT: {close_reason}",
            phase=phase_result.phase.value,
            direction=phase_result.direction.value,
            close_reason=close_reason,
        )

    def _check_close(self,
                     open_position: dict,
                     price: float,
                     snap: IndicatorSnapshot,
                     phase_result: PhaseResult) -> Optional[TradeSignal]:
        """
        Evaluate whether to close an open position early.
        Returns CLOSE signal or None (keep holding).

        CLOSE triggers:
          1. Momentum crossed zero AND is accelerating opposite direction
          2. EXHAUSTION detected (blow-off or divergence against position)
          3. Failed breakout: price back inside KC bands (fakeout)
          4. Opposite EXPANSION detected (handled by position_manager as FLIP,
             but we still signal CLOSE here as safety)
        """
        side = open_position.get("side", "")

        # 1. Momentum zero-cross (reversed and accelerating)
        if side == "LONG":
            if snap.momentum < 0 and snap.momentum < snap.momentum_prev:
                return self._close(price,
                    f"Momentum crossed below zero ({snap.momentum:.2f}). Trend reversed.",
                    phase_result)
        elif side == "SHORT":
            if snap.momentum > 0 and snap.momentum > snap.momentum_prev:
                return self._close(price,
                    f"Momentum crossed above zero ({snap.momentum:.2f}). Trend reversed.",
                    phase_result)

        # 2. Exhaustion detected against our position
        if phase_result.phase == Phase.EXHAUSTION:
            if side == "LONG" and snap.rsi_bearish_divergence:
                return self._close(price,
                    f"Bearish RSI divergence detected while holding LONG (RSI={snap.rsi:.1f}).",
                    phase_result)
            if side == "SHORT" and snap.rsi_bullish_divergence:
                return self._close(price,
                    f"Bullish RSI divergence detected while holding SHORT (RSI={snap.rsi:.1f}).",
                    phase_result)
            if snap.is_blowoff:
                return self._close(price,
                    f"Blow-off candle detected (range={snap.candle_range:.0f} > 2x ATR).",
                    phase_result)

        # 3. Failed breakout: price returned inside KC
        if side == "LONG" and price < snap.kc_lower:
            return self._close(price,
                f"Failed breakout: price {price:.2f} back below KC_lower {snap.kc_lower:.2f}.",
                phase_result)
        if side == "SHORT" and price > snap.kc_upper:
            return self._close(price,
                f"Failed breakout: price {price:.2f} back above KC_upper {snap.kc_upper:.2f}.",
                phase_result)

        return None  # No close needed

    def _score_confidence(self, snap: IndicatorSnapshot, phase_result: PhaseResult) -> int:
        """Score 0-100 based on quality of setup."""
        score = 60  # base for valid EXPANSION signal

        # Squeeze duration bonus
        if phase_result.squeeze_candles >= 6:
            score += 10
        elif phase_result.squeeze_candles >= 3:
            score += 5

        # Volume bonus
        if snap.vol_ratio >= 2.0:
            score += 10
        elif snap.vol_ratio >= 1.5:
            score += 5

        # Momentum strength bonus
        if abs(snap.momentum) > abs(snap.momentum_prev) * 1.5:
            score += 10

        # RSI in healthy range (not extreme)
        if 40 <= snap.rsi <= 65:
            score += 5

        # Penalty: vol barely meeting threshold
        if snap.vol_ratio < 1.5:
            score -= 5

        return min(100, max(0, score))
