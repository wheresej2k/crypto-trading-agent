"""RELIABLE + SELF-IMPROVING pillars: the crypto-specific 'software bracket'. Alpaca doesn't
support bracket orders for crypto (confirmed against their docs - see crypto_broker.py), so this
module builds the same protection itself: a resting GTC take-profit limit sell for the whole
position, placed right after a buy fills, plus a stop-loss the bot enforces on every run. Two
resting orders can't both cover the full position - Alpaca reserves each sell order's qty and has
no OCO for crypto (see open_bracket()).

State survives between GitHub Actions runs (each run is a fresh, disposable machine) by
persisting to state/open_brackets.json, which the workflow commits back to the repo after every
run - the same pattern the trade log itself uses.

Because the take-profit rests on Alpaca's own exchange, it catches a spike 24/7, even between
scheduled runs. The stop-loss is only checked once per run (hourly), so in a fast crash it can
fill below the stop price - it is NOT continuous protection.
"""
import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

STATE_PATH = Path(__file__).parent / "state" / "open_brackets.json"

FILL_POLL_ATTEMPTS = 6
FILL_POLL_DELAY_SECONDS = 5


@dataclass
class Bracket:
    trade_id: str
    symbol: str
    qty: float
    entry_price: float
    stop_price: float
    target_price: float
    # The resting take-profit limit sell on Alpaca, or "" if it couldn't be placed (reconcile()
    # retries it, and enforces the target in software until then). There is no resting stop
    # order any more - see open_bracket() for why.
    target_order_id: str
    opened_at: str
    # Highest price seen at a run since entry - drives the optional trailing stop.
    high_water: float = 0.0


def load_brackets() -> dict[str, Bracket]:
    if not STATE_PATH.exists():
        return {}
    with open(STATE_PATH) as f:
        raw = json.load(f)
    # Older state files carry a stop_order_id from the two-resting-orders design.
    return {
        symbol: Bracket(**{k: v for k, v in b.items() if k != "stop_order_id"})
        for symbol, b in raw.items()
    }


