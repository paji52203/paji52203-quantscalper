"""
MarketData — QuantScalper
Fetches OHLCV candle history from Bybit REST API.
Simple, no sentiment/macro — pure price data.
"""

import time
import requests
from typing import Optional, List


class MarketData:
    """
    Fetches OHLCV from Bybit v5 REST API.
    Returns list of [timestamp, open, high, low, close, volume]
    """

    BASE_URL = "https://api.bybit.com"

    INTERVAL_MAP = {
        "1": "1", "3": "3", "5": "5", "15": "15",
        "30": "30", "60": "60", "240": "240",
        "D": "D", "W": "W", "M": "M",
    }

    def __init__(self, symbol: str, timeframe: str = "15", logger=None):
        self.symbol = symbol
        self.interval = self.INTERVAL_MAP.get(str(timeframe), "15")
        self.logger = logger

    def fetch_ohlcv(self, limit: int = 300) -> Optional[List[list]]:
        """
        Fetch recent OHLCV candles.
        Returns list of [ts, open, high, low, close, volume] sorted oldest→newest.
        """
        try:
            url = f"{self.BASE_URL}/v5/market/kline"
            params = {
                "category": "linear",
                "symbol": self.symbol,
                "interval": self.interval,
                "limit": limit,
            }
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if data.get("retCode") != 0:
                self._log_error(f"Bybit API error: {data.get('retMsg')}")
                return None

            raw = data["result"]["list"]
            # Bybit returns newest first → reverse to oldest first
            # Format: [startTime, open, high, low, close, volume, turnover]
            candles = []
            for row in reversed(raw):
                candles.append([
                    int(row[0]),     # timestamp ms
                    float(row[1]),   # open
                    float(row[2]),   # high
                    float(row[3]),   # low
                    float(row[4]),   # close
                    float(row[5]),   # volume
                ])
            return candles

        except requests.RequestException as e:
            self._log_error(f"OHLCV fetch failed: {e}")
            return None

    def get_current_price(self) -> Optional[float]:
        """Get latest mark price from REST (for initialization)."""
        try:
            url = f"{self.BASE_URL}/v5/market/tickers"
            params = {"category": "linear", "symbol": self.symbol}
            resp = requests.get(url, params=params, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            if data.get("retCode") == 0:
                return float(data["result"]["list"][0]["lastPrice"])
        except Exception as e:
            self._log_error(f"Price fetch failed: {e}")
        return None

    def _log_error(self, msg: str):
        if self.logger:
            self.logger.error(msg)
        else:
            print(f"[MarketData ERROR] {msg}")
