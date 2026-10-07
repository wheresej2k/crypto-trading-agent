"""Regression tests for the software bracket protecting only a ~2% sliver of each position.

Found 2026-10-06. Every ~$15k buy filled, then the protective order was rejected "insufficient
balance" because it asked for filled_qty while Alpaca had taken its fee out of the coin. The
exception escaped after the fill, so the buy was logged as failed and left untracked; the next
run bought the few hundred dollars of headroom left and only THAT got a take-profit. Across 14
round trips the positions peaked at +5.7% on average and exited at +1.6%.

The fake broker below enforces the two Alpaca behaviours that caused it: the fee comes out of the
coin, and a resting sell order reserves its qty (so a second full-size order is rejected).
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import position_tracker
from position_tracker import load_brackets, open_bracket, reconcile

FEE = 0.0025


class FakeAlpaca:
    def __init__(self, price):
        self.price = price
        self.held = 0.0
        self.orders = {}  # id -> SimpleNamespace(side, qty, status, limit)
        self.fail_take_profit = False
        self.sells = []

    def _order(self, **kw):
        oid = f"o{len(self.orders) + 1}"
        self.orders[oid] = SimpleNamespace(id=oid, status=SimpleNamespace(value=kw.pop("status")), **kw)
        return self.orders[oid]

    def _reserved(self):
        return sum(o.qty for o in self.orders.values() if o.side == "sell" and o.status.value == "new")

    def buy_notional(self, symbol, notional):
        qty = notional / self.price
        self.held += qty * (1 - FEE)
        return self._order(side="buy", qty=qty, status="filled", filled_qty=str(qty),
                           filled_avg_price=str(self.price), filled_at=datetime.now(timezone.utc))

    def get_order(self, oid):
        return self.orders[oid]

    def available_qty(self, symbol):
        return str(self.held - self._reserved()) if self.held else None

    def place_take_profit(self, symbol, qty, limit_price, client_order_id):
        if self.fail_take_profit:
            raise RuntimeError("simulated API outage")
        if float(qty) > self.held - self._reserved() + 1e-12:
            raise RuntimeError("insufficient balance")
        return self._order(side="sell", qty=float(qty), status="new", limit=limit_price)

    def cancel_order(self, oid):
        self.orders[oid].status.value = "canceled"

    def cancel_open_orders_for(self, symbol):
        return 0

    def sell_qty(self, symbol, qty):
        if float(qty) > self.held - self._reserved() + 1e-12:
            raise RuntimeError("insufficient balance")
        self.held -= float(qty)
        self.sells.append(float(qty))
        return self._order(side="sell", qty=float(qty), status="filled", filled_avg_price=str(self.price))


class OpenBracketTests(unittest.TestCase):
    def test_take_profit_covers_the_whole_position_actually_held(self):
        broker = FakeAlpaca(price=100.0)
        bracket, warning = open_bracket(broker, "SOL/USD", 15000, stop_loss_pct=18, take_profit_pct=8)

        self.assertIsNone(warning)
        target = broker.orders[bracket.target_order_id]
        self.assertAlmostEqual(broker.held, target.qty)  # all of it, net of the fee
        self.assertLess(target.qty, 150.0)               # not the pre-fee filled_qty
        self.assertAlmostEqual(108.0, target.limit)

    def test_a_filled_buy_is_tracked_even_when_the_take_profit_fails(self):
        broker = FakeAlpaca(price=100.0)
        broker.fail_take_profit = True
        bracket, warning = open_bracket(broker, "SOL/USD", 15000, stop_loss_pct=18, take_profit_pct=8)

        self.assertIsNotNone(bracket)
        self.assertEqual("", bracket.target_order_id)
        self.assertIn("simulated API outage", warning)


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        position_tracker.FILL_POLL_DELAY_SECONDS = 0
        self.broker = FakeAlpaca(price=100.0)
        self.bracket, _ = open_bracket(self.broker, "SOL/USD", 15000, stop_loss_pct=18, take_profit_pct=8)

    def run_reconcile(self, price):
        self.broker.price = price
        return reconcile(self.broker, {"SOL/USD": self.bracket}, lambda s: price)

    def test_software_stop_sells_everything(self):
        still_open, events = self.run_reconcile(80.0)

        self.assertEqual({}, still_open)
        self.assertEqual("stop_loss", events[0]["exit_reason"])
        self.assertAlmostEqual(0.0, self.broker.held)
        self.assertEqual("canceled", self.broker.orders[self.bracket.target_order_id].status.value)

    def test_filled_take_profit_closes_the_bracket(self):
        self.broker.orders[self.bracket.target_order_id].status.value = "filled"
        still_open, events = self.run_reconcile(109.0)

        self.assertEqual({}, still_open)
        self.assertEqual("take_profit", events[0]["exit_reason"])

    def test_target_is_enforced_in_software_when_no_order_is_resting(self):
        self.broker.cancel_order(self.bracket.target_order_id)
        still_open, events = self.run_reconcile(109.0)

        self.assertEqual({}, still_open)
        self.assertEqual("take_profit", events[0]["exit_reason"])
        self.assertAlmostEqual(0.0, self.broker.held)

    def test_missing_take_profit_is_placed_again(self):
        self.broker.cancel_order(self.bracket.target_order_id)
        still_open, events = self.run_reconcile(101.0)

        self.assertEqual([], events)
        new_target = self.broker.orders[still_open["SOL/USD"].target_order_id]
        self.assertEqual("new", new_target.status.value)
        self.assertAlmostEqual(self.broker.held, new_target.qty)

    def test_quiet_price_leaves_everything_alone(self):
        target_id = self.bracket.target_order_id
        still_open, events = self.run_reconcile(103.0)

        self.assertEqual([], events)
        self.assertEqual(target_id, still_open["SOL/USD"].target_order_id)


class StateFileTests(unittest.TestCase):
    def test_old_state_with_a_stop_order_id_still_loads(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "open_brackets.json"
            path.write_text(json.dumps({"BTC/USD": {
                "trade_id": "abc", "symbol": "BTC/USD", "qty": 0.1, "entry_price": 1.0,
                "stop_price": 0.82, "target_price": 1.08, "stop_order_id": "s1",
                "target_order_id": "t1", "opened_at": "",
            }}))
            original = position_tracker.STATE_PATH
            position_tracker.STATE_PATH = path
            try:
                self.assertEqual("t1", load_brackets()["BTC/USD"].target_order_id)
            finally:
                position_tracker.STATE_PATH = original


if __name__ == "__main__":
    unittest.main()


class SelfHealingTests(unittest.TestCase):
    def setUp(self):
        position_tracker.FILL_POLL_DELAY_SECONDS = 0

    def test_untracked_position_is_adopted_and_protected(self):
        from crypto_broker import PositionSnapshot
        from position_tracker import adopt_untracked

        broker = FakeAlpaca(price=100.0)
        broker.held = 150.0  # bought by an earlier run that lost track of it
        brackets = {}
        positions = {"SOL/USD": PositionSnapshot("SOL/USD", 150.0, 15000.0, 95.0, 5.3),
                     "DOGE/USD": PositionSnapshot("DOGE/USD", 3.0, 0.30, 0.1, 0.0)}  # dust

        messages = adopt_untracked(broker, brackets, positions, ["SOL/USD", "DOGE/USD"], 18, 8)

        self.assertEqual(["SOL/USD"], list(brackets))
        self.assertEqual("SOL/USD", messages[0][0])
        b = brackets["SOL/USD"]
        self.assertAlmostEqual(95.0 * 1.08, b.target_price)  # priced from Alpaca's avg entry
        self.assertAlmostEqual(150.0, broker.orders[b.target_order_id].qty)

    def test_bracket_for_a_position_sold_elsewhere_is_dropped(self):
        broker = FakeAlpaca(price=100.0)
        bracket, _ = open_bracket(broker, "SOL/USD", 15000, 18, 8)
        broker.cancel_order(bracket.target_order_id)
        broker.held = 0.0  # sold by hand on Alpaca's site

        still_open, events = reconcile(broker, {"SOL/USD": bracket}, lambda s: 100.0)

        self.assertEqual(({}, []), (still_open, events))


class TrailingStopTests(unittest.TestCase):
    def setUp(self):
        position_tracker.FILL_POLL_DELAY_SECONDS = 0
        self.broker = FakeAlpaca(price=100.0)
        self.bracket, _ = open_bracket(self.broker, "SOL/USD", 15000, stop_loss_pct=18, take_profit_pct=0)

    def step(self, price):
        self.broker.price = price
        return reconcile(self.broker, {"SOL/USD": self.bracket}, lambda s: price,
                         trailing_stop_pct=3, trail_activation_pct=5)

    def test_no_take_profit_order_when_take_profit_is_off(self):
        self.assertEqual("", self.bracket.target_order_id)
        self.assertEqual(0.0, self.bracket.target_price)
        self.assertEqual([], self.step(150.0)[1])  # no software take-profit either

    def test_trail_arms_after_activation_and_sells_on_pullback(self):
        self.assertEqual([], self.step(104.0)[1])   # +4%: not armed, a 3% dip is fine
        self.assertEqual([], self.step(101.0)[1])
        self.assertEqual([], self.step(110.0)[1])   # +10%: armed, trail at 106.7
        _, events = self.step(106.0)
        self.assertEqual("trailing_stop", events[0]["exit_reason"])
        self.assertAlmostEqual(0.0, self.broker.held)
