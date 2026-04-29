"""
PhaseDetector v2 — QuantScalper
Classifies market phase + tracks squeeze zone for precise SL.

Changes v2:
  - Tracks squeeze zone high/low for SL placement
  - SuperTrend gate: direction must align with SuperTrend
  - EMA50/200 gate: direction must align with trend stack
  - Confluence score gate: score >= threshold to enter
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import List
from .indicator_engine import IndicatorSnapshot


class Phase(Enum):
    COMPRESSION = "COMPRESSION"   # Squeeze ON, energy building
    EXPANSION   = "EXPANSION"     # Squeeze fired, breakout confirmed
    EXHAUSTION  = "EXHAUSTION"    # Blow-off or divergence — skip
    NEUTRAL     = "NEUTRAL"       # No squeeze, no signal


class Direction(Enum):
    LONG    = "LONG"
    SHORT   = "SHORT"
    NEUTRAL = "NEUTRAL"


@dataclass
class PhaseResult:
    phase: Phase
    direction: Direction
    reason: str
    squeeze_candles: int
    momentum: float
    vol_ratio: float
    exhaustion_flags: List[str]
    # v2: squeeze zone for SL
    squeeze_zone_high: float = 0.0   # highest high during squeeze
    squeeze_zone_low:  float = 0.0   # lowest low during squeeze
    # v2: confluence
    confluence_score: int = 0
    supertrend_bull: bool = True
    ema_bull: bool = True


class PhaseDetector:
    """
    Reads IndicatorSnapshot → PhaseResult.

    Entry gates (v2):
      1. Exhaustion check (hard block)
      2. Squeeze fired with enough history
      3. SuperTrend direction aligns with squeeze momentum
      4. EMA50/200 trend stack aligns
      5. Confluence score >= threshold
      6. Volume confirmation
    """

    CONFLUENCE_THRESHOLD = 5   # score >= this to allow LONG entry
                               # score <= -this to allow SHORT entry

    def __init__(self, config: dict):
        self.squeeze_min   = int(config.get("SQUEEZE_MIN_CANDLES", 3))
        self.squeeze_stale = int(config.get("SQUEEZE_STALE_LIMIT", 20))
        self.vol_ratio_min = float(config.get("VOL_RATIO_MIN", 1.3))

        # State
        self._prev_squeeze_on      = False
        self._prev_squeeze_candles = 0
        self._squeeze_zone_highs: list = []
        self._squeeze_zone_lows:  list = []

    def detect(self, snap: IndicatorSnapshot) -> PhaseResult:
        """Classify market phase from latest indicator snapshot."""

        # Track squeeze zone (high/low while squeeze is active)
        if snap.squeeze_on:
            self._squeeze_zone_highs.append(snap.high)
            self._squeeze_zone_lows.append(snap.low)
            self._prev_squeeze_candles = snap.squeeze_candles
        else:
            # Squeeze ended — reset zone after we use it
            if not self._prev_squeeze_on:
                self._squeeze_zone_highs = []
                self._squeeze_zone_lows  = []

        squeeze_zone_high = max(self._squeeze_zone_highs) if self._squeeze_zone_highs else snap.high
        squeeze_zone_low  = min(self._squeeze_zone_lows)  if self._squeeze_zone_lows  else snap.low

        # Squeeze just fired: was ON, now OFF
        squeeze_fired = self._prev_squeeze_on and not snap.squeeze_on

        effective_squeeze_candles = (
            self._prev_squeeze_candles if squeeze_fired
            else snap.squeeze_candles
        )

        self._prev_squeeze_on = snap.squeeze_on

        exhaustion_flags = self._check_exhaustion(snap)

        # ── EXHAUSTION: hard block ──
        if exhaustion_flags:
            return PhaseResult(
                phase=Phase.EXHAUSTION,
                direction=Direction.NEUTRAL,
                reason=f"Exhaustion: {', '.join(exhaustion_flags)}",
                squeeze_candles=effective_squeeze_candles,
                momentum=snap.momentum,
                vol_ratio=snap.vol_ratio,
                exhaustion_flags=exhaustion_flags,
                squeeze_zone_high=squeeze_zone_high,
                squeeze_zone_low=squeeze_zone_low,
                confluence_score=snap.confluence_score,
                supertrend_bull=snap.supertrend_bull,
                ema_bull=snap.ema_bull,
            )

        # ── Extreme Trend Breakout (No Squeeze Required) ──
        # If the market suddenly dumps/pumps strongly without prior compression.
        is_extreme_trend_long  = (snap.confluence_score >= 8) and (snap.adx > 25) and (snap.vol_ratio >= self.vol_ratio_min)
        is_extreme_trend_short = (snap.confluence_score <= -8) and (snap.adx > 25) and (snap.vol_ratio >= self.vol_ratio_min)
        is_extreme_trend       = is_extreme_trend_long or is_extreme_trend_short

        # ── EXPANSION: squeeze fired OR extreme trend ──
        if ((squeeze_fired and effective_squeeze_candles >= self.squeeze_min and snap.vol_ratio >= self.vol_ratio_min) 
            or is_extreme_trend):

            direction = self._get_direction(snap)

            # Prevent mismatch
            if is_extreme_trend_long and direction != Direction.LONG:
                direction = Direction.LONG
            if is_extreme_trend_short and direction != Direction.SHORT:
                direction = Direction.SHORT

            # ── Gate 1: SuperTrend must align ──
            if direction == Direction.LONG and not snap.supertrend_bull:
                return self._hold(snap, effective_squeeze_candles,
                    f"Setup LONG but SuperTrend is BEARISH — skip.",
                    squeeze_zone_high, squeeze_zone_low)

            if direction == Direction.SHORT and snap.supertrend_bull:
                return self._hold(snap, effective_squeeze_candles,
                    f"Setup SHORT but SuperTrend is BULLISH — skip.",
                    squeeze_zone_high, squeeze_zone_low)

            # ── Gate 2: EMA trend stack ──
            if direction == Direction.LONG and not snap.ema_bull:
                return self._hold(snap, effective_squeeze_candles,
                    f"Setup LONG but EMA50 < EMA200 (bearish trend) — skip.",
                    squeeze_zone_high, squeeze_zone_low)

            if direction == Direction.SHORT and snap.ema_bull:
                return self._hold(snap, effective_squeeze_candles,
                    f"Setup SHORT but EMA50 > EMA200 (bullish trend) — skip.",
                    squeeze_zone_high, squeeze_zone_low)

            # ── Gate 3: Confluence score ──
            if direction == Direction.LONG and snap.confluence_score < self.CONFLUENCE_THRESHOLD:
                return self._hold(snap, effective_squeeze_candles,
                    f"Setup LONG but confluence score {snap.confluence_score} < {self.CONFLUENCE_THRESHOLD}.",
                    squeeze_zone_high, squeeze_zone_low)

            if direction == Direction.SHORT and snap.confluence_score > -self.CONFLUENCE_THRESHOLD:
                return self._hold(snap, effective_squeeze_candles,
                    f"Setup SHORT but confluence score {snap.confluence_score} > -{self.CONFLUENCE_THRESHOLD}.",
                    squeeze_zone_high, squeeze_zone_low)

            # ── All gates passed → EXPANSION ──
            # Clear zone after use
            self._squeeze_zone_highs = []
            self._squeeze_zone_lows  = []
            
            if is_extreme_trend:
                reason_str = (f"EXTREME TREND (No Squeeze). ADX={snap.adx:.1f} "
                              f"ST={'BULL' if snap.supertrend_bull else 'BEAR'} "
                              f"EMA={'BULL' if snap.ema_bull else 'BEAR'} "
                              f"Score={snap.confluence_score} "
                              f"Mom={snap.momentum:.1f} Vol={snap.vol_ratio:.2f}x")
            else:
                reason_str = (f"SQUEEZE BREAKOUT ({effective_squeeze_candles}c). "
                              f"ST={'BULL' if snap.supertrend_bull else 'BEAR'} "
                              f"EMA={'BULL' if snap.ema_bull else 'BEAR'} "
                              f"Score={snap.confluence_score} "
                              f"Mom={snap.momentum:.1f} Vol={snap.vol_ratio:.2f}x")

            return PhaseResult(
                phase=Phase.EXPANSION,
                direction=direction,
                reason=reason_str,
                squeeze_candles=effective_squeeze_candles,
                momentum=snap.momentum,
                vol_ratio=snap.vol_ratio,
                exhaustion_flags=[],
                squeeze_zone_high=squeeze_zone_high,
                squeeze_zone_low=squeeze_zone_low,
                confluence_score=snap.confluence_score,
                supertrend_bull=snap.supertrend_bull,
                ema_bull=snap.ema_bull,
            )

        # ── COMPRESSION ──
        if snap.squeeze_on and effective_squeeze_candles < self.squeeze_stale:
            return PhaseResult(
                phase=Phase.COMPRESSION,
                direction=Direction.NEUTRAL,
                reason=f"Squeeze active {effective_squeeze_candles}c. Score={snap.confluence_score}. Waiting.",
                squeeze_candles=effective_squeeze_candles,
                momentum=snap.momentum,
                vol_ratio=snap.vol_ratio,
                exhaustion_flags=[],
                squeeze_zone_high=squeeze_zone_high,
                squeeze_zone_low=squeeze_zone_low,
                confluence_score=snap.confluence_score,
                supertrend_bull=snap.supertrend_bull,
                ema_bull=snap.ema_bull,
            )

        # ── NEUTRAL ──
        return PhaseResult(
            phase=Phase.NEUTRAL,
            direction=Direction.NEUTRAL,
            reason=f"No setup. ST={'BULL' if snap.supertrend_bull else 'BEAR'} Score={snap.confluence_score}",
            squeeze_candles=effective_squeeze_candles,
            momentum=snap.momentum,
            vol_ratio=snap.vol_ratio,
            exhaustion_flags=[],
            squeeze_zone_high=squeeze_zone_high,
            squeeze_zone_low=squeeze_zone_low,
            confluence_score=snap.confluence_score,
            supertrend_bull=snap.supertrend_bull,
            ema_bull=snap.ema_bull,
        )

    def _hold(self, snap, sq_c, reason, sq_high, sq_low) -> PhaseResult:
        return PhaseResult(
            phase=Phase.NEUTRAL,
            direction=Direction.NEUTRAL,
            reason=reason,
            squeeze_candles=sq_c,
            momentum=snap.momentum,
            vol_ratio=snap.vol_ratio,
            exhaustion_flags=[],
            squeeze_zone_high=sq_high,
            squeeze_zone_low=sq_low,
            confluence_score=snap.confluence_score,
            supertrend_bull=snap.supertrend_bull,
            ema_bull=snap.ema_bull,
        )

    def _check_exhaustion(self, snap: IndicatorSnapshot) -> list:
        flags = []
        if snap.is_blowoff:
            flags.append(f"blow-off (range={snap.candle_range:.0f} > 2xATR)")
        if snap.is_climactic_volume:
            flags.append(f"climactic vol ({snap.vol_ratio:.1f}x)")
        if snap.rsi_bearish_divergence:
            flags.append(f"RSI bearish div (RSI={snap.rsi:.1f})")
        if snap.rsi_bullish_divergence:
            flags.append(f"RSI bullish div (RSI={snap.rsi:.1f})")
        if snap.squeeze_candles >= self.squeeze_stale:
            flags.append(f"stale squeeze ({snap.squeeze_candles}c)")
        
        # Wick rejection blocks
        if snap.momentum < 0 and snap.is_bullish_pinbar:
            flags.append(f"Bullish Pin Bar rejection blocking SHORT")
        if snap.momentum > 0 and snap.is_bearish_pinbar:
            flags.append(f"Bearish Pin Bar rejection blocking LONG")
            
        return flags

    def _get_direction(self, snap: IndicatorSnapshot) -> Direction:
        """Direction from momentum + SuperTrend alignment."""
        if snap.momentum > 0 and snap.supertrend_bull:
            return Direction.LONG
        if snap.momentum < 0 and not snap.supertrend_bull:
            return Direction.SHORT
        # Fallback: momentum alone
        if snap.momentum > 0:
            return Direction.LONG
        if snap.momentum < 0:
            return Direction.SHORT
        return Direction.NEUTRAL
