"""Backtests the SMA-crossover strategy against real historical hourly crypto bars, so you can
see how it would have performed over months/years in a few seconds - instead of waiting for the
live bot to trade one hour at a time. Uses the exact same decision rule (strategy.decide) and the
exact same risk_manager.evaluate_decisions() as the live bot - this reflects the real rules, not
a separate rosier simulation.

It also mirrors the real exits (see position_tracker.py): the resting take-profit fills the
instant an hourly bar's high touches it; the stop-loss (fixed or trailing) is checked once an
hour against the close and fills there. Alpaca's fees are charged on every fill.

Usage:
    python backtest.py                 # last 8760 hours (~1 year)
    python backtest.py --hours 43800   # ~5 years - close to the full history Alpaca has for crypto
"""
import argparse
from datetime import datetime, timedelta, timezone

import math

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import load_settings
from crypto_broker import Bar, PositionSnapshot
from risk_manager import evaluate_decisions
from position_tracker import stop_level
from strategy import decide, sma_series

STARTING_CASH = 100_000.0

# Alpaca crypto fees at the lowest volume tier, taken out of what you receive. 0.25% matches the
# live fills exactly (e.g. 125999.988 DOGE received for an order of 126315.777).
TAKER_FEE = 0.0025  # market orders: entries, signal exits, stops
MAKER_FEE = 0.0015  # the resting take-profit limit order

# Alpaca's history has a few missing hours here and there (harmless) and occasional holes of
# months. A gap longer than this breaks an SMA's continuity and force-closes a held position.
MAX_BAR_GAP = timedelta(hours=24)


def last_hours(bars_by_symbol, hours):
    """The trailing `hours` of history by CLOCK TIME, for every symbol. Slicing `bars[-hours:]`
    by count instead reaches back much further for a symbol with holes in its data (SOL/USD's
    last 43,800 bars start in April 2021, six months before everyone else's)."""
    latest = max(bars[-1].timestamp for bars in bars_by_symbol.values() if bars)
    cutoff = latest - timedelta(hours=hours)
    return {s: [b for b in bars if b.timestamp > cutoff] for s, bars in bars_by_symbol.items()}


def fetch_hourly_bars(data_client, symbol, hours):
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Hour, start=start)
    raw = list(data_client.get_crypto_bars(req)[symbol])
    return [
        Bar(b.timestamp, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume))
        for b in raw
    ]


def fetch_all_bars(settings, data_client, hours):
    """Fetches history for every watchlist symbol once, so it can be reused across many
    simulate() calls with different settings (e.g. tune.py's parameter sweep) without re-hitting
    the API for every combination.
    """
    bars_by_symbol = {}
    for symbol in settings.watchlist:
        bars = fetch_hourly_bars(data_client, symbol, hours)
        if len(bars) < hours // 2:
            print(f"  WARNING: not enough history for {symbol} ({len(bars)} hours), skipping")
            continue
        bars_by_symbol[symbol] = bars
    if not bars_by_symbol:
        raise SystemExit("No symbols had enough historical data to backtest.")
    return bars_by_symbol


def run_backtest(settings, data_client, hours):
    bars_by_symbol = fetch_all_bars(settings, data_client, hours)
    return simulate(settings, bars_by_symbol)


def _valid_from(bars, required_window):
    """For each bar index, whether the `required_window` bars ending there are one contiguous
    stretch of data. Alpaca's history has holes - SOL/USD is missing ~14 months (10,092 hours) -
    and an SMA that averages across a hole mixes prices more than a year apart."""
    valid = [False] * len(bars)
    run = 0
    for i, b in enumerate(bars):
        run = run + 1 if i and b.timestamp - bars[i - 1].timestamp <= MAX_BAR_GAP else 1
        valid[i] = run >= required_window
    return valid


