"""The backtest must step through time by TIMESTAMP, not list index.

Found 2026-10-06: Alpaca's SOL/USD history is missing ~14 months, so in the ~5-year window
bar i of SOL was over a year apart from bar i of BTC, and SOL "traded" straight across the hole.
The window config/params.json had been tuned on was mostly that artifact.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest import last_hours, simulate
from config import Settings
from crypto_broker import Bar

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def settings():
    return Settings("k", "s", ["AAA/USD", "BBB/USD"], 2, 4, 6, 18.0, 8.0, 0.0, 15.0, 75.0, 50.0, 5)


def flat(hours, price=100.0, start=T0):
    return [Bar(start + timedelta(hours=h), price, price, price, price, 1.0) for h in range(hours)]


class AlignmentTests(unittest.TestCase):
    def test_a_hole_in_one_symbol_does_not_shift_the_other_in_time(self):
        # BBB has a 1000-hour hole; a steady AAA must still be simulated hour by hour.
        aaa = flat(2000)
        bbb = flat(500) + flat(500, start=T0 + timedelta(hours=1500))
        r = simulate(settings(), {"AAA/USD": aaa, "BBB/USD": bbb})
        self.assertEqual(2000 - 5, r["hours_simulated"])  # clock covers AAA's full span

    def test_position_across_a_data_hole_is_closed_not_carried(self):
        # BBB rallies (so the strategy buys it), then its data stops for 1000 hours and resumes
        # 10x higher. Carrying the position across the hole would book a fake 10x.
        rising = [Bar(T0 + timedelta(hours=h), 100 + h, 100 + h, 100 + h, 100 + h, 1.0) for h in range(40)]
        after = flat(100, price=1500.0, start=T0 + timedelta(hours=1040))
        r = simulate(settings(), {"AAA/USD": flat(1140), "BBB/USD": rising + after})
        self.assertEqual(1, r["exit_reasons"].get("data_gap"))
        self.assertLess(r["total_return_pct"], 10)

    def test_last_hours_slices_by_clock_time(self):
        aaa = flat(100)
        bbb = flat(10) + flat(10, start=T0 + timedelta(hours=90))  # 20 bars spanning 100h
        sliced = last_hours({"AAA/USD": aaa, "BBB/USD": bbb}, 20)
        self.assertEqual(20, len(sliced["AAA/USD"]))
        self.assertEqual(10, len(sliced["BBB/USD"]))  # not its last 20 BARS, which reach back 100h


if __name__ == "__main__":
    unittest.main()