def save_brackets(brackets: dict[str, Bracket]):
    STATE_PATH.parent.mkdir(exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump({symbol: asdict(b) for symbol, b in brackets.items()}, f, indent=2)


def stop_level(entry_price: float, stop_price: float, high_water: float,
               trailing_stop_pct: float, trail_activation_pct: float) -> tuple[float, str]:
    """The price at/below which the position must be sold, and why. Shared by reconcile() and
    backtest.simulate() so live and backtest apply the identical exit rule.

    The trailing stop only arms once the high-water mark is trail_activation_pct above entry,
    and never lowers the fixed stop."""
    if trailing_stop_pct > 0 and high_water >= entry_price * (1 + trail_activation_pct / 100):
        trail = high_water * (1 - trailing_stop_pct / 100)
        if trail > stop_price:
            return trail, "trailing_stop"
    return stop_price, "stop_loss"


def wait_for_fill(broker, order_id: str):
    """Polls an order (up to ~30s) until it fills. Returns the filled order, or None if it never
    filled in the poll window - rare for a market order on a liquid pair, but real orders can
    always be left pending on Alpaca's side if something goes wrong; callers must handle None
    rather than assume a fill happened.
    """
    for _ in range(FILL_POLL_ATTEMPTS):
        current = broker.get_order(order_id)
        if current.status.value == "filled":
            return current
        time.sleep(FILL_POLL_DELAY_SECONDS)
    return None


def open_bracket(broker, symbol: str, notional_usd: float, stop_loss_pct: float, take_profit_pct: float):
    """Places the market buy, waits for it to fill, then places the resting take-profit.

    Returns (Bracket, warning_or_None) once the buy has filled - a filled buy is ALWAYS returned
    as a tracked bracket, even if the protective order couldn't be placed, because it's a real
    position. Returns (None, error_string) only if the buy never filled in the poll window (the
    order is then left open on Alpaca and needs a manual look - see README).

    Only ONE resting order is placed, covering the whole position. Alpaca reserves each sell
    order's qty against the balance and has no OCO for crypto, so a stop AND a target can't both
    cover the full position - the second is rejected "insufficient balance". The take-profit is
    the one that rests on the exchange (a spike can come and go between hourly runs); the
    stop-loss is enforced by reconcile() on every run.

    The qty is Alpaca's own qty_available, not filled_qty: Alpaca takes its fee out of the coin,
    so filled_qty is ~0.25% MORE than is actually held and an order for it is rejected. That
    rejection used to be raised out of here AFTER the buy had filled - the trade was logged as a
    failed BUY, left unprotected and untracked, and the next run "bought" the few hundred dollars
    of headroom left under max_position_pct. Only that ~2% sliver ever got a take-profit.
    """
    order = broker.buy_notional(symbol, notional_usd)
    filled = wait_for_fill(broker, order.id)

    if filled is None:
        return None, (
            f"buy order {order.id} did not fill within "
            f"{FILL_POLL_ATTEMPTS * FILL_POLL_DELAY_SECONDS}s - still pending on Alpaca, no "
            f"protective orders placed. Check the Alpaca dashboard."
        )

    entry_price = float(filled.filled_avg_price)
    bracket = Bracket(
        trade_id=str(uuid.uuid4())[:8],
        symbol=symbol,
        qty=float(filled.filled_qty),
        entry_price=entry_price,
        stop_price=entry_price * (1 - stop_loss_pct / 100),
        # 0 = no take-profit (take_profit_pct set to 0).
        target_price=entry_price * (1 + take_profit_pct / 100) if take_profit_pct > 0 else 0.0,
        target_order_id="",
        opened_at=filled.filled_at.isoformat() if filled.filled_at else "",
        high_water=entry_price,
    )
    return bracket, place_target(broker, bracket)


def place_target(broker, b: Bracket) -> str | None:
    """Places the resting take-profit for everything Alpaca says is available to sell. Returns
    None on success or a warning string - never raises, so a filled position can't drop out of
    tracking the way it used to.
    """
    if not b.target_price:
        return None
    try:
        qty = broker.available_qty(b.symbol)
        if qty is None:
            return "position not visible on Alpaca yet - take-profit will be placed next run"
        # Suffix keeps client_order_id unique when a canceled target is re-placed.
        client_id = f"target-{b.trade_id}-{uuid.uuid4().hex[:4]}"
        order = broker.place_take_profit(b.symbol, qty, b.target_price, client_order_id=client_id)
        b.target_order_id = str(order.id)
        b.qty = float(qty)
        return None
    except Exception as e:
        return f"take-profit not placed ({e}) - retrying next run, target enforced in software until then"


def _exit_now(broker, b: Bracket, price: float) -> float:
    """Market-sells the whole position. Returns the fill price, or `price` if the fill wasn't
    confirmed inside the poll window."""
    if b.target_order_id:
        broker.cancel_order(b.target_order_id)
    broker.cancel_open_orders_for(b.symbol)
    qty = broker.available_qty(b.symbol)
    if qty is None:
        raise RuntimeError(f"no {b.symbol} position on Alpaca to sell")
    order = broker.sell_qty(b.symbol, qty)
    filled = wait_for_fill(broker, order.id)
    return float(filled.filled_avg_price) if filled else price


def reconcile(broker, brackets: dict[str, Bracket], price_of,
              trailing_stop_pct: float = 0.0, trail_activation_pct: float = 0.0) -> tuple[dict[str, Bracket], list[dict]]:
    """Checks every tracked bracket against the exchange and the latest price (`price_of(symbol)`):
    - resting take-profit filled -> closed at the target;
    - price at/below the stop (fixed, or trailing - see stop_level) -> cancel the take-profit and market-sell (the software stop-loss);
    - take-profit missing (never placed, or canceled) -> enforce the target in software, and try
      placing it again.
    Returns the still-open brackets plus 'close events' - this is where the SELF-IMPROVING
    pillar's 'what happened after' gets captured, feeding trade_log.py's full-lifecycle record.
    A failure on one bracket is printed and that bracket kept; it never stops the run.
    """
    still_open = {}
    close_events = []

    for symbol, b in brackets.items():
        try:
            target_status = broker.get_order(b.target_order_id).status.value if b.target_order_id else None
            if target_status == "filled":
                close_events.append(build_close_event(b, "take_profit", b.target_price))
                continue
            if target_status in ("canceled", "expired", "rejected"):
                b.target_order_id = ""
            if broker.available_qty(symbol) is None:
                # Sold outside the bot (e.g. by hand on Alpaca's site) - nothing left to protect.
                print(f"    WARNING: {symbol} bracket {b.trade_id} has no position on Alpaca any more - dropping it")
                continue

            price = price_of(symbol)
            b.high_water = max(b.high_water or b.entry_price, price)
            level, reason = stop_level(b.entry_price, b.stop_price, b.high_water, trailing_stop_pct, trail_activation_pct)
            if price <= level:
                close_events.append(build_close_event(b, reason, _exit_now(broker, b, price)))
                continue
            if b.target_price and not b.target_order_id and price >= b.target_price:
                close_events.append(build_close_event(b, "take_profit", _exit_now(broker, b, price)))
                continue
            if b.target_price and not b.target_order_id:
                warning = place_target(broker, b)
                if warning:
                    print(f"    WARNING {symbol}: {warning}")
        except Exception as e:
            print(f"    WARNING: could not reconcile {symbol}: {e}")
        still_open[symbol] = b

    return still_open, close_events


ADOPT_MIN_VALUE_USD = 10.0


def adopt_untracked(broker, brackets: dict[str, Bracket], positions: dict, watchlist: list[str],
                    stop_loss_pct: float, take_profit_pct: float) -> list[tuple[str, str]]:
    """Any position Alpaca holds with no bracket gets one, priced from Alpaca's average entry.

    The state file is not a reliable record of what the account holds - it has lost positions
    before (2026-09-19: LINK and SOL held, untracked, with orphaned stop orders reserving half of
    each), and a filled buy that errored afterwards used to drop out of tracking entirely. An
    untracked position has no take-profit or stop, and the bot never adds to it. Adopting it every
    run makes all of those self-healing. Leftover dust under ADOPT_MIN_VALUE_USD is ignored.
    Returns (symbol, message) per position adopted or failed.
    """
    messages = []
    for symbol, pos in positions.items():
        if symbol in brackets or symbol not in watchlist or pos.market_value < ADOPT_MIN_VALUE_USD:
            continue
        try:
            # Orphaned resting orders would reserve the qty the take-profit needs.
            broker.cancel_open_orders_for(symbol)
            entry = pos.avg_entry_price
            b = Bracket(
                trade_id=str(uuid.uuid4())[:8],
                symbol=symbol,
                qty=pos.qty,
                entry_price=entry,
                stop_price=entry * (1 - stop_loss_pct / 100),
                target_price=entry * (1 + take_profit_pct / 100) if take_profit_pct > 0 else 0.0,
                target_order_id="",
                opened_at="",
                high_water=entry,
            )
            warning = place_target(broker, b)
            brackets[symbol] = b
            messages.append((symbol, f"adopted untracked position (${pos.market_value:,.2f}, entry {entry})"
                            + (f" - {warning}" if warning else "")))
        except Exception as e:
            messages.append((symbol, f"could not adopt untracked position: {e}"))
    return messages


def cancel_bracket(broker, brackets: dict[str, Bracket], symbol: str) -> tuple[dict[str, Bracket], Bracket | None]:
    """Used when the strategy itself generates a SELL signal (bearish crossover) on a symbol that
    still has open protective orders. Those resting orders reserve the position's quantity on
    Alpaca's side, so they must be canceled BEFORE a separate signal-driven market sell is
    submitted - otherwise the sell would be rejected for insufficient available quantity.

    Returns the removed Bracket (so the caller can log its real entry_price against whatever
    price the signal-driven sell actually fills at - see trader.py) or None if this symbol had
    no tracked bracket.
    """
    b = brackets.get(symbol)
    if b is None:
        return brackets, None

    if b.target_order_id:
        broker.cancel_order(b.target_order_id)

    remaining = {s: br for s, br in brackets.items() if s != symbol}
    return remaining, b


def build_close_event(bracket: Bracket, exit_reason: str, exit_price: float) -> dict:
    pl_pct = (exit_price - bracket.entry_price) / bracket.entry_price * 100
    return {
        "symbol": bracket.symbol, "trade_id": bracket.trade_id, "exit_reason": exit_reason,
        "entry_price": bracket.entry_price, "exit_price": exit_price, "pl_pct": pl_pct,
    }
