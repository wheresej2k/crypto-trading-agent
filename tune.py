"""Sweeps combinations of strategy parameters (SMA windows, stop-loss/take-profit, confidence
threshold) through the backtest to find the most profitable combination that still passes a
safety filter, instead of just the most profitable combination outright (which tends to mean
"took on more risk and got lucky").

IMPORTANT CAVEAT: picking whatever scored best on historical data is "curve-fitting" - there's a
real risk of tuning to noise that happened to exist in that stretch of history rather than to
anything that will keep working. Treat the winner here as a hypothesis to try on paper, not a
proven result. As a partial check against this, every combination is tested across three
different historical windows (~3 months, ~1 year, ~5 years - close to the full history Alpaca has
for crypto, which starts 2021-01-01) - a combination only counts as "safe" if it held up in ALL
of them, not just one.

"Safe" here specifically means, across every tested window:
  - it was never net-unprofitable (return >= 0%)
  - its worst drawdown never exceeded SAFE_MAX_DRAWDOWN_PCT
  - its win rate never fell below MIN_WIN_RATE_PCT
These three numbers ARE this project's quantified "what does working mean" bar from the README -
change them here to make the bar stricter or looser.

Deliberate design choice: this sweep only touches STRATEGY parameters (the SMA windows,
stop-loss/take-profit, confidence threshold) - it never touches max_position_pct,
max_total_exposure_pct, or max_daily_loss_pct. Those three are your risk-tolerance choice, not a
"what wins backtests" question, and an automated process silently raising how much of your
account it's willing to risk - even in paper trading - is exactly the kind of self-modifying-risk
behavior this project's design brief calls out as needing a human decision, not an algorithm. If
you want to change your risk tier, do that deliberately in config/params.json yourself.

Usage:
    python tune.py
"""
import dataclasses

from alpaca.data.historical import CryptoHistoricalDataClient

from backtest import fetch_all_bars, simulate
from config import load_settings

SHORT_WINDOWS = [6, 12, 24]      # hours
LONG_WINDOWS = [24, 72, 168]     # hours (1 day, 3 days, 1 week)
STOP_LOSS_PCTS = [6, 10, 15]
TAKE_PROFIT_PCTS = [10, 18, 25]
MIN_CONFIDENCES = [40, 55]

# The safety bar - a combination must never have lost money, never drawn down worse than this,
# and never had a win rate below this, in ANY of the three windows tested, to count at all.
# Adjusted 2026-09-14 from an initial guess (-22%/40%) to numbers grounded in what the real
# ~5-year sweep actually showed was achievable - nothing survived the 2022 crash under the
# original bar. The user explicitly approved this loosening after seeing the near-miss data
# (see tune.py's diagnose() output) - this is not an automated change.
SAFE_MAX_DRAWDOWN_PCT = -32.0
MIN_WIN_RATE_PCT = 35.0

WINDOWS_TO_TEST = [("~3 months", 24 * 90), ("~1 year", 24 * 365), ("~5 years", 24 * 1825)]


def build_combos():
    return [
        (sw, lw, sl, tp, mc)
        for sw in SHORT_WINDOWS
        for lw in LONG_WINDOWS
        for sl in STOP_LOSS_PCTS
        for tp in TAKE_PROFIT_PCTS
        for mc in MIN_CONFIDENCES
        if sw < lw
    ]


def sweep(base_settings, data_client, bars_by_symbol=None):
    """Runs every combo across every test window. Returns one row per combo: params plus
    per-window return/drawdown/win-rate. `bars_by_symbol` can be passed in pre-fetched (the
    monthly auto-retune workflow does this to fetch history exactly once).
    """
    max_hours = max(hours for _, hours in WINDOWS_TO_TEST)
    if bars_by_symbol is None:
        print(f"Fetching {max_hours} hours of history once for all combinations...")
        bars_by_symbol = fetch_all_bars(base_settings, data_client, max_hours)

    combos = build_combos()
    print(f"Testing {len(combos)} parameter combinations across {len(WINDOWS_TO_TEST)} time windows "
          f"({len(combos) * len(WINDOWS_TO_TEST)} simulations - this can take a few minutes)...\n")

    results = []
    for sw, lw, sl, tp, mc in combos:
        settings = dataclasses.replace(
            base_settings,
            short_sma_window=sw, long_sma_window=lw,
            stop_loss_pct=sl, take_profit_pct=tp, min_confidence=mc,
        )
        window_returns, window_drawdowns, window_winrates = {}, {}, {}
        for label, hours in WINDOWS_TO_TEST:
            trimmed_bars = {s: bars[-hours:] for s, bars in bars_by_symbol.items()}
            r = simulate(settings, trimmed_bars)
            window_returns[label] = r["total_return_pct"] if r else None
            window_drawdowns[label] = r["max_drawdown_pct"] if r else None
            window_winrates[label] = r["win_rate_pct"] if r else None

        results.append((sw, lw, sl, tp, mc, window_returns, window_drawdowns, window_winrates))

    return results


