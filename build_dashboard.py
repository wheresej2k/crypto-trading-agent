"""Builds docs/index.html - a fully static status dashboard for GitHub Pages, generated fresh
after every trading run by hourly-trade.yml. Matches the pattern used by the sibling
crypto-mean-reversion-agent project's own build_dashboard.py.

This replaces the earlier design where a scheduled Claude session cloned the repo, rebuilt a page,
and republished it as a claude.ai Artifact - that worked, but stopped updating the moment the user
was out of Claude usage, since running that routine at all required Claude. Nothing in this script
or in GitHub Pages hosting depends on Claude in any way: GitHub Actions builds this file with plain
Python, and GitHub Pages serves the static result, both for free, indefinitely.

The dashboard renders this bot locally and links directly to the sibling dashboard.
Exchange navigation replaces the page instead of recursively embedding dashboards.

Usage:
    python build_dashboard.py
"""
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from config import load_settings
from crypto_broker import CryptoBroker
from heartbeat import hours_since_last_success
from tune import SAFE_MAX_DRAWDOWN_PCT, MIN_WIN_RATE_PCT

ROOT = Path(__file__).parent
STATE_DIR = ROOT / "state"
LOG_PATH = ROOT / "logs" / "trade_log.csv"
PARAMS_PATH = ROOT / "config" / "params.json"
OUT_PATH = ROOT / "docs" / "index.html"
MAX_LOG_ROWS = 40

# Alpaca paper accounts start funded at $100,000 - used only for "since you started" performance.
STARTING_EQUITY = 100_000.0

SIBLING_DASHBOARD_URL = "https://wheresej2k.github.io/crypto-mean-reversion-agent/"


def load_json(path):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def load_recent_log_rows(path, limit):
    if not path.exists():
        return []
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return list(reversed(rows[-limit:]))


def load_all_log_rows(path):
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def find_matching_buy(all_rows, trade_id):
    if not trade_id:
        return None
    for r in all_rows:
        if r.get("trade_id") == trade_id and r.get("action") == "BUY" and r.get("status") == "executed":
            return r
    return None


def fmt_money(v):
    return f"${v:,.2f}"


def fmt_pct(v, decimals=2):
    if v is None:
        return "n/a"
    return f"{v:+.{decimals}f}%"


def esc(s):
    if s is None:
        return ""
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def utc_span(timestamp_str):
    if not timestamp_str:
        return "n/a"
    return f'<span class="local-time" data-utc="{esc(timestamp_str)}">{esc(timestamp_str)} UTC</span>'


