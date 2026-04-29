"""
IndicatorEngine — QuantScalper
Computes all raw indicators needed by PhaseDetector and SignalEngine.
No trading decisions here. Pure math.
"""

import numpy as np
from typing import List, Optional
from dataclasses import dataclass


@dataclass
class IndicatorSnapshot:
    """Single-point snapshot of all computed indicators."""
    # Price
    close: float
    high: float
    low: float
    open: float
    volume: float

    # Bollinger Bands (20, 2.0)
    bb_upper: float
    bb_middle: float
    bb_lower: float

    # Keltner Channel (20, 1.5 ATR)
    kc_upper: float
    kc_middle: float
    kc_lower: float

    # ATR
    atr14: float
    candle_range: float  # high - low of current candle

    # TTM Squeeze
    squeeze_on: bool          # BB inside KC
    squeeze_candles: int      # consecutive candles squeeze has been ON

    # Momentum histogram (linear regression deviation)
    momentum: float           # positive = bullish, negative = bearish
    momentum_prev: float      # previous bar momentum (for slope check)

    # RSI
    rsi: float
    rsi_prev_high: float      # highest RSI in last N candles
    rsi_prev_low: float       # lowest RSI in last N candles
    price_prev_high: float    # highest close in last N candles
    price_prev_low: float     # lowest close in last N candles

    # Volume
    vol_sma20: float
    vol_ratio: float          # volume / vol_sma20

    # Derived flags
    rsi_bearish_divergence: bool   # price new high but RSI lower high
    rsi_bullish_divergence: bool   # price new low but RSI higher low
    is_blowoff: bool               # candle_range > ATR_BLOW_MULT * atr14
    is_climactic_volume: bool      # vol > VOL_CLIMAX_MULT * vol_sma20


