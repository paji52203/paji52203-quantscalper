"""
IndicatorEngine — QuantScalper v2
Computes all raw indicators needed by PhaseDetector and SignalEngine.
No trading decisions here. Pure math.

New in v2:
  - SuperTrend (ATR-based dynamic trend line)
  - ADX (Average Directional Index — trend strength / regime filter)
  - MACD (momentum confirmation)
  - Confluence Score (0-10, used by SignalEngine)
"""

import numpy as np
from typing import List, Optional
from dataclasses import dataclass, field


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
    rsi_prev_high: float
    rsi_prev_low: float
    price_prev_high: float
    price_prev_low: float

    # Volume
    vol_sma20: float
    vol_ratio: float

    # Derived flags
    rsi_bearish_divergence: bool
    rsi_bullish_divergence: bool
    is_blowoff: bool
    is_climactic_volume: bool
    is_bullish_pinbar: bool     # long lower wick rejecting support
    is_bearish_pinbar: bool     # long upper wick rejecting resistance

    # ── NEW v2 ──────────────────────────────────────────────

    # SuperTrend
    supertrend: float         # SuperTrend line value
    supertrend_bull: bool     # True = price above ST (bullish trend)

    # ADX (trend strength)
    adx: float                # 0-100, >20 = trending, >40 = strong trend

    # MACD
    macd: float               # MACD line
    macd_signal: float        # Signal line
    macd_hist: float          # Histogram (macd - signal)
    macd_bull: bool           # macd > signal = bullish momentum

    # EMA trend
    ema50: float
    ema200: float
    ema_bull: bool            # ema50 > ema200

    # Confluence Score (-10 to +10)
    # Positive = bullish pressure, Negative = bearish pressure
    confluence_score: int