def build_positions_table(positions):
    if not positions:
        return "<p class=\"muted\">No open positions.</p>"
    rows = []
    for symbol, pos in sorted(positions.items()):
        # PositionSnapshot.unrealized_plpc is ALREADY a percentage (crypto_broker converts
        # Alpaca's fraction, and backtest.py builds it the same way). Multiplying again rendered
        # a +1.76% DOGE position as +175.80%. This went unnoticed because the position-visibility
        # bug meant this table was always empty.
        pl_pct = pos.unrealized_plpc
        pl_class = "pos" if pl_pct >= 0 else "neg"
        rows.append(
            "<tr>"
            f"<td>{esc(symbol)}</td>"
            f"<td>{pos.qty:.6f}</td>"
            f"<td>{fmt_money(pos.avg_entry_price)}</td>"
            f"<td>{fmt_money(pos.market_value)}</td>"
            f"<td class=\"{pl_class}\">{fmt_pct(pl_pct)}</td>"
            "</tr>"
        )
    return (
        "<div class=\"table-scroll\"><table><thead><tr><th>Symbol</th><th>Qty</th><th>Avg entry</th>"
        f"<th>Market value</th><th>Unrealized P/L</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def build_last_trade_card(all_rows, positions):
    executed = [r for r in all_rows if r.get("status") == "executed" and r.get("action") in ("BUY", "SELL", "CLOSE")]
    if not executed:
        return "<p class=\"muted\">No trade has been executed yet - every run so far has held or been skipped by the confidence/risk filters.</p>"

    last = executed[-1]
    symbol = last.get("symbol")
    action = last.get("action")
    when = utc_span(last.get("timestamp_utc"))

    if action == "CLOSE":
        buy = find_matching_buy(all_rows, last.get("trade_id"))
        pl_pct = float(last["pl_pct"]) if last.get("pl_pct") else None
        notional = float(buy["amount"]) if buy and buy.get("amount") else None
        dollar_pl = notional * pl_pct / 100 if notional is not None and pl_pct is not None else None
        pl_class = "pos" if (pl_pct or 0) >= 0 else "neg"
        entry_price = float(last["entry_price"]) if last.get("entry_price") else None
        exit_price = float(last["exit_price"]) if last.get("exit_price") else None
        return (
            "<div class=\"cards\">"
            f"<div class=\"card\"><div class=\"label\">Symbol</div><div class=\"value\">{esc(symbol)}</div></div>"
            f"<div class=\"card\"><div class=\"label\">Exit reason</div><div class=\"value\">{esc(last.get('exit_reason'))}</div></div>"
            f"<div class=\"card\"><div class=\"label\">P/L %</div><div class=\"value {pl_class}\">{fmt_pct(pl_pct)}</div></div>"
            f"<div class=\"card\"><div class=\"label\">P/L $</div><div class=\"value {pl_class}\">{fmt_money(dollar_pl) if dollar_pl is not None else 'n/a'}</div></div>"
            "</div>"
            f"<p class=\"muted\">Closed {when} - entry {fmt_money(entry_price) if entry_price is not None else 'n/a'}, "
            f"exit {fmt_money(exit_price) if exit_price is not None else 'n/a'}.</p>"
        )

    if action == "BUY":
        pos = positions.get(symbol)
        amount = last.get("amount")
        if pos:
            dollar_pl = pos.market_value - (pos.qty * pos.avg_entry_price)
            pl_class = "pos" if dollar_pl >= 0 else "neg"
            return (
                "<div class=\"cards\">"
                f"<div class=\"card\"><div class=\"label\">Symbol</div><div class=\"value\">{esc(symbol)}</div></div>"
                "<div class=\"card\"><div class=\"label\">Status</div><div class=\"value\">Open</div></div>"
                f"<div class=\"card\"><div class=\"label\">Unrealized P/L %</div><div class=\"value {pl_class}\">{fmt_pct(pos.unrealized_plpc)}</div></div>"
                f"<div class=\"card\"><div class=\"label\">Unrealized P/L $</div><div class=\"value {pl_class}\">{fmt_money(dollar_pl)}</div></div>"
                "</div>"
                f"<p class=\"muted\">Bought {when} for {fmt_money(float(amount)) if amount else 'n/a'} - still open, "
                "protected by a live stop-loss/take-profit bracket.</p>"
            )
        return (
            f"<p class=\"muted\">Bought {esc(symbol)} {when} for {fmt_money(float(amount)) if amount else 'n/a'} - "
            "no longer showing as an open position (see Realized trades below for how it closed).</p>"
        )

    return (
        f"<p class=\"muted\">Sold {esc(symbol)} {when} (qty {esc(last.get('amount'))}) - {esc(last.get('reasoning'))}</p>"
    )


def build_realized_trades_table(all_rows):
    closed = [r for r in all_rows if r.get("action") == "CLOSE" and r.get("status") == "executed"]
    if not closed:
        return "<p class=\"muted\">No trades have closed yet.</p>", 0.0, 0, 0

    rows_html = []
    total_usd = 0.0
    win_count = 0
    for r in reversed(closed):
        buy = find_matching_buy(all_rows, r.get("trade_id"))
        pl_pct = float(r["pl_pct"]) if r.get("pl_pct") else None
        notional = float(buy["amount"]) if buy and buy.get("amount") else None
        dollar_pl = notional * pl_pct / 100 if notional is not None and pl_pct is not None else None
        entry_price = float(r["entry_price"]) if r.get("entry_price") else None
        exit_price = float(r["exit_price"]) if r.get("exit_price") else None
        if dollar_pl is not None:
            total_usd += dollar_pl
        if pl_pct is not None and pl_pct > 0:
            win_count += 1
        pl_class = "pos" if (pl_pct or 0) >= 0 else "neg"
        rows_html.append(
            "<tr>"
            f"<td>{utc_span(r.get('timestamp_utc'))}</td>"
            f"<td>{esc(r.get('symbol'))}</td>"
            f"<td>{esc(r.get('exit_reason'))}</td>"
            f"<td>{fmt_money(entry_price) if entry_price is not None else 'n/a'}</td>"
            f"<td>{fmt_money(exit_price) if exit_price is not None else 'n/a'}</td>"
            f"<td class=\"{pl_class}\">{fmt_pct(pl_pct) if pl_pct is not None else 'n/a'}</td>"
            f"<td class=\"{pl_class}\">{fmt_money(dollar_pl) if dollar_pl is not None else 'n/a'}</td>"
            "</tr>"
        )
    table = (
        "<div class=\"table-scroll\"><table><thead><tr><th>Time</th><th>Symbol</th><th>Exit reason</th>"
        "<th>Entry</th><th>Exit</th><th>P/L %</th><th>P/L $</th></tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody></table></div>"
    )
    return table, total_usd, len(closed), win_count


def build_log_table(rows):
    if not rows:
        return "<p class=\"muted\">No trade log entries yet.</p>"
    out = []
    for r in rows:
        status = r.get("status", "")
        action = r.get("action", "")
        css_class = "row-buy" if action == "BUY" and status == "executed" else (
            "row-sell" if action in ("SELL", "CLOSE") and status == "executed" else (
                "row-error" if status == "failed" else ""
            )
        )
        out.append(
            f"<tr class=\"{css_class}\">"
            f"<td>{utc_span(r.get('timestamp_utc'))}</td>"
            f"<td>{esc(r.get('symbol'))}</td>"
            f"<td>{esc(action)}</td>"
            f"<td>{esc(status)}</td>"
            f"<td>{esc(r.get('amount'))}</td>"
            f"<td>{esc(r.get('reasoning'))}</td>"
            "</tr>"
        )
    return (
        "<div class=\"table-scroll\"><table><thead><tr><th>Time</th><th>Symbol</th><th>Action</th>"
        f"<th>Status</th><th>Amount</th><th>Reasoning</th></tr></thead>"
        f"<tbody>{''.join(out)}</tbody></table></div>"
    )


def main():
    settings = load_settings()
    broker = CryptoBroker(settings.alpaca_api_key, settings.alpaca_secret_key)
    account = broker.get_account()
    positions = broker.get_positions()

    day_pl_usd = account.equity - account.last_equity
    day_pl_pct = account.day_pl_pct
    all_time_usd = account.equity - STARTING_EQUITY
    all_time_pct = all_time_usd / STARTING_EQUITY * 100

    last_success = load_json(STATE_DIR / "last_success.json") or {}
    params = load_json(PARAMS_PATH) or {}
    log_rows = load_recent_log_rows(LOG_PATH, MAX_LOG_ROWS)
    all_log_rows = load_all_log_rows(LOG_PATH)

    last_trade_card = build_last_trade_card(all_log_rows, positions)
    realized_table, total_realized_usd, closed_count, win_count = build_realized_trades_table(all_log_rows)
    win_rate = (win_count / closed_count * 100) if closed_count else None

    hours_since = hours_since_last_success()
    hours_since_str = f"{hours_since:.1f}h ago" if hours_since is not None else "unknown"

    generated_at = datetime.now(timezone.utc).isoformat()

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crypto Bots - Alpaca Trend-Following + Kraken Mean-Reversion</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #0f1216; --panel: #171b21; --border: #2a2f37; --text: #e7e9ec; --muted: #9aa4b2;
    --green: #3ecf8e; --red: #ef5f5f; --accent: #5b9dff;
  }}
  @media (prefers-color-scheme: light) {{
    :root {{ --bg: #f7f8fa; --panel: #ffffff; --border: #e2e5ea; --text: #171b21; --muted: #5b6472; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--bg); color: var(--text); margin: 0; padding: 24px 16px 60px;
    font: 14px/1.5 -apple-system, Segoe UI, Roboto, sans-serif;
  }}
  .wrap {{ max-width: 980px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .subtitle {{ color: var(--muted); margin: 0 0 20px; font-size: 13px; }}
  .tabs {{ display: flex; gap: 8px; margin-bottom: 20px; border-bottom: 1px solid var(--border); }}
  .tab-btn {{
    background: none; border: none; color: var(--muted); font: inherit; font-weight: 600;
    text-decoration: none; padding: 10px 4px; margin-right: 16px; cursor: pointer; border-bottom: 2px solid transparent;
  }}
  .tab-btn.active {{ color: var(--text); border-bottom-color: var(--accent); }}
  .tab-panel {{ display: none; }}
  .tab-panel.active {{ display: block; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }}
  .card .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }}
  .card .value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
  .card.clickable {{ cursor: pointer; }}
  .card.clickable:hover, .card.clickable:focus-visible {{ border-color: var(--accent); }}
  .card .card-hint {{ color: var(--accent); font-size: 12px; margin-top: 4px; }}
  .pos {{ color: var(--green); }} .neg {{ color: var(--red); }}
  section {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin-bottom: 20px; }}
  section h2 {{ font-size: 15px; margin: 0 0 12px; }}
  .table-scroll {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }}
  td:last-child, th:last-child {{ white-space: normal; }}
  th {{ color: var(--muted); font-weight: 600; font-size: 12px; }}
  .row-buy {{ background: color-mix(in srgb, var(--green) 8%, transparent); }}
  .row-sell {{ background: color-mix(in srgb, var(--accent) 8%, transparent); }}
  .row-error {{ background: color-mix(in srgb, var(--red) 10%, transparent); }}
  .muted {{ color: var(--muted); }}
  .params {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 8px; font-size: 13px; }}
  .params div span {{ color: var(--muted); }}
  footer {{ text-align: center; color: var(--muted); font-size: 12px; margin-top: 24px; }}
  a {{ color: var(--accent); }}
  code {{ background: var(--border); padding: 1px 5px; border-radius: 4px; font-size: 12px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Crypto Paper-Trading Bots</h1>
  <p class="subtitle">
    Two independent, free, rule-based paper-trading bots. Rebuilt automatically by GitHub Actions
    after every trading run - nothing on this page depends on Claude in any way.
    Last rebuilt: <span class="local-time" data-utc="{esc(generated_at)}">{esc(generated_at)}</span>.
  </p>

  <div class="tabs">
    <button class="tab-btn active" data-tab="alpaca">Trend-Following (Alpaca)</button>
    <a class="tab-btn" href="{SIBLING_DASHBOARD_URL}" target="_top">Mean-Reversion (Kraken)</a>
  </div>

  <div class="tab-panel active" id="tab-alpaca">
    <p class="subtitle">
      Real Alpaca paper trading account (simulated money, not real trades). Trend-following
      crossover strategy, hourly runs. Last successful run: {utc_span(last_success.get('timestamp_utc'))}
      ({hours_since_str}).
    </p>

    <div class="cards">
      <div class="card"><div class="label">Equity</div><div class="value">{fmt_money(account.equity)}</div></div>
      <div class="card"><div class="label">Cash</div><div class="value">{fmt_money(account.cash)}</div></div>
      <div class="card"><div class="label">Today's P/L</div><div class="value {'pos' if day_pl_usd >= 0 else 'neg'}">{fmt_pct(day_pl_pct)}</div></div>
      <div class="card"><div class="label">All-time P/L</div><div class="value {'pos' if all_time_usd >= 0 else 'neg'}">{fmt_pct(all_time_pct)}</div></div>
      <div class="card"><div class="label">Open positions</div><div class="value">{len(positions)}</div></div>
      <div class="card clickable" id="card-completed-runs" role="button" tabindex="0">
        <div class="label">Completed runs</div>
        <div class="value">{last_success.get('run_count', 'n/a')}</div>
        <div class="card-hint">View trades &rarr;</div>
      </div>
    </div>

    <section>
      <h2>Open positions</h2>
      {build_positions_table(positions)}
    </section>

    <section>
      <h2>Live strategy parameters</h2>
      <div class="params">
        <div><span>Short SMA window: </span>{params.get('short_sma_window')}h</div>
        <div><span>Long SMA window: </span>{params.get('long_sma_window')}h</div>
        <div><span>Trend filter window: </span>{params.get('trend_window')}h</div>
        <div><span>Stop-loss: </span>{params.get('stop_loss_pct')}%</div>
        <div><span>Take-profit: </span>{params.get('take_profit_pct')}%</div>
        <div><span>Min confidence: </span>{params.get('min_confidence')}</div>
        <div><span>Max position size: </span>{params.get('max_position_pct')}% of equity</div>
        <div><span>Max total exposure: </span>{params.get('max_total_exposure_pct')}%</div>
        <div><span>Max daily loss: </span>{params.get('max_daily_loss_pct')}%</div>
      </div>
      <p class="muted" style="margin-top:12px;">
        Validated safety gates (must hold in every backtest window before any config goes live):
        max drawdown &le; {abs(SAFE_MAX_DRAWDOWN_PCT):.0f}%, min win rate &ge; {MIN_WIN_RATE_PCT:.0f}%.
      </p>
    </section>

    <section>
      <h2>Recent activity (last {len(log_rows)} log rows)</h2>
      {build_log_table(log_rows)}
    </section>
  </div>

  <div class="tab-panel" id="tab-last-trade">
    <p class="subtitle">
      Did the bot actually take a trade, and how did it turn out - pulled straight out of the full
      run-by-run log below, since most runs just hold or get filtered out.
    </p>

    <section>
      <h2>Last trade taken</h2>
      {last_trade_card}
    </section>

    <section>
      <h2>Realized P/L (closed trades)</h2>
      <div class="cards">
        <div class="card"><div class="label">Closed trades</div><div class="value">{closed_count}</div></div>
        <div class="card"><div class="label">Win rate</div><div class="value">{f'{win_rate:.0f}%' if win_rate is not None else 'n/a'}</div></div>
        <div class="card"><div class="label">Total realized P/L</div><div class="value {'pos' if total_realized_usd >= 0 else 'neg'}">{fmt_money(total_realized_usd)}</div></div>
      </div>
      {realized_table}
    </section>
  </div>



  <footer>
    Source: <a href="https://github.com/wheresej2k/crypto-trading-agent">github.com/wheresej2k/crypto-trading-agent</a>
    - built with plain Python by GitHub Actions, no Claude session involved in generating this page.
  </footer>
</div>
<script>
  document.querySelectorAll('.local-time').forEach(function(el) {{
    var iso = el.getAttribute('data-utc');
    if (!iso) return;
    var d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z');
    if (isNaN(d.getTime())) return;
    el.textContent = d.toLocaleString(undefined, {{
      year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'
    }});
  }});

  function activateTab(name) {{
    document.querySelectorAll('button.tab-btn').forEach(function(b) {{ b.classList.toggle('active', b.dataset.tab === name); }});
    document.querySelectorAll('.tab-panel').forEach(function(p) {{ p.classList.toggle('active', p.id === 'tab-' + name); }});
  }}

  document.querySelectorAll('button.tab-btn').forEach(function(btn) {{
    btn.addEventListener('click', function() {{ activateTab(btn.dataset.tab); }});
  }});

  var completedRunsCard = document.getElementById('card-completed-runs');
  if (completedRunsCard) {{
    completedRunsCard.addEventListener('click', function() {{
      activateTab('last-trade');
      document.querySelector('.tabs').scrollIntoView({{ behavior: 'smooth', block: 'start' }});
    }});
    completedRunsCard.addEventListener('keydown', function(e) {{
      if (e.key === 'Enter' || e.key === ' ') {{
        e.preventDefault();
        completedRunsCard.click();
      }}
    }});
  }}
</script>
</body>
</html>
"""

    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