class IndicatorEngine:
    """
    Computes all indicators from OHLCV data.
    Input: List of OHLCV candles [timestamp, open, high, low, close, volume]
    Output: IndicatorSnapshot for the latest candle
    """

    def __init__(self, config: dict):
        self.bb_period = int(config.get("BB_PERIOD", 20))
        self.bb_std = float(config.get("BB_STD", 2.0))
        self.kc_period = int(config.get("KC_PERIOD", 20))
        self.kc_atr_mult = float(config.get("KC_ATR_MULT", 1.5))
        self.atr_period = int(config.get("ATR_PERIOD", 14))
        self.rsi_period = int(config.get("RSI_PERIOD", 14))
        self.rsi_lookback = int(config.get("RSI_DIVERGENCE_LOOKBACK", 5))
        self.vol_period = int(config.get("VOL_SMA_PERIOD", 20))
        self.atr_blow_mult = float(config.get("ATR_BLOW_MULT", 2.0))
        self.vol_climax_mult = float(config.get("VOL_CLIMAX_MULT", 3.0))

        # State
        self._squeeze_candles = 0  # track consecutive squeeze candles
        self._prev_squeeze_on = False

    def compute(self, ohlcv: list) -> Optional[IndicatorSnapshot]:
        """
        Compute all indicators from OHLCV list.
        OHLCV format: [[ts, open, high, low, close, volume], ...]
        Returns None if not enough data.
        """
        min_required = max(self.bb_period, self.kc_period, self.atr_period,
                           self.rsi_period, self.vol_period) + self.rsi_lookback + 5
        if len(ohlcv) < min_required:
            return None

        opens   = np.array([float(c[1]) for c in ohlcv])
        highs   = np.array([float(c[2]) for c in ohlcv])
        lows    = np.array([float(c[3]) for c in ohlcv])
        closes  = np.array([float(c[4]) for c in ohlcv])
        volumes = np.array([float(c[5]) for c in ohlcv])

        # ── Bollinger Bands ──
        bb_mid   = self._sma(closes, self.bb_period)
        bb_std   = self._rolling_std(closes, self.bb_period)
        bb_upper = bb_mid + self.bb_std * bb_std
        bb_lower = bb_mid - self.bb_std * bb_std

        # ── ATR ──
        atr = self._atr(highs, lows, closes, self.atr_period)

        # ── Keltner Channel ──
        kc_mid   = self._ema(closes, self.kc_period)
        kc_upper = kc_mid + self.kc_atr_mult * atr
        kc_lower = kc_mid - self.kc_atr_mult * atr

        # ── Squeeze (last bar) ──
        squeeze_on = bool(bb_upper[-1] < kc_upper[-1] and bb_lower[-1] > kc_lower[-1])
        if squeeze_on:
            self._squeeze_candles += 1
        else:
            self._squeeze_candles = 0

        # ── Momentum Histogram ──
        # Linear regression of closes over bb_period, deviation = close - linreg
        momentum_series = self._momentum(closes, self.bb_period)

        # ── RSI ──
        rsi_series = self._rsi(closes, self.rsi_period)

        # ── Volume SMA ──
        vol_sma = self._sma(volumes, self.vol_period)
        vol_ratio = float(volumes[-1] / vol_sma[-1]) if vol_sma[-1] > 0 else 0.0

        # ── RSI Divergence ──
        lb = self.rsi_lookback
        price_window = closes[-lb-1:-1]
        rsi_window   = rsi_series[-lb-1:-1]

        price_prev_high = float(np.max(price_window)) if len(price_window) > 0 else closes[-1]
        price_prev_low  = float(np.min(price_window)) if len(price_window) > 0 else closes[-1]
        rsi_prev_high   = float(np.max(rsi_window))   if len(rsi_window) > 0 else rsi_series[-1]
        rsi_prev_low    = float(np.min(rsi_window))   if len(rsi_window) > 0 else rsi_series[-1]

        # Bearish divergence: price makes new high, RSI does not
        rsi_bearish_div = bool(
            closes[-1] > price_prev_high and rsi_series[-1] < rsi_prev_high
        )
        # Bullish divergence: price makes new low, RSI does not
        rsi_bullish_div = bool(
            closes[-1] < price_prev_low and rsi_series[-1] > rsi_prev_low
        )

        # ── Candle range / Blow-off ──
        candle_range = float(highs[-1] - lows[-1])
        is_blowoff = candle_range > self.atr_blow_mult * atr[-1]
        is_climactic = vol_ratio > self.vol_climax_mult

        return IndicatorSnapshot(
            close=float(closes[-1]),
            high=float(highs[-1]),
            low=float(lows[-1]),
            open=float(opens[-1]),
            volume=float(volumes[-1]),
            bb_upper=float(bb_upper[-1]),
            bb_middle=float(bb_mid[-1]),
            bb_lower=float(bb_lower[-1]),
            kc_upper=float(kc_upper[-1]),
            kc_middle=float(kc_mid[-1]),
            kc_lower=float(kc_lower[-1]),
            atr14=float(atr[-1]),
            candle_range=candle_range,
            squeeze_on=squeeze_on,
            squeeze_candles=self._squeeze_candles,
            momentum=float(momentum_series[-1]),
            momentum_prev=float(momentum_series[-2]) if len(momentum_series) > 1 else 0.0,
            rsi=float(rsi_series[-1]),
            rsi_prev_high=rsi_prev_high,
            rsi_prev_low=rsi_prev_low,
            price_prev_high=price_prev_high,
            price_prev_low=price_prev_low,
            vol_sma20=float(vol_sma[-1]),
            vol_ratio=vol_ratio,
            rsi_bearish_divergence=rsi_bearish_div,
            rsi_bullish_divergence=rsi_bullish_div,
            is_blowoff=is_blowoff,
            is_climactic_volume=is_climactic,
        )

    # ── Math Helpers ──────────────────────────────────────────────────────────

    def _sma(self, arr: np.ndarray, period: int) -> np.ndarray:
        result = np.full_like(arr, np.nan)
        for i in range(period - 1, len(arr)):
            result[i] = np.mean(arr[i - period + 1:i + 1])
        return result

    def _ema(self, arr: np.ndarray, period: int) -> np.ndarray:
        result = np.full_like(arr, np.nan, dtype=float)
        k = 2.0 / (period + 1)
        result[period - 1] = np.mean(arr[:period])
        for i in range(period, len(arr)):
            result[i] = arr[i] * k + result[i - 1] * (1 - k)
        return result

    def _rolling_std(self, arr: np.ndarray, period: int) -> np.ndarray:
        result = np.full_like(arr, np.nan, dtype=float)
        for i in range(period - 1, len(arr)):
            result[i] = np.std(arr[i - period + 1:i + 1], ddof=0)
        return result

    def _atr(self, highs, lows, closes, period: int) -> np.ndarray:
        tr = np.zeros(len(closes))
        for i in range(1, len(closes)):
            tr[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )
        result = self._sma(tr, period)
        return result

    def _rsi(self, closes: np.ndarray, period: int) -> np.ndarray:
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        result = np.full(len(closes), 50.0)

        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])

        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            rs = avg_gain / avg_loss if avg_loss > 0 else 1e9
            result[i + 1] = 100 - (100 / (1 + rs))

        return result

    def _momentum(self, closes: np.ndarray, period: int) -> np.ndarray:
        """Linear regression deviation from midline (momentum histogram)."""
        result = np.zeros(len(closes))
        for i in range(period - 1, len(closes)):
            y = closes[i - period + 1:i + 1]
            x = np.arange(period, dtype=float)
            x -= x.mean()
            slope = np.dot(x, y) / np.dot(x, x)
            intercept = y.mean() - slope * x.mean()
            linreg_val = intercept + slope * (period - 1)
            result[i] = closes[i] - linreg_val
        return result