class IndicatorEngine:
    """
    Computes all indicators from OHLCV data.
    Input:  List of OHLCV candles [timestamp, open, high, low, close, volume]
    Output: IndicatorSnapshot for the latest candle
    """

    def __init__(self, config: dict):
        self.bb_period    = int(config.get("BB_PERIOD", 20))
        self.bb_std       = float(config.get("BB_STD", 2.0))
        self.kc_period    = int(config.get("KC_PERIOD", 20))
        self.kc_atr_mult  = float(config.get("KC_ATR_MULT", 1.5))
        self.atr_period   = int(config.get("ATR_PERIOD", 14))
        self.rsi_period   = int(config.get("RSI_PERIOD", 14))
        self.rsi_lookback = int(config.get("RSI_DIVERGENCE_LOOKBACK", 5))
        self.vol_period   = int(config.get("VOL_SMA_PERIOD", 20))
        self.atr_blow_mult   = float(config.get("ATR_BLOW_MULT", 2.0))
        self.vol_climax_mult = float(config.get("VOL_CLIMAX_MULT", 3.0))

        # SuperTrend params
        self.st_period = int(config.get("ST_PERIOD", 10))
        self.st_mult   = float(config.get("ST_MULT", 3.0))

        # ADX params
        self.adx_period = int(config.get("ADX_PERIOD", 14))

        # MACD params
        self.macd_fast   = int(config.get("MACD_FAST", 12))
        self.macd_slow   = int(config.get("MACD_SLOW", 26))
        self.macd_signal = int(config.get("MACD_SIGNAL", 9))

        # EMA trend
        self.ema50_period  = 50
        self.ema200_period = 200

        # State
        self._squeeze_candles  = 0
        self._prev_squeeze_on  = False
        self._st_prev_upper    = 0.0
        self._st_prev_lower    = 0.0
        self._st_prev_bull     = True

    def compute(self, ohlcv: list) -> Optional[IndicatorSnapshot]:
        min_required = max(
            self.bb_period, self.kc_period, self.atr_period,
            self.rsi_period, self.vol_period,
            self.macd_slow + self.macd_signal,
            self.ema200_period
        ) + self.rsi_lookback + 10

        if len(ohlcv) < min_required:
            return None

        opens   = np.array([float(c[1]) for c in ohlcv])
        highs   = np.array([float(c[2]) for c in ohlcv])
        lows    = np.array([float(c[3]) for c in ohlcv])
        closes  = np.array([float(c[4]) for c in ohlcv])
        volumes = np.array([float(c[5]) for c in ohlcv])

        # ── Bollinger Bands ──
        bb_mid   = self._sma(closes, self.bb_period)
        bb_std_  = self._rolling_std(closes, self.bb_period)
        bb_upper = bb_mid + self.bb_std * bb_std_
        bb_lower = bb_mid - self.bb_std * bb_std_

        # ── ATR ──
        atr = self._atr(highs, lows, closes, self.atr_period)

        # ── Keltner Channel ──
        kc_mid   = self._ema(closes, self.kc_period)
        kc_upper = kc_mid + self.kc_atr_mult * atr
        kc_lower = kc_mid - self.kc_atr_mult * atr

        # ── Squeeze ──
        squeeze_on = bool(bb_upper[-1] < kc_upper[-1] and bb_lower[-1] > kc_lower[-1])
        if squeeze_on:
            self._squeeze_candles += 1
        else:
            self._squeeze_candles = 0

        # ── Momentum ──
        momentum_series = self._momentum(closes, self.bb_period)

        # ── RSI ──
        rsi_series = self._rsi(closes, self.rsi_period)

        # ── Volume ──
        vol_sma   = self._sma(volumes, self.vol_period)
        vol_ratio = float(volumes[-1] / vol_sma[-1]) if vol_sma[-1] > 0 else 0.0

        # ── RSI Divergence ──
        lb = self.rsi_lookback
        price_window = closes[-lb-1:-1]
        rsi_window   = rsi_series[-lb-1:-1]
        price_prev_high = float(np.max(price_window)) if len(price_window) > 0 else closes[-1]
        price_prev_low  = float(np.min(price_window)) if len(price_window) > 0 else closes[-1]
        rsi_prev_high   = float(np.max(rsi_window))   if len(rsi_window)   > 0 else rsi_series[-1]
        rsi_prev_low    = float(np.min(rsi_window))   if len(rsi_window)   > 0 else rsi_series[-1]
        rsi_bearish_div = bool(closes[-1] > price_prev_high and rsi_series[-1] < rsi_prev_high)
        rsi_bullish_div = bool(closes[-1] < price_prev_low  and rsi_series[-1] > rsi_prev_low)

        # ── Candle range / Blow-off / Pin Bar ──
        candle_range  = float(highs[-1] - lows[-1])
        is_blowoff    = candle_range > self.atr_blow_mult * atr[-1]
        is_climactic  = vol_ratio > self.vol_climax_mult

        body = abs(closes[-1] - opens[-1])
        upper_wick = highs[-1] - max(opens[-1], closes[-1])
        lower_wick = min(opens[-1], closes[-1]) - lows[-1]
        
        # Bullish pinbar: long lower wick (>= 2x body), small upper wick
        is_bull_pinbar = (lower_wick >= 2 * body) and (upper_wick < body) and (candle_range > 0)
        # Bearish pinbar: long upper wick (>= 2x body), small lower wick
        is_bear_pinbar = (upper_wick >= 2 * body) and (lower_wick < body) and (candle_range > 0)

        # ── SuperTrend ──
        st_val, st_bull = self._supertrend(highs, lows, closes, atr)

        # ── ADX ──
        adx_val = self._adx(highs, lows, closes, self.adx_period)

        # ── MACD ──
        macd_line, macd_sig, macd_hist_ = self._macd(
            closes, self.macd_fast, self.macd_slow, self.macd_signal
        )
        macd_bull = bool(macd_line[-1] > macd_sig[-1])

        # ── EMA 50 / 200 ──
        ema50_  = self._ema(closes, self.ema50_period)
        ema200_ = self._ema(closes, self.ema200_period)
        ema_bull = bool(ema50_[-1] > ema200_[-1])

        # ── Confluence Score (-10 to +10) ──
        score = self._confluence(
            squeeze_on=squeeze_on,
            squeeze_candles=self._squeeze_candles,
            momentum=float(momentum_series[-1]),
            momentum_prev=float(momentum_series[-2]) if len(momentum_series) > 1 else 0.0,
            rsi=float(rsi_series[-1]),
            vol_ratio=vol_ratio,
            st_bull=st_bull,
            adx=adx_val,
            macd_bull=macd_bull,
            macd_hist=float(macd_hist_[-1]),
            ema_bull=ema_bull,
            is_bull_pinbar=is_bull_pinbar,
            is_bear_pinbar=is_bear_pinbar,
        )

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
            is_bullish_pinbar=bool(is_bull_pinbar),
            is_bearish_pinbar=bool(is_bear_pinbar),
            # v2
            supertrend=float(st_val),
            supertrend_bull=st_bull,
            adx=adx_val,
            macd=float(macd_line[-1]),
            macd_signal=float(macd_sig[-1]),
            macd_hist=float(macd_hist_[-1]),
            macd_bull=macd_bull,
            ema50=float(ema50_[-1]),
            ema200=float(ema200_[-1]),
            ema_bull=ema_bull,
            confluence_score=score,
        )

    # ── Confluence Scoring ────────────────────────────────────────────────────

    def _confluence(self, squeeze_on, squeeze_candles, momentum, momentum_prev,
                    rsi, vol_ratio, st_bull, adx, macd_bull, macd_hist, ema_bull,
                    is_bull_pinbar=False, is_bear_pinbar=False) -> int:
        """
        Compute directional confluence score.
        Positive = bullish, Negative = bearish.
        Range: -10 to +10.

        Signal engine uses:
          score >= +6 → BUY candidate
          score <= -6 → SELL candidate
          (squeeze must also fire for full confirmation)
        """
        score = 0

        # SuperTrend direction (±2 — heaviest weight as primary trend filter)
        score += 2 if st_bull else -2

        # EMA stack (±1)
        score += 1 if ema_bull else -1

        # Momentum direction (±2)
        if momentum > 0:
            score += 2
        elif momentum < 0:
            score -= 2

        # Momentum accelerating (±1)
        if momentum > 0 and momentum > momentum_prev:
            score += 1
        elif momentum < 0 and momentum < momentum_prev:
            score -= 1

        # RSI direction (±1)
        if rsi > 52:
            score += 1
        elif rsi < 48:
            score -= 1

        # MACD direction (±1)
        score += 1 if macd_bull else -1

        # ADX regime — only add if trending (±1 when ADX > 20)
        if adx > 20:
            score += 1 if st_bull else -1

        # Volume confirmation (±1)
        if vol_ratio >= 1.2:
            score += 1 if (momentum > 0) else -1

        # Squeeze active — adds urgency/confidence but no direction (±0)
        # Squeeze CANDLES bonus: longer squeeze = stronger release
        if squeeze_on and squeeze_candles >= 3:
            score += 1 if (momentum > 0) else -1

        # Pin Bar Rejection bonus (±2)
        if is_bull_pinbar and momentum > 0:
            score += 2
        if is_bear_pinbar and momentum < 0:
            score -= 2

        return max(-10, min(10, score))

    # ── SuperTrend ────────────────────────────────────────────────────────────

    def _supertrend(self, highs, lows, closes, atr):
        """Compute SuperTrend. Returns (line_value, is_bullish) for last candle."""
        n = len(closes)
        upper = np.zeros(n)
        lower = np.zeros(n)
        st    = np.zeros(n)
        bull  = np.ones(n, dtype=bool)

        for i in range(1, n):
            hl2 = (highs[i] + lows[i]) / 2.0
            atr_val = atr[i] if not np.isnan(atr[i]) else 0.0
            upper[i] = hl2 + self.st_mult * atr_val
            lower[i] = hl2 - self.st_mult * atr_val

            # Adjust bands to only tighten
            if i > 1:
                if lower[i] < lower[i-1] or closes[i-1] < lower[i-1]:
                    lower[i] = lower[i]
                else:
                    lower[i] = lower[i-1]
                if upper[i] > upper[i-1] or closes[i-1] > upper[i-1]:
                    upper[i] = upper[i]
                else:
                    upper[i] = upper[i-1]

            # Determine direction
            if i == 1:
                bull[i] = True
                st[i]   = lower[i]
            else:
                if bull[i-1]:
                    bull[i] = closes[i] >= lower[i]
                else:
                    bull[i] = closes[i] > upper[i]
                st[i] = lower[i] if bull[i] else upper[i]

        return float(st[-1]), bool(bull[-1])

    # ── ADX ──────────────────────────────────────────────────────────────────

    def _adx(self, highs, lows, closes, period: int) -> float:
        """Average Directional Index — measures trend strength (0-100)."""
        n = len(closes)
        if n < period + 1:
            return 0.0

        plus_dm  = np.zeros(n)
        minus_dm = np.zeros(n)
        tr_arr   = np.zeros(n)

        for i in range(1, n):
            up   = highs[i]  - highs[i-1]
            down = lows[i-1] - lows[i]
            plus_dm[i]  = up   if (up > down and up > 0)   else 0.0
            minus_dm[i] = down if (down > up and down > 0) else 0.0
            tr_arr[i]   = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i]  - closes[i-1])
            )

        # Smoothed via Wilder's smoothing
        def wilder(arr, p):
            r = np.zeros(len(arr))
            r[p] = np.sum(arr[1:p+1])
            for i in range(p+1, len(arr)):
                r[i] = r[i-1] - (r[i-1] / p) + arr[i]
            return r

        tr_s  = wilder(tr_arr, period)
        pdm_s = wilder(plus_dm, period)
        mdm_s = wilder(minus_dm, period)

        pdi = np.where(tr_s > 0, 100 * pdm_s / tr_s, 0.0)
        mdi = np.where(tr_s > 0, 100 * mdm_s / tr_s, 0.0)
        dx  = np.where((pdi + mdi) > 0, 100 * np.abs(pdi - mdi) / (pdi + mdi), 0.0)

        adx_arr = wilder(dx, period)
        return float(adx_arr[-1])

    # ── MACD ─────────────────────────────────────────────────────────────────

    def _macd(self, closes, fast, slow, signal_period):
        """MACD = EMA(fast) - EMA(slow). Signal = EMA(MACD, signal_period)."""
        ema_fast = self._ema(closes, fast)
        ema_slow = self._ema(closes, slow)
        macd_line = ema_fast - ema_slow
        sig_line  = self._ema(macd_line, signal_period)
        hist      = macd_line - sig_line
        return macd_line, sig_line, hist

    # ── Math Helpers ──────────────────────────────────────────────────────────

    def _sma(self, arr: np.ndarray, period: int) -> np.ndarray:
        result = np.full_like(arr, np.nan, dtype=float)
        for i in range(period - 1, len(arr)):
            result[i] = np.mean(arr[i - period + 1:i + 1])
        return result

    def _ema(self, arr: np.ndarray, period: int) -> np.ndarray:
        result = np.full_like(arr, np.nan, dtype=float)
        k = 2.0 / (period + 1)
        # Find first valid point
        start = period - 1
        if start >= len(arr):
            return result
        result[start] = np.mean(arr[:period])
        for i in range(start + 1, len(arr)):
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
                abs(lows[i]  - closes[i - 1])
            )
        return self._sma(tr, period)

    def _rsi(self, closes: np.ndarray, period: int) -> np.ndarray:
        deltas = np.diff(closes)
        gains  = np.where(deltas > 0, deltas, 0.0)
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
            slope     = np.dot(x, y) / np.dot(x, x)
            intercept = y.mean() - slope * x.mean()
            linreg    = intercept + slope * (period - 1)
            result[i] = closes[i] - linreg
        return result
