"""Regression tests for two defects that together made the bot structurally unable to sell.

Both were found on 2026-09-19 with five open positions, all signalling SELL, none executing.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Settings
from crypto_broker import CryptoBroker, PositionSnapshot
from risk_manager import evaluate_decisions
from strategy import TradeDecision


def settings(**overrides):
    values = {
        "alpaca_api_key": "k", "alpaca_secret_key": "s",
        "watchlist": ["BTC/USD", "ETH/USD"],
        "short_sma_window": 4, "long_sma_window": 24, "trend_window": 360,
        "stop_loss_pct": 18.0, "take_profit_pct": 8.0, "min_confidence": 65.0,
        "max_position_pct": 15.0, "max_total_exposure_pct": 75.0,
        "max_daily_loss_pct": 5.0, "max_trades_per_run": 5,
    }
    values.update(overrides)
    return Settings(**values)


class CryptoSymbolNormalizationTests(unittest.TestCase):
    """Alpaca's trading API returns crypto positions WITHOUT the slash the rest of the code uses.

    The old filter skipped any symbol without a slash, which dropped every crypto position and
    left the bot believing it held nothing.
    """

    def test_strips_and_restores_the_quote_currency(self):
        n = CryptoBroker._normalize_crypto_symbol
        self.assertEqual("BTC/USD", n("BTCUSD"))
        self.assertEqual("DOGE/USD", n("DOGEUSD"))
        self.assertEqual("LINK/USD", n("LINKUSD"))

    def test_leaves_an_already_slashed_symbol_alone(self):
        self.assertEqual("BTC/USD", CryptoBroker._normalize_crypto_symbol("BTC/USD"))

    def test_prefers_the_longest_matching_quote_currency(self):
        # "USDT" must win over the "USD" that is a prefix of it, or ETHUSDT would become ETH/USDT
        # spelled wrong and be filed under a key nothing else looks up.
        self.assertEqual("ETH/USDT", CryptoBroker._normalize_crypto_symbol("ETHUSDT"))
        self.assertEqual("ETH/USDC", CryptoBroker._normalize_crypto_symbol("ETHUSDC"))

    def test_unparseable_symbol_returns_none_rather_than_a_wrong_key(self):
        self.assertIsNone(CryptoBroker._normalize_crypto_symbol("AAPL"))

    def test_get_positions_keeps_crypto_and_drops_stocks(self):
        broker = CryptoBroker.__new__(CryptoBroker)
        broker.trading = SimpleNamespace(get_all_positions=lambda: [
            SimpleNamespace(symbol="BTCUSD", asset_class="AssetClass.CRYPTO", qty="0.5",
                            market_value="15000", avg_entry_price="30000", unrealized_plpc="0.04"),
            SimpleNamespace(symbol="AAPL", asset_class="AssetClass.US_EQUITY", qty="10",
                            market_value="2000", avg_entry_price="190", unrealized_plpc="0.05"),
        ])
        positions = CryptoBroker.get_positions(broker)
        self.assertEqual(["BTC/USD"], list(positions))
        self.assertAlmostEqual(15000.0, positions["BTC/USD"].market_value)
        self.assertAlmostEqual(4.0, positions["BTC/USD"].unrealized_plpc)


class ExitIsNotGatedByEntryConfidenceTests(unittest.TestCase):
    """min_confidence is an entry filter. Confidence scales with the SMA spread, and a crossover
    is the moment that spread crosses zero - so exits arrive with near-zero confidence by
    construction. Gating them blocked every exit exactly when it was needed.
    """

    def _sell(self, confidence):
        return TradeDecision(symbol="BTC/USD", action="SELL", size_pct=100,
                             confidence=confidence, reasoning="bearish crossover",
                             short_sma=81207.0, long_sma=81269.0)

    def test_low_confidence_sell_still_executes(self):
        positions = {"BTC/USD": PositionSnapshot("BTC/USD", 0.19, 15660.0, 80850.0, 0.4)}
        buys, sells, skipped = evaluate_decisions(
            [self._sell(2)], settings(), equity=102800, cash=0.22,
            positions=positions, day_pl_pct=-0.7)
        self.assertEqual([], skipped)
        self.assertEqual(1, len(sells))
        self.assertEqual("BTC/USD", sells[0].symbol)

    def test_low_confidence_buy_is_still_blocked(self):
        buy = TradeDecision(symbol="BTC/USD", action="BUY", size_pct=100, confidence=2,
                            reasoning="weak crossover", short_sma=1.0, long_sma=0.9)
        buys, _, skipped = evaluate_decisions(
            [buy], settings(), equity=100000, cash=100000, positions={}, day_pl_pct=0)
        self.assertEqual([], buys)
        self.assertIn("below minimum", skipped[0].reason)


class PositionsTableTests(unittest.TestCase):
    def test_percentage_is_not_multiplied_twice(self):
        from build_dashboard import build_positions_table
        html = build_positions_table(
            {"DOGE/USD": PositionSnapshot("DOGE/USD", 127569.5, 11150.39, 0.0874, 1.76)})
        self.assertIn("+1.76%", html)
        self.assertNotIn("+176.00%", html)


if __name__ == "__main__":
    unittest.main()