def is_safe(row):
    window_returns, window_drawdowns, window_winrates = row[-3], row[-2], row[-1]
    returns = [v for v in window_returns.values() if v is not None]
    drawdowns = [v for v in window_drawdowns.values() if v is not None]
    winrates = list(window_winrates.values())
    if len(returns) < len(WINDOWS_TO_TEST):
        return False
    if any(v is None for v in winrates):
        # A window with zero completed round-trip trades has no meaningful win rate - treat that
        # as insufficient evidence rather than letting it silently pass or fail the bar.
        return False
    return min(returns) >= 0 and min(drawdowns) >= SAFE_MAX_DRAWDOWN_PCT and min(winrates) >= MIN_WIN_RATE_PCT


def avg_return(row):
    window_returns = row[-3]
    values = [v for v in window_returns.values() if v is not None]
    return sum(values) / len(values) if values else -999


def rank_safe_combos(results):
    safe_results = [r for r in results if is_safe(r)]
    safe_results.sort(key=avg_return, reverse=True)
    return safe_results


def diagnose(results):
    """When nothing passes the safety filter, a bare 'nothing passed' isn't enough to act on -
    this reports which specific criterion is the bottleneck (profitability, drawdown, or win
    rate) and shows the closest near-misses, so there's something to actually decide from instead
    of just a dead end.
    """
    profitable_count = 0
    drawdown_ok_count = 0
    winrate_ok_count = 0
    for row in results:
        window_returns, window_drawdowns, window_winrates = row[-3], row[-2], row[-1]
        returns = [v for v in window_returns.values() if v is not None]
        drawdowns = [v for v in window_drawdowns.values() if v is not None]
        winrates = [v for v in window_winrates.values() if v is not None]
        if len(returns) == len(WINDOWS_TO_TEST) and min(returns) >= 0:
            profitable_count += 1
        if drawdowns and min(drawdowns) >= SAFE_MAX_DRAWDOWN_PCT:
            drawdown_ok_count += 1
        if len(winrates) == len(WINDOWS_TO_TEST) and all(v is not None for v in winrates) and min(winrates) >= MIN_WIN_RATE_PCT:
            winrate_ok_count += 1

    total = len(results)
    print(f"Of {total} combinations tested:")
    print(f"  {profitable_count} were profitable in every window")
    print(f"  {drawdown_ok_count} stayed within the {SAFE_MAX_DRAWDOWN_PCT:.0f}% drawdown limit in every window")
    print(f"  {winrate_ok_count} met the {MIN_WIN_RATE_PCT:.0f}% win-rate floor in every window")
    print("(a combination needs all three to count as 'safe' - whichever count above is lowest is the actual bottleneck)\n")

    print("Closest near-misses (best average return regardless of safety, for comparison):")
    header = (
        f"{'short':>5} {'long':>5} {'stop%':>6} {'tp%':>5} {'minconf':>7}  "
        + "  ".join(f"{label:>26}" for label, _ in WINDOWS_TO_TEST)
    )
    print(header)
    by_return = sorted(results, key=avg_return, reverse=True)
    for sw, lw, sl, tp, mc, window_returns, window_drawdowns, window_winrates in by_return[:10]:
        row = "  ".join(
            f"{window_returns[label]:+7.2f}% (dd {window_drawdowns[label]:5.2f}%, wr "
            f"{window_winrates[label]:.1f}%)" if window_winrates[label] is not None else
            f"{window_returns[label]:+7.2f}% (dd {window_drawdowns[label]:5.2f}%, wr  n/a)"
            for label, _ in WINDOWS_TO_TEST
        )
        print(f"{sw:>5} {lw:>5} {sl:>6} {tp:>5} {mc:>7}  {row}")


def main():
    base_settings = load_settings()
    data_client = CryptoHistoricalDataClient(base_settings.alpaca_api_key, base_settings.alpaca_secret_key)

    results = sweep(base_settings, data_client)
    safe_results = rank_safe_combos(results)

    print(f"{len(safe_results)} of {len(results)} combinations passed the safety filter "
          f"(profitable in every window, drawdown no worse than {SAFE_MAX_DRAWDOWN_PCT:.0f}%, "
          f"win rate at least {MIN_WIN_RATE_PCT:.0f}% in every window).\n")

    if not safe_results:
        print("No combination passed the safety filter.\n")
        diagnose(results)
        return

    header = (
        f"{'short':>5} {'long':>5} {'stop%':>6} {'tp%':>5} {'minconf':>7}  "
        + "  ".join(f"{label:>26}" for label, _ in WINDOWS_TO_TEST)
    )
    print(header)
    for sw, lw, sl, tp, mc, window_returns, window_drawdowns, window_winrates in safe_results[:15]:
        row = "  ".join(
            f"{window_returns[label]:+7.2f}% (dd {window_drawdowns[label]:5.2f}%, wr {window_winrates[label]:4.1f}%)"
            for label, _ in WINDOWS_TO_TEST
        )
        print(f"{sw:>5} {lw:>5} {sl:>6} {tp:>5} {mc:>7}  {row}")

    best = safe_results[0]
    sw, lw, sl, tp, mc = best[:5]
    print("\nMost profitable combination that still passed the safety filter:")
    print(f"  short={sw}h, long={lw}h, stop_loss={sl}%, take_profit={tp}%, min_confidence={mc}")
    print("Running this script never changes anything live on its own - this only updates")
    print("config/params.json if you copy these values in yourself, or approve the automated")
    print("monthly retune workflow's pull request. See the caveat at the top of this file.")


if __name__ == "__main__":
    main()
