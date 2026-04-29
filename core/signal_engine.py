"""
SignalEngine v2 — QuantScalper
Converts PhaseResult + IndicatorSnapshot into executable TradeSignal.

Changes v2:
  - SL from squeeze zone low/high (tighter, better RR)
  - SuperTrend line as dynamic SL fallback
  - Confidence score uses confluence_score from IndicatorSnapshot
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
    rr: float
    confidence: int      # 0-100
    reason: str
    phase: str
    direction: str
    close_reason: str = ""


class SignalEngine:
    """
    Entry logic v2:
      - EXPANSION phase required (from PhaseDetector — already has ST+EMA+score gates)
      - SL = squeeze zone low/high (structure SL, tighter than KC)
      - TP = entry ± RR_MIN * SL_dist
      - Fee gate and RR validation
    """

    def __init__(self, config: dict):
        self.rr_min        = float(config.get("RR_MIN", 1.5))
        self.min_move_pct  = float(config.get("MIN_MOVE_PCT", 0.35)) / 100.0

    def evaluate(self,
                 phase_result: PhaseResult,
                 snap: IndicatorSnapshot,
                 current_price: Optional[float] = None,
                 open_position: Optional[dict] = None) -> TradeSignal:
        """
        Generate trade signal.
        current_price: live tick price. If None uses snap.close.
        open_position: dict with 'side','entry','sl','tp'. Evaluated for CLOSE first.
        """
        price = current_price if current_price is not None else snap.close

        # ── CLOSE evaluation (open position) ──
        if open_position:
            close_signal = self._check_close(open_position, price, snap, phase_result)
            if close_signal:
                return close_signal

        # ── EXPANSION required ──
        if phase_result.phase != Phase.EXPANSION:
            return self._hold(price, phase_result.reason, phase_result)

        if phase_result.direction == Direction.NEUTRAL:
            return self._hold(price, "Direction unclear at expansion.", phase_result)

        # ── SL from squeeze zone (structure-based, tighter) ──
        # Primary SL: low/high of the squeeze zone candles
        # Fallback:   SuperTrend line (dynamic), then KC band
        if phase_result.direction == Direction.LONG:
            action  = "BUY"
            # SL below squeeze zone low, small ATR buffer
            sq_sl = phase_result.squeeze_zone_low - (0.1 * snap.atr14)
            # Fallback: max of SuperTrend and KC lower (tighter of the two vs price)
            fallback_sl = max(snap.supertrend, snap.kc_lower)
            # Pick squeeze zone SL if valid (below entry)
            sl      = sq_sl if (0 < sq_sl < price) else fallback_sl
            sl_dist = price - sl
            tp      = price + self.rr_min * sl_dist

        else:  # SHORT
            action  = "SELL"
            # SL above squeeze zone high, small ATR buffer
            sq_sl = phase_result.squeeze_zone_high + (0.1 * snap.atr14)
            # Fallback: min of SuperTrend and KC upper
            fallback_sl = min(snap.supertrend, snap.kc_upper)
            sl      = sq_sl if (sq_sl > price > 0) else fallback_sl
            sl_dist = sl - price
            tp      = price - self.rr_min * sl_dist

        # Validate SL side
        if action == "BUY"  and sl >= price:
            return self._hold(price, f"SL {sl:.2f} >= entry {price:.2f}", phase_result)
        if action == "SELL" and sl <= price:
            return self._hold(price, f"SL {sl:.2f} <= entry {price:.2f}", phase_result)
        if sl_dist <= 0:
            return self._hold(price, "SL distance zero.", phase_result)

        # Fee gate
        expected_move_pct = abs(tp - price) / price
        if expected_move_pct < self.min_move_pct:
            return self._hold(
                price,
                f"Fee gate: {expected_move_pct*100:.3f}% < {self.min_move_pct*100:.2f}%",
                phase_result
            )

        # RR check
        tp_dist = abs(tp - price)
        rr = round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0.0
        if rr < self.rr_min:
            return self._hold(price, f"RR {rr:.2f} < min {self.rr_min}", phase_result)

        confidence = self._score_confidence(snap, phase_result)

        reason = (
            f"EXPANSION {phase_result.squeeze_candles}c | "
            f"ST={'▲' if phase_result.supertrend_bull else '▼'} "
            f"EMA={'▲' if phase_result.ema_bull else '▼'} "
            f"Score={phase_result.confluence_score} "
            f"ADX={snap.adx:.1f} "
            f"Vol={snap.vol_ratio:.2f}x RSI={snap.rsi:.1f} RR={rr} "
            f"SL=structure"
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

    # ── Hold / Close helpers ──────────────────────────────────────────────────

    def _hold(self, price: float, reason: str, phase_result: PhaseResult) -> TradeSignal:
        return TradeSignal(
            action="HOLD", entry=price, sl=0.0, tp=0.0, rr=0.0,
            confidence=0, reason=reason,
            phase=phase_result.phase.value,
            direction=phase_result.direction.value,
        )

    def _close(self, price: float, close_reason: str, phase_result: PhaseResult) -> TradeSignal:
        return TradeSignal(
            action="CLOSE", entry=price, sl=0.0, tp=0.0, rr=0.0,
            confidence=0, reason=f"EXIT: {close_reason}",
            phase=phase_result.phase.value,
            direction=phase_result.direction.value,
            close_reason=close_reason,
        )

    def _check_close(self, open_position: dict, price: float,
                     snap: IndicatorSnapshot, phase_result: PhaseResult) -> Optional[TradeSignal]:
        """
        Exit triggers:
          1. Momentum zero-cross + accelerating opposite
          2. Exhaustion against position
          3. Price crosses SuperTrend line (dynamic SL)
          4. Price back inside KC (failed breakout)
        """
        side = open_position.get("side", "")

        # 1. Momentum reversed and accelerating
        if side == "LONG":
            if snap.momentum < 0 and snap.momentum < snap.momentum_prev:
                return self._close(price,
                    f"Momentum below zero ({snap.momentum:.1f}). Trend reversed.",
                    phase_result)
        elif side == "SHORT":
            if snap.momentum > 0 and snap.momentum > snap.momentum_prev:
                return self._close(price,
                    f"Momentum above zero ({snap.momentum:.1f}). Trend reversed.",
                    phase_result)

        # 2. Exhaustion against position
        if phase_result.phase == Phase.EXHAUSTION:
            if side == "LONG" and snap.rsi_bearish_divergence:
                return self._close(price,
                    f"Bearish RSI div while LONG (RSI={snap.rsi:.1f}).", phase_result)
            if side == "SHORT" and snap.rsi_bullish_divergence:
                return self._close(price,
                    f"Bullish RSI div while SHORT (RSI={snap.rsi:.1f}).", phase_result)
            if snap.is_blowoff:
                return self._close(price,
                    f"Blow-off candle (range > 2xATR).", phase_result)

        # 3. SuperTrend flipped against position (dynamic SL)
        if side == "LONG" and not snap.supertrend_bull:
            return self._close(price,
                f"SuperTrend flipped BEARISH while holding LONG.", phase_result)
        if side == "SHORT" and snap.supertrend_bull:
            return self._close(price,
                f"SuperTrend flipped BULLISH while holding SHORT.", phase_result)

        # 4. Failed breakout: back inside KC
        if side == "LONG" and price < snap.kc_lower:
            return self._close(price,
                f"Failed breakout: price {price:.2f} < KC_lower {snap.kc_lower:.2f}.", phase_result)
        if side == "SHORT" and price > snap.kc_upper:
            return self._close(price,
                f"Failed breakout: price {price:.2f} > KC_upper {snap.kc_upper:.2f}.", phase_result)

        return None

    def _score_confidence(self, snap: IndicatorSnapshot, phase_result: PhaseResult) -> int:
        """0-100 confidence score using confluence_score + bonus factors."""
        # Map confluence score (-10..+10) → base 50..100
        abs_score = abs(phase_result.confluence_score)
        base = 50 + int(abs_score * 5)   # score 10 → 100, score 5 → 75

        # ADX bonus: strong trend
        if snap.adx >= 30:
            base += 5
        elif snap.adx >= 20:
            base += 2

        # Squeeze duration bonus
        if phase_result.squeeze_candles >= 6:
            base += 5
        elif phase_result.squeeze_candles >= 3:
            base += 2

        return min(100, max(0, base))