def simulate(settings, bars_by_symbol):
    """Steps through one shared hourly CLOCK, looking each symbol's bar up by timestamp.

    The previous version stepped by list index (bar i of every symbol at once). That is only
    correct if every symbol has a bar for every hour - and SOL/USD's 10,092-hour hole meant the
    ~5-year window paired SOL's 2021 prices with BTC's 2022 prices, then jumped SOL across the
    hole. That window's numbers (the ones config/params.json was tuned on) were mostly artifact.
    """
    required_window = max(settings.long_sma_window, settings.trend_window)
    bars_by_symbol = {s: bars for s, bars in bars_by_symbol.items() if len(bars) >= required_window + 1}
    if not bars_by_symbol:
        return None

    # Precompute the full rolling SMA history once per symbol (see strategy.sma_series) instead
    # of recomputing a window sum at every timestep - this is what keeps a multi-year hourly
    # sweep in tune.py fast rather than impractically slow.
    closes_by_symbol = {s: [b.close for b in bars] for s, bars in bars_by_symbol.items()}
    short_sma_by_symbol = {s: sma_series(c, settings.short_sma_window) for s, c in closes_by_symbol.items()}
    long_sma_by_symbol = {s: sma_series(c, settings.long_sma_window) for s, c in closes_by_symbol.items()}
    trend_sma_by_symbol = {s: sma_series(c, settings.trend_window) for s, c in closes_by_symbol.items()}
    valid_by_symbol = {s: _valid_from(bars, required_window) for s, bars in bars_by_symbol.items()}
    index_by_symbol = {s: {b.timestamp: i for i, b in enumerate(bars)} for s, bars in bars_by_symbol.items()}

    # Start once at least one symbol can trade, so the SMA warm-up isn't counted as idle time.
    tradeable_from = [bars_by_symbol[s][v.index(True)].timestamp for s, v in valid_by_symbol.items() if True in v]
    if not tradeable_from:
        return None
    clock = sorted({b.timestamp for bars in bars_by_symbol.values() for b in bars if b.timestamp >= min(tradeable_from)})

    cash = STARTING_CASH
    positions: dict[str, dict] = {}
    last_close: dict[str, float] = {}
    last_seen: dict[str, object] = {}
    trade_count = 0
    win_count = 0
    loss_count = 0
    equity_curve = []
    prev_equity = STARTING_CASH
    day_start_equity = STARTING_CASH
    current_day = None
    exit_reasons = {}

    def close_position(symbol, exit_price, fee_rate, reason):
        nonlocal cash, win_count, loss_count, trade_count
        pos = positions.pop(symbol)
        proceeds = pos["qty"] * exit_price * (1 - fee_rate)
        cash += proceeds
        # Win/loss on what was actually paid (fees included), so fee drag can't hide as "wins".
        if proceeds > pos["cost"]:
            win_count += 1
        else:
            loss_count += 1
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        trade_count += 1

    for now in clock:
        bars_now = {}
        for s in bars_by_symbol:
            i = index_by_symbol[s].get(now)
            if i is not None:
                bars_now[s] = (i, bars_by_symbol[s][i])
                last_close[s] = bars_by_symbol[s][i].close
                last_seen[s] = now

        # A held symbol whose data has gone quiet for longer than a normal gap: close it at the
        # last price seen, rather than carrying it silently across a months-long hole.
        for symbol in list(positions.keys()):
            if symbol not in bars_now and now - last_seen[symbol] > MAX_BAR_GAP:
                close_position(symbol, last_close[symbol], TAKER_FEE, "data_gap")

        # Exits, in the order the live bot sees them (position_tracker.reconcile):
        # 1. The resting take-profit limit order fills the instant the bar's high touches it.
        # 2. The stop (fixed or trailing - position_tracker.stop_level) is a software check run
        #    once an hour against the latest price, so it triggers on the hourly CLOSE and fills
        #    there, not at the stop price - a gap down costs the full gap, as it would live.
        for symbol in list(positions.keys()):
            if symbol not in bars_now:
                continue
            bar_now = bars_now[symbol][1]
            pos = positions[symbol]
            if pos["target_price"] and bar_now.high >= pos["target_price"]:
                close_position(symbol, pos["target_price"], MAKER_FEE, "take_profit")
                continue
            pos["high_water"] = max(pos["high_water"], bar_now.close)
            level, reason = stop_level(pos["entry_price"], pos["stop_price"], pos["high_water"],
                                       settings.trailing_stop_pct, settings.trail_activation_pct)
            if bar_now.close <= level:
                close_position(symbol, bar_now.close, TAKER_FEE, reason)

        position_snapshots = {}
        for symbol, pos in positions.items():
            price = last_close[symbol]
            unrealized_plpc = (price - pos["entry_price"]) / pos["entry_price"] * 100
            position_snapshots[symbol] = PositionSnapshot(symbol, pos["qty"], pos["qty"] * price, pos["entry_price"], unrealized_plpc)

        equity = cash + sum(p.market_value for p in position_snapshots.values())
        # Daily P/L against equity at the start of the UTC day, like Alpaca's last_equity -
        # comparing to the previous HOUR meant the daily-loss limit almost never bound here.
        if now.date() != current_day:
            current_day, day_start_equity = now.date(), prev_equity
        day_pl_pct = (equity - day_start_equity) / day_start_equity * 100 if day_start_equity else 0.0

        decisions = []
        for symbol, (i, bar_now) in bars_now.items():
            if not valid_by_symbol[symbol][i]:
                continue
            trend_ok = bar_now.close >= trend_sma_by_symbol[symbol][i]
            decisions.append(decide(
                symbol, short_sma_by_symbol[symbol][i], long_sma_by_symbol[symbol][i],
                symbol in position_snapshots, settings.short_sma_window, settings.long_sma_window, trend_ok,
            ))

        approved_buys, approved_sells, _ = evaluate_decisions(decisions, settings, equity, cash, position_snapshots, day_pl_pct)

        # Signal exits always sell the whole position (strategy.decide uses size_pct=100, and
        # trader.py sells Alpaca's full qty_available).
        for sell in approved_sells:
            if sell.symbol in positions:
                close_position(sell.symbol, last_close[sell.symbol], TAKER_FEE, "signal_exit")

        for buy in approved_buys:
            # Mirrors trader.py's "one bracket per symbol at a time" rule.
            if buy.symbol in positions:
                continue
            price = last_close[buy.symbol]
            cash -= buy.notional_usd
            positions[buy.symbol] = {
                # Alpaca takes the fee out of the coin received.
                "qty": buy.notional_usd / price * (1 - TAKER_FEE),
                "cost": buy.notional_usd,
                "entry_price": price,
                "high_water": price,
                "stop_price": price * (1 - settings.stop_loss_pct / 100),
                "target_price": price * (1 + settings.take_profit_pct / 100) if settings.take_profit_pct > 0 else 0.0,
            }
            trade_count += 1

        prev_equity = equity
        equity_curve.append(equity)

    final_equity = equity_curve[-1] if equity_curve else STARTING_CASH
    total_return_pct = (final_equity - STARTING_CASH) / STARTING_CASH * 100

    # Equal-weight buy-and-hold over the same clock, each symbol from its first tradeable bar.
    per_symbol_alloc = STARTING_CASH / len(bars_by_symbol)
    buy_hold_final = 0.0
    for s, bars in bars_by_symbol.items():
        in_range = [b for b in bars if b.timestamp >= clock[0]]
        buy_hold_final += per_symbol_alloc / in_range[0].close * in_range[-1].close if in_range else per_symbol_alloc
    buy_hold_return_pct = (buy_hold_final - STARTING_CASH) / STARTING_CASH * 100

    peak = STARTING_CASH
    max_drawdown_pct = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        max_drawdown_pct = min(max_drawdown_pct, (e - peak) / peak * 100)

    completed_trades = win_count + loss_count
    win_rate_pct = (win_count / completed_trades * 100) if completed_trades else None

    return {
        "symbols_used": list(bars_by_symbol.keys()),
        "hours_simulated": len(clock),
        "starting_equity": STARTING_CASH,
        "final_equity": final_equity,
        "total_return_pct": total_return_pct,
        "buy_hold_return_pct": buy_hold_return_pct,
        "trade_count": trade_count,
        "max_drawdown_pct": max_drawdown_pct,
        "win_rate_pct": win_rate_pct,
        "completed_trades": completed_trades,
        "exit_reasons": exit_reasons,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=8760, help="How many hourly bars of history to simulate (default: 8760 = ~1 year)")
    args = parser.parse_args()

    settings = load_settings()
    data_client = CryptoHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_secret_key)

    print(f"Backtesting {', '.join(settings.watchlist)} over the last {args.hours} hourly bars (~{args.hours / 24 / 365.25:.1f} years)...\n")
    r = run_backtest(settings, data_client, args.hours)

    print(f"Symbols used: {', '.join(r['symbols_used'])}")
    print(f"Hourly bars simulated: {r['hours_simulated']}")
    print(f"Starting equity: ${r['starting_equity']:,.2f}")
    print(f"Final equity:    ${r['final_equity']:,.2f}")
    print(f"Strategy return:      {r['total_return_pct']:+.2f}%")
    print(f"Buy & hold return:    {r['buy_hold_return_pct']:+.2f}%  (equal-weight watchlist, held the whole period)")
    print(f"Worst drawdown:       {r['max_drawdown_pct']:.2f}%")
    print(f"Total trades executed: {r['trade_count']} ({r['completed_trades']} completed round-trips)")
    if r["win_rate_pct"] is not None:
        print(f"Win rate:             {r['win_rate_pct']:.1f}%")
    else:
        print("Win rate:             n/a (no completed round-trip trades)")


if __name__ == "__main__":
    main()
