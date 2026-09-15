"""Builds docs/index.html - a fully static status dashboard for GitHub Pages, generated fresh
after every trading run by hourly-trade.yml. Matches the pattern used by the sibling
crypto-mean-reversion-agent project's own build_dashboard.py.

This replaces the earlier design where a scheduled Claude session cloned the repo, rebuilt a page,
and republished it as a claude.ai Artifact - that worked, but stopped updating the moment the user
was out of Claude usage, since running that routine at all required Claude. Nothing in this script
or in GitHub Pages hosting depends on Claude in any way: GitHub Actions builds this file with plain
Python, and GitHub Pages serves the static result, both for free, indefinitely.

The page has two tabs: this bot's own live data (rendered natively, straight from this repo's own
state), and the sibling mean-reversion bot's dashboard (embedded via iframe from its own already-
live GitHub Pages URL - wheresej2k.github.io/crypto-mean-reversion-agent/). Embedding via iframe
instead of fetching/re-rendering its data here means neither bot's workflow ever needs credentials
or write access to the other's repo - each just builds and hosts its own page, and links to the
other's.

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
        pl_pct = pos.unrealized_plpc * 100
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
    padding: 10px 4px; margin-right: 16px; cursor: pointer; border-bottom: 2px solid transparent;
  }}
  .tab-btn.active {{ color: var(--text); border-bottom-color: var(--accent); }}
  .tab-panel {{ display: none; }}
  .tab-panel.active {{ display: block; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }}
  .card .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }}
  .card .value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
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
  iframe {{ width: 100%; height: 1700px; border: 0; border-radius: 10px; }}
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
    <button class="tab-btn" data-tab="kraken">Mean-Reversion (Kraken)</button>
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
      <div class="card"><div class="label">Completed runs</div><div class="value">{last_success.get('run_count', 'n/a')}</div></div>
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

  <div class="tab-panel" id="tab-kraken">
    <p class="subtitle">
      Sibling bot's own live dashboard, embedded directly from its own GitHub Pages site
      (<a href="{SIBLING_DASHBOARD_URL}" target="_blank" rel="noopener">open in a new tab</a>).
    </p>
    <iframe src="{SIBLING_DASHBOARD_URL}" loading="lazy" title="Mean-reversion bot dashboard"></iframe>
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

  document.querySelectorAll('.tab-btn').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      document.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
      document.querySelectorAll('.tab-panel').forEach(function(p) {{ p.classList.remove('active'); }});
      btn.classList.add('active');
      document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
    }});
  }});
</script>
</body>
</html>
"""

    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
