"""
BybitConnector — QuantScalper
Real-time WebSocket for price ticks + candle closes.
REST for order placement/modification.
"""

import time
import hmac
import hashlib
import threading
import requests
from typing import Optional, Callable, Dict, Any
from pybit.unified_trading import WebSocket


class BybitConnector:
    """
    Manages:
      1. WebSocket kline subscription → fires on each candle tick
      2. WebSocket ticker subscription → fires on each price tick
      3. REST API for order placement, SL/TP modification, position query
    """

    BASE_URL = "https://api.bybit.com"

    def __init__(self,
                 api_key: str,
                 api_secret: str,
                 symbol: str,
                 timeframe: str = "15",
                 testnet: bool = False,
                 logger=None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.symbol = symbol
        self.timeframe = timeframe
        self.testnet = testnet
        self.logger = logger

        # WebSocket handles
        self._ws_public: Optional[WebSocket] = None
        self._ws_private: Optional[WebSocket] = None

        # Callbacks registered by start.py
        self._on_price_tick: Optional[Callable[[float], None]] = None
        self._on_candle_close: Optional[Callable[[dict], None]] = None

        # Latest state
        self.latest_price: float = 0.0
        self._lock = threading.Lock()

    # ── Callback Registration ─────────────────────────────────────────────────

    def on_price_tick(self, callback: Callable[[float], None]):
        """Register handler called on every price tick."""
        self._on_price_tick = callback

    def on_candle_close(self, callback: Callable[[dict], None]):
        """Register handler called when a 15m candle CLOSES."""
        self._on_candle_close = callback

    # ── WebSocket Management ──────────────────────────────────────────────────

    def start(self):
        """Start public WebSocket subscriptions."""
        try:
            self._ws_public = WebSocket(
                testnet=self.testnet,
                channel_type="linear",
            )
            # Subscribe to kline (candle) stream
            self._ws_public.kline_stream(
                interval=int(self.timeframe),
                symbol=self.symbol,
                callback=self._handle_kline,
            )
            # Subscribe to ticker for real-time price
            self._ws_public.ticker_stream(
                symbol=self.symbol,
                callback=self._handle_ticker,
            )
            self._log(f"WebSocket started for {self.symbol} {self.timeframe}m")
        except Exception as e:
            self._log_error(f"WebSocket start failed: {e}")

    def stop(self):
        if self._ws_public:
            try:
                self._ws_public.exit()
            except Exception:
                pass
            self._ws_public = None

    # ── Message Handlers ──────────────────────────────────────────────────────

    def _handle_ticker(self, msg: dict):
        """Real-time price tick handler."""
        try:
            data = msg.get("data", {})
            price = float(data.get("lastPrice", 0) or data.get("markPrice", 0))
            if price > 0:
                with self._lock:
                    self.latest_price = price
                if self._on_price_tick:
                    self._on_price_tick(price)
        except Exception as e:
            self._log_error(f"Ticker handler error: {e}")

    def _handle_kline(self, msg: dict):
        """Kline stream handler — fires on candle close."""
        try:
            data_list = msg.get("data", [])
            for candle in data_list:
                # Bybit kline: confirm=True means candle is CLOSED
                if candle.get("confirm", False):
                    candle_data = {
                        "ts":     int(candle["start"]),
                        "open":   float(candle["open"]),
                        "high":   float(candle["high"]),
                        "low":    float(candle["low"]),
                        "close":  float(candle["close"]),
                        "volume": float(candle["volume"]),
                    }
                    if self._on_candle_close:
                        self._on_candle_close(candle_data)
        except Exception as e:
            self._log_error(f"Kline handler error: {e}")

    # ── REST Order Execution ──────────────────────────────────────────────────

    def place_order(self,
                    side: str,
                    qty: float,
                    sl: float,
                    tp: float,
                    dry_run: bool = False) -> Optional[Dict[str, Any]]:
        """
        Place market order with SL and TP.
        side: "Buy" or "Sell"
        qty: quantity in base currency (BTC)
        """
        if dry_run:
            self._log(f"[DRY-RUN] {side} qty={qty:.6f} SL={sl:.2f} TP={tp:.2f}")
            return {"orderId": "dry-run", "side": side, "qty": qty}

        payload = {
            "category": "linear",
            "symbol": self.symbol,
            "side": side,
            "orderType": "Market",
            "qty": str(round(qty, 6)),
            "stopLoss": str(round(sl, 2)),
            "takeProfit": str(round(tp, 2)),
            "slTriggerBy": "MarkPrice",
            "tpTriggerBy": "MarkPrice",
            "timeInForce": "IOC",
        }
        return self._signed_post("/v5/order/create", payload)

    def modify_sl(self, sl: float, dry_run: bool = False) -> Optional[Dict]:
        """Modify SL of the current open position."""
        if dry_run:
            self._log(f"[DRY-RUN] Modify SL → {sl:.2f}")
            return {"result": "dry-run"}

        payload = {
            "category": "linear",
            "symbol": self.symbol,
            "stopLoss": str(round(sl, 2)),
            "slTriggerBy": "MarkPrice",
        }
        return self._signed_post("/v5/position/trading-stop", payload)

    def close_position(self, side: str, qty: float, dry_run: bool = False) -> Optional[Dict]:
        """Close open position with market order in opposite direction."""
        close_side = "Sell" if side == "LONG" else "Buy"
        if dry_run:
            self._log(f"[DRY-RUN] Close {side} qty={qty:.6f}")
            return {"result": "dry-run"}

        payload = {
            "category": "linear",
            "symbol": self.symbol,
            "side": close_side,
            "orderType": "Market",
            "qty": str(round(qty, 6)),
            "reduceOnly": True,
            "timeInForce": "IOC",
        }
        return self._signed_post("/v5/order/create", payload)

    def get_position(self) -> Optional[Dict]:
        """Get current open position info."""
        params = {"category": "linear", "symbol": self.symbol}
        result = self._signed_get("/v5/position/list", params)
        if result and result.get("retCode") == 0:
            positions = result["result"]["list"]
            for pos in positions:
                if float(pos.get("size", 0)) > 0:
                    return pos
        return None

    def get_balance(self) -> float:
        """Get available USDT balance (supports Unified Trading Account)."""
        result = self._signed_get("/v5/account/wallet-balance",
                                  {"accountType": "UNIFIED"})
        if result and result.get("retCode") == 0:
            for acc in result["result"]["list"]:
                for coin in acc.get("coin", []):
                    if coin["coin"] == "USDT":
                        # Unified account: availableToWithdraw may be empty → use walletBalance
                        raw = coin.get("availableToWithdraw") or coin.get("walletBalance", "0")
                        try:
                            return float(raw)
                        except (ValueError, TypeError):
                            return 0.0
        return 0.0

    def get_balance_detail(self) -> dict:
        """Get full USDT balance detail (equity, wallet, unrealised PnL)."""
        result = self._signed_get("/v5/account/wallet-balance",
                                  {"accountType": "UNIFIED"})
        if result and result.get("retCode") == 0:
            for acc in result["result"]["list"]:
                for coin in acc.get("coin", []):
                    if coin["coin"] == "USDT":
                        def _f(k): 
                            try: return float(coin.get(k) or 0)
                            except: return 0.0
                        return {
                            "equity":        _f("equity"),
                            "wallet":        _f("walletBalance"),
                            "unrealised_pnl":_f("unrealisedPnl"),
                            "cum_realised":  _f("cumRealisedPnl"),
                        }
        return {}

    def set_leverage(self, leverage: int):
        """Set leverage for the symbol."""
        payload = {
            "category": "linear",
            "symbol": self.symbol,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage),
        }
        return self._signed_post("/v5/position/set-leverage", payload)

    # ── Signed HTTP Helpers ───────────────────────────────────────────────────

    def _signed_post(self, endpoint: str, payload: dict) -> Optional[Dict]:
        try:
            ts = str(int(time.time() * 1000))
            body = str(payload).replace("'", '"')
            sign_str = ts + self.api_key + "5000" + body
            signature = hmac.new(
                self.api_secret.encode(), sign_str.encode(), hashlib.sha256
            ).hexdigest()
            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-SIGN": signature,
                "X-BAPI-RECV-WINDOW": "5000",
                "Content-Type": "application/json",
            }
            import json
            resp = requests.post(
                self.BASE_URL + endpoint,
                headers=headers,
                data=json.dumps(payload),
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            self._log_error(f"POST {endpoint} failed: {e}")
            return None

    def _signed_get(self, endpoint: str, params: dict) -> Optional[Dict]:
        try:
            ts = str(int(time.time() * 1000))
            param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            sign_str = ts + self.api_key + "5000" + param_str
            signature = hmac.new(
                self.api_secret.encode(), sign_str.encode(), hashlib.sha256
            ).hexdigest()
            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-SIGN": signature,
                "X-BAPI-RECV-WINDOW": "5000",
            }
            resp = requests.get(
                self.BASE_URL + endpoint,
                headers=headers,
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            self._log_error(f"GET {endpoint} failed: {e}")
            return None

    def _log(self, msg: str):
        if self.logger:
            self.logger.info(msg)
        else:
            print(f"[BybitConnector] {msg}")

    def _log_error(self, msg: str):
        if self.logger:
            self.logger.error(msg)
        else:
            print(f"[BybitConnector ERROR] {msg}")
