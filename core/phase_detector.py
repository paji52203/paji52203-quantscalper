"""
PhaseDetector — QuantScalper
Classifies current market into one of 3 phases based on IndicatorSnapshot.
No trading logic here — only phase classification.
"""

from enum import Enum
from dataclasses import dataclass
from .indicator_engine import IndicatorSnapshot


class Phase(Enum):
    COMPRESSION = "COMPRESSION"   # Squeeze ON, energy building
    EXPANSION   = "EXPANSION"     # Squeeze just fired, breakout starting
    EXHAUSTION  = "EXHAUSTION"    # Blow-off or divergence, skip entry
    NEUTRAL     = "NEUTRAL"       # No squeeze, no signal


class Direction(Enum):
    LONG    = "LONG"
    SHORT   = "SHORT"
    NEUTRAL = "NEUTRAL"


@dataclass
class PhaseResult:
    phase: Phase
    direction: Direction
    reason: str                  # Human-readable explanation
    squeeze_candles: int         # How many candles squeeze was active
    momentum: float
    vol_ratio: float
    exhaustion_flags: list       # Which exhaustion conditions triggered


class PhaseDetector:
    """
    Reads IndicatorSnapshot and returns PhaseResult.
    Decision tree:
      1. Check EXHAUSTION first (safety gate)
      2. If squeeze just fired → EXPANSION
      3. If squeeze still on → COMPRESSION
      4. Otherwise → NEUTRAL
    """

    def __init__(self, config: dict):
        self.squeeze_min = int(config.get("SQUEEZE_MIN_CANDLES", 3))
        self.squeeze_stale = int(config.get("SQUEEZE_STALE_LIMIT", 20))
        self.vol_ratio_min = float(config.get("VOL_RATIO_MIN", 1.3))

        # State: track previous squeeze state AND duration
        self._prev_squeeze_on = False
        self._prev_squeeze_candles = 0  # how long the PREVIOUS squeeze lasted

    def detect(self, snap: IndicatorSnapshot) -> PhaseResult:
        """Classify market phase from latest indicator snapshot."""

        exhaustion_flags = self._check_exhaustion(snap)

        # Squeeze just fired: was ON, now OFF
        squeeze_fired = self._prev_squeeze_on and not snap.squeeze_on

        # Remember how long the squeeze lasted at fire moment
        if snap.squeeze_on:
            self._prev_squeeze_candles = snap.squeeze_candles

        # Update state
        self._prev_squeeze_on = snap.squeeze_on

        # Use preserved count when squeeze just fired
        effective_squeeze_candles = (
            self._prev_squeeze_candles if squeeze_fired
            else snap.squeeze_candles
        )

        # EXHAUSTION overrides everything
        if exhaustion_flags:
            return PhaseResult(
                phase=Phase.EXHAUSTION,
                direction=Direction.NEUTRAL,
                reason=f"Exhaustion: {', '.join(exhaustion_flags)}",
                squeeze_candles=effective_squeeze_candles,
                momentum=snap.momentum,
                vol_ratio=snap.vol_ratio,
                exhaustion_flags=exhaustion_flags,
            )

        # EXPANSION: squeeze fired + enough compression history + volume ok
        if (squeeze_fired
                and effective_squeeze_candles >= self.squeeze_min
                and snap.vol_ratio >= self.vol_ratio_min):

            direction = self._get_direction(snap)
            return PhaseResult(
                phase=Phase.EXPANSION,
                direction=direction,
                reason=(f"Squeeze fired after {effective_squeeze_candles} candles. "
                        f"Momentum={snap.momentum:.2f} Vol={snap.vol_ratio:.2f}x"),
                squeeze_candles=effective_squeeze_candles,
                momentum=snap.momentum,
                vol_ratio=snap.vol_ratio,
                exhaustion_flags=[],
            )

        # COMPRESSION: squeeze still active and not stale
        if snap.squeeze_on and effective_squeeze_candles < self.squeeze_stale:
            return PhaseResult(
                phase=Phase.COMPRESSION,
                direction=Direction.NEUTRAL,
                reason=f"Squeeze active for {effective_squeeze_candles} candles. Waiting.",
                squeeze_candles=effective_squeeze_candles,
                momentum=snap.momentum,
                vol_ratio=snap.vol_ratio,
                exhaustion_flags=[],
            )

        # NEUTRAL
        return PhaseResult(
            phase=Phase.NEUTRAL,
            direction=Direction.NEUTRAL,
            reason="No squeeze, no clear phase.",
            squeeze_candles=effective_squeeze_candles,
            momentum=snap.momentum,
            vol_ratio=snap.vol_ratio,
            exhaustion_flags=[],
        )

    def _check_exhaustion(self, snap: IndicatorSnapshot) -> list:
        """Return list of exhaustion flags. Empty = not exhausted."""
        flags = []

        if snap.is_blowoff:
            flags.append(f"blow-off (range={snap.candle_range:.0f} > 2x ATR={snap.atr14:.0f})")

        if snap.is_climactic_volume:
            flags.append(f"climactic volume ({snap.vol_ratio:.1f}x avg)")

        if snap.rsi_bearish_divergence:
            flags.append(f"RSI bearish divergence (RSI={snap.rsi:.1f})")

        if snap.rsi_bullish_divergence:
            flags.append(f"RSI bullish divergence (RSI={snap.rsi:.1f})")

        if snap.squeeze_candles >= self.squeeze_stale:
            flags.append(f"stale squeeze ({snap.squeeze_candles} candles)")

        return flags

    def _get_direction(self, snap: IndicatorSnapshot) -> Direction:
        """Determine expansion direction from momentum histogram."""
        momentum_rising = snap.momentum > snap.momentum_prev
        momentum_falling = snap.momentum < snap.momentum_prev

        if snap.momentum > 0 and momentum_rising:
            return Direction.LONG
        if snap.momentum < 0 and momentum_falling:
            return Direction.SHORT

        # Weak signal — momentum direction but not accelerating
        if snap.momentum > 0:
            return Direction.LONG
        if snap.momentum < 0:
            return Direction.SHORT

        return Direction.NEUTRAL
