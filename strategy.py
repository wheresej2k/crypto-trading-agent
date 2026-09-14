"""Free, rule-based trade signal generator: moving-average crossover PLUS a long-term trend
filter, adapted for hourly crypto bars (the stock bot uses daily bars - see README for why the
windows differ here). No API calls, no cost, fully backtestable.

Two rules, both must agree to enter a position:
1. Crossover (tactical): if the short-term average price has moved above the long-term average,
   that's bullish; below (while holding), that's bearish (exit).
2. Trend filter (macro, added 2026-09-14): a BUY only fires if the price is ALSO above a much
   longer moving average (trend_window - typically 1-3 weeks, well beyond the crossover windows).
   This is what keeps the bot from buying into a confirmed downtrend just because the short-term
   crossover flickered bullish - a plain crossover strategy repeatedly whipsaws (buys a bounce,
   gets stopped out, buys the next bounce...) during a sustained decline, which is exactly what
   hurt this strategy over the trailing ~1-year crypto crash. The trend filter sits the bot out in
   cash instead of trading against the macro trend, and was added specifically to give the bot a
   better chance in current (not just historically-averaged) market conditions. It never blocks a
   SELL/exit - only new entries.

This module only ever sees data that already passed data_validator.py - data-quality concerns are
handled entirely upstream, so this stays focused on the signal math.

Two entry points share one decision core (decide()):
- generate_signals(): live use, one decision per symbol from the latest bars - called once an
  hour by trader.py, cheap regardless of implementation.
- sma_series() + decide(): backtest.py/tune.py precompute the WHOLE rolling-average history for
  each symbol in one O(n) pass (a running sum, not "resum the whole window every hour") and feed
  each point through the exact same decide() function trader.py uses. This matters at this
  project's scale: a multi-year hourly backtest is tens of thousands of timesteps, and naively
  resumming a window every step would make tune.py's parameter sweep impractically slow. The two
  paths compute identical numbers (same math, just done efficiently) and share the identical
  decision rule - "replays through the exact same strategy code" stays true even though the SMA
  plumbing underneath is optimized for the backtest's scale.
"""
import math
from dataclasses import dataclass

from crypto_broker import Bar

# Scaled so roughly a 4% SMA spread reaches full (100) confidence - kept from the stock bot's
# mapping since it's an arbitrary-but-reasonable scale, not something crypto specifically needs
# to change; the SMA windows and hourly granularity are what do.
CONFIDENCE_SCALE = 25


@dataclass
class TradeDecision:
    symbol: str
    action: str  # BUY / SELL / HOLD
    size_pct: float
    confidence: float
    reasoning: str
    short_sma: float
    long_sma: float


def sma_series(closes: list[float], window: int) -> list[float]:
    """Rolling simple moving average over the whole list, computed in O(n) via a running sum
    instead of resumming the window at every point. Entries before `window` values are available
    are NaN. `sma_series(closes, w)[i]` equals `sum(closes[i-w+1:i+1]) / w` - same result as the
    naive approach, just computed once instead of redundantly on every timestep.
    """
    n = len(closes)
    result = [math.nan] * n
    if n < window:
        return result
    running_sum = sum(closes[:window])
    result[window - 1] = running_sum / window
    for i in range(window, n):
        running_sum += closes[i] - closes[i - window]
        result[i] = running_sum / window
    return result


def decide(
    symbol: str,
    short_sma: float,
    long_sma: float,
    has_position: bool,
    short_window: int,
    long_window: int,
    trend_ok: bool = True,
) -> TradeDecision:
    """`trend_ok` is the macro trend filter's verdict (price above the long-term trend average) -
    defaults to True so existing callers that don't pass it behave exactly as before. It only
    ever blocks a BUY; a SELL/exit fires on the crossover alone regardless of trend_ok, since
    exiting a position should never be gated by an extra filter.
    """
    spread_pct = (short_sma - long_sma) / long_sma * 100
    confidence = min(100.0, abs(spread_pct) * CONFIDENCE_SCALE)

    if spread_pct > 0 and trend_ok:
        action = "BUY"
        reasoning = (
            f"{short_window}h avg (${short_sma:.4f}) is {spread_pct:.2f}% above "
            f"{long_window}h avg (${long_sma:.4f}) - bullish crossover, price above the long-term trend filter"
        )
    elif spread_pct > 0 and not trend_ok:
        action = "HOLD"
        confidence = 0.0
        reasoning = (
            f"{short_window}h avg is {spread_pct:.2f}% above {long_window}h avg (bullish crossover), "
            f"but price is below the long-term trend filter - sitting out a confirmed downtrend"
        )
    elif spread_pct < 0 and has_position:
        action = "SELL"
        reasoning = (
            f"{short_window}h avg (${short_sma:.4f}) is {abs(spread_pct):.2f}% below "
            f"{long_window}h avg (${long_sma:.4f}) - bearish crossover, exiting position"
        )
    else:
        action = "HOLD"
        reasoning = f"{short_window}h avg {spread_pct:+.2f}% vs {long_window}h avg - no actionable signal"

    return TradeDecision(symbol, action, 100.0, confidence, reasoning, short_sma, long_sma)


def generate_signals(
    bars_by_symbol: dict[str, list[Bar]],
    positions: dict,
    short_window: int,
    long_window: int,
    trend_window: int,
) -> list[TradeDecision]:
    decisions = []
    for symbol, bars in bars_by_symbol.items():
        closes = [b.close for b in bars]
        short_sma = sum(closes[-short_window:]) / short_window
        long_sma = sum(closes[-long_window:]) / long_window
        trend_sma = sum(closes[-trend_window:]) / trend_window
        trend_ok = closes[-1] >= trend_sma
        has_position = symbol in positions and positions[symbol].qty > 0
        decisions.append(decide(symbol, short_sma, long_sma, has_position, short_window, long_window, trend_ok))
    return decisions
