"""
TelegramNotifier — QuantScalper
Synchronous Telegram notifications via Bot API.
Sends alerts for all key trading events.
"""

import os
import time
import threading
import requests
from datetime import datetime
from typing import Optional


class TelegramNotifier:
    """
    Lightweight synchronous Telegram notifier.
    Uses requests (not aiohttp) — compatible with threaded WebSocket callbacks.

    Events:
      - startup()          → bot is live
      - entry()            → new position opened
      - close()            → position closed
      - trail_sl()         → SL updated (trailed)
      - flip()             → position flipped
      - phase_change()     → new phase detected (EXPANSION only)
      - error()            → critical error
    """

    BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, token: str, chat_id: str, symbol: str,
                 dry_run: bool = True, logger=None):
        self.token = token
        self.chat_id = chat_id
        self.symbol = symbol
        self.dry_run = dry_run
        self.logger = logger
        self._enabled = bool(token and chat_id)
        self._lock = threading.Lock()

        if self._enabled:
            self._log(f"Telegram notifier ready for chat_id={chat_id}")
        else:
            self._log("Telegram not configured — notifications disabled.")

    # ── Event Methods ─────────────────────────────────────────────────────────

    def startup(self):
        mode = "🧪 DRY-RUN" if self.dry_run else "🔴 LIVE"
        self._send(
            f"🚀 <b>QuantScalper Started</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Symbol:</b> <code>{self.symbol}</code>\n"
            f"<b>Mode:</b> {mode}\n"
            f"<b>Time:</b> <code>{self._now()}</code>"
        )

    def entry(self, side: str, entry: float, sl: float, tp: float,
              rr: float, confidence: int, reason: str, qty: float = 0.0):
        emoji = "🟢" if side == "LONG" else "🔴"
        arrow = "📈" if side == "LONG" else "📉"
        self._send(
            f"{emoji} <b>ENTER {side}</b> {arrow}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Symbol:</b> <code>{self.symbol}</code>\n"
            f"<b>Entry:</b>  <code>${entry:,.2f}</code>\n"
            f"<b>SL:</b>     <code>${sl:,.2f}</code>\n"
            f"<b>TP:</b>     <code>${tp:,.2f}</code>\n"
            f"<b>R/R:</b>    <code>1:{rr}</code>\n"
            f"<b>Conf:</b>   <code>{confidence}%</code>\n"
            + (f"<b>Qty:</b>    <code>{qty:.4f} BTC</code>\n" if qty > 0 else "")
            + f"━━━━━━━━━━━━━━━━━━━\n"
            f"<i>{reason[:200]}</i>\n"
            f"🕙 <code>{self._now()}</code>"
            + (" | 🧪 DRY-RUN" if self.dry_run else "")
        )

    def close(self, side: str, entry: float, close_price: float,
              reason: str, pnl_usdt: Optional[float] = None):
        if pnl_usdt is not None:
            pnl_emoji = "💰" if pnl_usdt >= 0 else "💸"
            pnl_str = f"\n<b>PnL:</b>    {pnl_emoji} <code>${pnl_usdt:+.2f}</code>"
        else:
            pnl_str = ""

        self._send(
            f"⛔ <b>CLOSE {side}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Symbol:</b>  <code>{self.symbol}</code>\n"
            f"<b>Entry:</b>   <code>${entry:,.2f}</code>\n"
            f"<b>Exit:</b>    <code>${close_price:,.2f}</code>"
            f"{pnl_str}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"<i>{reason[:200]}</i>\n"
            f"🕙 <code>{self._now()}</code>"
        )

    def trail_sl(self, side: str, old_sl: float, new_sl: float, current_price: float):
        direction = "⬆️" if new_sl > old_sl else "⬇️"
        self._send(
            f"🔒 <b>TRAIL SL</b> {direction}\n"
            f"<b>{side}</b> | <code>{self.symbol}</code>\n"
            f"<b>Old SL:</b> <code>${old_sl:,.2f}</code>\n"
            f"<b>New SL:</b> <code>${new_sl:,.2f}</code>\n"
            f"<b>Price:</b>  <code>${current_price:,.2f}</code>\n"
            f"🕙 <code>{self._now()}</code>"
        )

    def flip(self, from_side: str, to_side: str, price: float, reason: str):
        self._send(
            f"🔄 <b>FLIP: {from_side} → {to_side}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Symbol:</b> <code>{self.symbol}</code>\n"
            f"<b>Price:</b>  <code>${price:,.2f}</code>\n"
            f"<i>{reason[:200]}</i>\n"
            f"🕙 <code>{self._now()}</code>"
        )

    def expansion(self, direction: str, squeeze_candles: int,
                  momentum: float, vol_ratio: float, rsi: float):
        emoji = "📈" if direction == "LONG" else "📉"
        self._send(
            f"⚡ <b>EXPANSION DETECTED</b> {emoji}\n"
            f"<b>Dir:</b>     <code>{direction}</code>\n"
            f"<b>Squeeze:</b> <code>{squeeze_candles} candles</code>\n"
            f"<b>Mom:</b>     <code>{momentum:.2f}</code>\n"
            f"<b>Vol:</b>     <code>{vol_ratio:.2f}x avg</code>\n"
            f"<b>RSI:</b>     <code>{rsi:.1f}</code>\n"
            f"🕙 <code>{self._now()}</code>"
        )

    def exhaustion(self, flags: list, rsi: float, vol_ratio: float):
        flag_str = "\n".join(f"  • {f}" for f in flags)
        self._send(
            f"🚫 <b>EXHAUSTION — Entry Blocked</b>\n"
            f"<b>RSI:</b> <code>{rsi:.1f}</code> | "
            f"<b>Vol:</b> <code>{vol_ratio:.2f}x</code>\n"
            f"{flag_str}\n"
            f"🕙 <code>{self._now()}</code>"
        )

    def error(self, msg: str):
        self._send(
            f"❌ <b>ERROR</b> — QuantScalper\n"
            f"<code>{msg[:500]}</code>\n"
            f"🕙 <code>{self._now()}</code>"
        )

    def stopped(self):
        self._send(
            f"🛑 <b>QuantScalper Stopped</b>\n"
            f"<code>{self.symbol}</code> | "
            f"🕙 <code>{self._now()}</code>"
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _send(self, text: str):
        if not self._enabled:
            return
        with self._lock:
            try:
                url = self.BASE_URL.format(token=self.token)
                payload = {
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
                resp = requests.post(url, json=payload, timeout=10)
                if resp.status_code != 200:
                    self._log(f"Telegram error {resp.status_code}: {resp.text[:100]}")
            except Exception as e:
                self._log(f"Telegram send failed: {e}")

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _log(self, msg: str):
        if self.logger:
            self.logger.info("[Telegram] %s", msg)
        else:
            print(f"[Telegram] {msg}")
