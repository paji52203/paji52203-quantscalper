"""
RiskManager — QuantScalper
Simple, honest position sizing.
No dynamic leverage madness — fixed leverage from config.
"""


class RiskManager:
    """
    Calculates position quantity and validates trade costs.

    Fee structure (Bybit linear futures):
      Taker fee: 0.055% per order (market orders)
      Round-trip entry: 0.11%  (open + close)
      Flip cost:        0.11%  (close old + open new)
      → Entry needs > 0.35% to be profitable
      → Flip needs > 0.11% just to break even
      → Flip always worth it if current position is in loss
    """

    MIN_QTY = 0.001   # Bybit BTC minimum order size
    TAKER_FEE_PCT = 0.00055  # 0.055%

    def __init__(self, config: dict, logger=None):
        self.leverage = int(config.get("LEVERAGE", 20))
        self.risk_pct = float(config.get("RISK_PCT_PER_TRADE", 1.0)) / 100.0
        self.fee_pct = self.TAKER_FEE_PCT
        self.logger = logger

        # Fee thresholds
        self.round_trip_cost = 2 * self.fee_pct  # 0.11% — open + close
        self.flip_cost = 2 * self.fee_pct         # 0.11% — close old + open new

    def calculate_qty(self,
                      balance: float,
                      entry: float,
                      sl: float) -> float:
        """
        Calculate order quantity in BTC.

        Uses risk-based sizing:
          max_loss_usdt = balance * risk_pct
          sl_distance_pct = |entry - sl| / entry
          notional = max_loss_usdt / sl_distance_pct
          qty = notional / entry

        Capped by: leverage * balance (max notional)
        """
        if balance <= 0 or entry <= 0 or sl <= 0:
            return 0.0

        sl_dist = abs(entry - sl)
        if sl_dist <= 0:
            return 0.0

        sl_pct = sl_dist / entry

        # Max loss in USDT this trade can cause
        max_loss_usdt = balance * self.risk_pct

        # Required notional to produce that loss at this SL distance
        notional = max_loss_usdt / sl_pct

        # Cap at leverage * balance
        max_notional = balance * self.leverage
        notional = min(notional, max_notional)

        qty = notional / entry
        qty = round(qty, 4)

        # Enforce minimum
        if qty < self.MIN_QTY:
            self._log(f"Qty {qty:.6f} below minimum {self.MIN_QTY}. Skipping.")
            return 0.0

        self._log(
            f"Sizing: Balance={balance:.2f} USDT | Risk={self.risk_pct*100:.1f}% "
            f"| SL_dist={sl_pct*100:.3f}% | Notional={notional:.2f} | Qty={qty:.4f} BTC"
        )
        return qty

    def is_flip_worth_it(self,
                         entry: float,
                         new_tp: float,
                         new_sl: float,
                         current_pnl_usdt: float = 0.0,
                         notional: float = 0.0) -> tuple:
        """
        Decide if a flip maneuver is worth executing.

        Returns: (should_flip: bool, reason: str)

        Logic:
          1. If current position is LOSING → flip immediately if new direction valid
             (stopping loss > paying flip fee, even if new position only nets $0.01)
          2. If current position is WINNING → flip only if new TP covers flip cost
          3. Never delay when losing — accumulation of loss > flip fee
        """
        new_tp_dist  = abs(new_tp - entry) / entry if entry > 0 else 0
        new_sl_dist  = abs(new_sl - entry) / entry if entry > 0 else 0
        flip_cost_pct = self.flip_cost  # 0.11%

        # New position must at minimum cover its own flip fee
        covers_flip_fee = new_tp_dist > flip_cost_pct

        # Calculate expected net gain from flip
        expected_gain_pct = new_tp_dist - self.round_trip_cost  # after close+open+close
        expected_gain_usdt = (expected_gain_pct * notional) if notional > 0 else 0

        # Case 1: Already losing → flip immediately if new side is valid
        if current_pnl_usdt < 0 and covers_flip_fee:
            reason = (
                f"Flip: cutting loss (PnL={current_pnl_usdt:+.2f} USDT). "
                f"New side expected gain={expected_gain_pct*100:.3f}% "
                f"covers flip cost={flip_cost_pct*100:.3f}%."
            )
            return True, reason

        if current_pnl_usdt < 0 and not covers_flip_fee:
            reason = (
                f"Flip BLOCKED: new TP distance {new_tp_dist*100:.3f}% "
                f"< flip cost {flip_cost_pct*100:.3f}%. "
                f"Would add loss, not reduce it. CLOSE instead."
            )
            return False, reason

        # Case 2: Position profitable → only flip if new side is meaningfully better
        if current_pnl_usdt >= 0:
            if covers_flip_fee:
                reason = (
                    f"Flip: profitable position ({current_pnl_usdt:+.2f} USDT) + "
                    f"new side expected net gain={expected_gain_usdt:+.2f} USDT."
                )
                return True, reason
            else:
                reason = (
                    f"Flip BLOCKED: position profitable ({current_pnl_usdt:+.2f}) "
                    f"and new TP only {new_tp_dist*100:.3f}%. Not worth flipping."
                )
                return False, reason

        return False, "Flip condition undetermined."

    def estimate_pnl(self, side: str, entry: float,
                     current_price: float, notional: float) -> float:
        """Estimate unrealized PnL in USDT."""
        if side == "LONG":
            return (current_price - entry) / entry * notional
        else:
            return (entry - current_price) / entry * notional

