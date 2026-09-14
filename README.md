# Crypto Trading Agent (paper trading, 100% free)

An automated crypto trading bot: a free, rule-based strategy (moving-average crossover on hourly
bars) watches a crypto watchlist 24/7 and generates buy/sell/hold signals, a risk-limit layer
filters those signals before anything reaches Alpaca, and a "software bracket" (see below) manages
stop-loss/take-profit protection the way Alpaca's own bracket orders would - if crypto supported
them, which it doesn't.

**This runs entirely on free services: Alpaca's paper-trading environment (no real money, no
funding required), Alpaca's free crypto market data, and GitHub Actions' free automation minutes.
There is no paid API involved anywhere.**

This is a sibling project to `stock-trading-agent`, but it is **not a copy-paste with tickers
swapped**. Crypto trades 24/7 with no market-hours gate, and - critically - Alpaca does not
support bracket orders (native stop-loss/take-profit) for crypto at all, only for stocks. Every
part of how this bot places and protects a trade had to be rebuilt around that. See "How this
differs from the stock bot" below for the specifics.

Read the whole README before running it, especially "Important limitations" at the bottom.

## The goal, stated concretely (read this before trusting any of it)

"Working" means a specific, quantified bar, not a vibe. Before it's trusted with even paper money,
a set of strategy parameters must, when backtested across **three different historical windows**
(~3 months, ~1 year, and ~5 years - close to the full history Alpaca has for crypto, which starts
2021-01-01):

- **Never be net-unprofitable** (total return >= 0%) in any window
- **Never draw down more than 22%** from its peak equity in any window
- **Never have a win rate below 40%** on completed round-trip trades in any window

This is exactly what `tune.py`'s safety filter enforces (see `SAFE_MAX_DRAWDOWN_PCT` and
`MIN_WIN_RATE_PCT` at the top of that file) before it will even consider a parameter combination's
profitability - the same "never-unprofitable, capped-drawdown" idea the stock bot's `tune.py` used,
extended here with an explicit win-rate floor. These three numbers came from the "moderate" risk
tier chosen when this project was set up; tighten or loosen them in `tune.py` if your risk
tolerance changes.

**This is a bar for trusting the strategy's parameters, not a guarantee about the future.** A
backtest passing this bar is a hypothesis worth continuing to test on paper - see the curve-fitting
caveat in `tune.py`.

## The four pillars this project is built around

### 1. ACCURATE - the right data, in the right shape
- `crypto_broker.py` pulls hourly bars directly from Alpaca's own crypto market data (the
  exchange Alpaca itself executes crypto trades on - no third-party feed, no stock-market
  IEX-feed workaround needed since crypto data isn't restricted like free-tier stock data is).
- **Data granularity matches the strategy**: the bot runs once an hour, so it trades on hourly
  bars - not 1-minute noise it can't act on any faster than hourly anyway, and not daily bars
  that would throw away most of what "24/7" actually means.
- `data_validator.py` is a dedicated module that checks every bar of market data **before** the
  strategy ever sees it: is the latest bar actually recent (not a stale/frozen feed)? Are there
  gaps (missing bars that would silently distort the moving averages)? Are there non-positive,
  NaN, or wildly implausible single-hour price moves (more likely bad data than a real move)? A
  symbol that fails any check is dropped from *that run only* and logged - the bot never silently
  trades on data it can't vouch for.

### 2. RELIABLE - runs 24/7 without you babysitting it
- `retry.py` wraps every Alpaca API call in automatic retries with exponential backoff, so one
  network blip or momentary rate limit doesn't kill an entire hourly run.
- Every error is logged clearly (`logs/trade_log.csv` gets a row, and the GitHub Actions log
  shows the full traceback) - nothing fails silently.
- **Missed-run detection**: this is the direct fix for the exact problem the stock bot hit - a
  GitHub Actions cron schedule silently not firing, with nothing to notice unless someone happens
  to check. `heartbeat.py` records a timestamp after every run that completes without crashing;
  a completely separate scheduled workflow (`.github/workflows/watchdog.yml`, `watchdog.py`) checks
  that timestamp every 3 hours and texts a plain-text warning if more than `WATCHDOG_ALERT_AFTER_HOURS`
  (default 3) hours have passed with no successful run. It runs on its own independent trigger, so
  it still works even if the main hourly workflow's own schedule is the thing that broke.
- **The stop-loss/take-profit protection doesn't depend on the bot running at all.** Because
  Alpaca has no crypto bracket orders, `position_tracker.py` places the stop-loss and take-profit
  as two independent resting orders directly on Alpaca's exchange right after a buy fills. Those
  orders execute continuously, 24/7, regardless of whether a scheduled run happens on time - an
  open position stays protected even during an outage that delays the bot itself.

### 3. WELL-DEFINED GOAL - see the section above
Explicit, quantified, and enforced by `tune.py`'s safety filter before any parameter set is
considered "good," not just implied by whatever the code happens to do.

### 4. SELF-IMPROVING - learns from every trade, safely
- **Full lifecycle logging**: `trade_log.py` records not just that a trade happened, but the
  short/long SMA values the strategy actually saw at decision time, its reasoning, and - critically
  - what happened *after*: when a position closes (stop-loss, take-profit, or a signal-driven
  exit), a linked row records the real entry/exit price and realized P/L. This is the raw
  material any future analysis of "what's actually working" would use.
- **Automated re-tuning, gated behind a human review.** Once a month,
  `.github/workflows/monthly-retune.yml` re-runs `tune.py`'s exact safety-filtered sweep against
  fresh historical data (`auto_retune.py`). If a different parameter combination now scores best
  *and it still passes the same safety filter*, it opens a **pull request** proposing the change
  to `config/params.json`, with the backtest numbers in the PR description. **It never merges
  automatically, never touches the live bot directly, and never touches your risk-tolerance
  settings** (position size, total exposure, and daily-loss caps - see below). You review the PR
  and merge it yourself, or don't.

**Where I think this pillar is genuinely risky to fully automate, and why I didn't:** an
automated process that can silently widen how much of your account it's willing to risk - even in
paper trading - is a different and more dangerous kind of automation than one that proposes
adjusting *when* it enters/exits a trade. That's why `tune.py`/`auto_retune.py` are hard-coded to
never touch `max_position_pct`, `max_total_exposure_pct`, or `max_daily_loss_pct` - those three
live in `config/params.json` too, but only you change them, by hand, when you deliberately decide
to change your risk tolerance. Everything the automated retune *can* propose (SMA windows,
stop-loss/take-profit, confidence threshold) still has to survive the exact same backtest safety
filter as a manual `tune.py` run before it's even offered to you as a PR - there's no path from
"the bot noticed something" to "the bot's live behavior changed" that skips a human.

## How this differs from the stock bot (not a copy-paste)

| | Stocks | Crypto (this project) |
|---|---|---|
| Market hours | Only trades 9:30am-4pm ET | Trades 24/7, no market-hours gate |
| Bracket orders (stop-loss+take-profit) | Native, one order | **Not supported by Alpaca for crypto** - built manually as two independent resting orders (see `position_tracker.py`), reconciled every run |
| Data granularity | Daily bars, run once/day | Hourly bars, run once/hour |
| Backtest depth | ~3.4 years (stock data limit at setup time) | ~5 years (crypto data goes back to 2021-01-01) |
| Missed-run detection | Not built (the gap this project fixes) | `heartbeat.py` + a separate watchdog workflow |
| Tunable parameters | Baked into the GitHub Actions workflow's `env:` block | Split into `config/params.json` (tunable, git-committed) vs `.env`/secrets (credentials + watchlist) - specifically so the automated monthly retune can propose changes as a reviewable file diff |
| Auto re-tuning | Manual only | Manual (`tune.py`) **and** automated-with-a-PR-gate (`auto_retune.py` via `monthly-retune.yml`) |

## One-time setup

### 1. Install Python
If you don't already have it, install Python 3.11+ from [python.org](https://www.python.org/downloads/)
(check "Add python.exe to PATH" during install on Windows).

### 2. Get Alpaca paper-trading API keys (free)
1. Sign up at [alpaca.markets](https://alpaca.markets/) - free, no payment method needed.
2. Go to the [Paper Trading dashboard](https://app.alpaca.markets/paper/dashboard/overview).
3. Generate an API key pair there - make sure you're looking at the **paper** keys, not the live
   ones (Alpaca shows them in separate tabs).

### 3. Install dependencies
Open a terminal in this folder and run:

```bash
pip install -r requirements.txt
```

### 4. Configure your keys
Copy `.env.example` to a new file named `.env` in this same folder, then open `.env` in a text
editor and fill in your actual Alpaca keys, phone/Gmail info (see step 6), and watchlist.

**Never commit or share your `.env` file - it contains your API keys.** It's already listed in
`.gitignore` so `git` won't track it.

### 5. Review the strategy/risk parameters
`config/params.json` holds the tunable numbers (SMA windows, stop-loss/take-profit, position and
exposure caps, daily-loss limit) - it's already seeded with reasonable starting values for a
moderate risk tolerance. Read through it once; run `tune.py` later (see below) to refine the
strategy-timing numbers against real history before trusting it.

### 6. Set up phone notifications (optional but recommended)
1. **Carrier MMS gateway**: find your carrier's free email-to-picture-message address, e.g.
   `5551234567@vzwpix.com` (Verizon), `5551234567@tmomail.net` (T-Mobile),
   `5551234567@mypixmessages.com` (AT&T). Put it in `.env` as `PHONE_MMS_ADDRESS`.
2. **Gmail App Password**: turn on 2-Step Verification on your Google account, then generate an
   App Password at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
   Put your Gmail address and the generated 16-character password in `.env`.

## Running it

Always test with `--dry-run` first - it does everything (fetches data, generates signals, applies
risk limits) except actually submitting orders or touching saved state:

```bash
python trader.py --dry-run
```

Check `logs/trade_log.csv` and the console output. Once you're comfortable with what it's
recommending, run it for real (still paper money):

```bash
python trader.py
```

Unlike the stock bot, there's no `--force`/market-hours flag - crypto trades 24/7, so every run is
a "live" run as far as market availability goes.

## Backtesting and tuning

Before trusting this with even paper money, see how it would have performed on real historical
hourly crypto prices - this replays months or years of data in seconds using the exact same
decision rule and risk-limit code the live bot uses:

```bash
python backtest.py                 # last 8760 hours (~1 year)
python backtest.py --hours 43800   # ~5 years - close to the full history Alpaca has for crypto
```

It reports the strategy's return, a buy-and-hold comparison, the worst drawdown, trade count, and
win rate.

`tune.py` sweeps combinations of the SMA windows, stop-loss/take-profit, and confidence threshold,
and reports which performed best across the three windows described in "The goal, stated
concretely" above - filtered first to combinations that pass that safety bar, same idea as the
stock bot's `tune.py`, extended with a win-rate floor:

```bash
python tune.py
```

Expect this to take **around 5-10 minutes** (fetching ~5 years of hourly data for 5 symbols is
the slow part; the sweep itself, thanks to an efficient rolling-average implementation, takes
under a minute) - it's not hung, just working through real history.

Running `tune.py` never changes anything live on its own - copy the winning combination into
`config/params.json` yourself if you like what you see, or let the monthly automated retune
propose it as a pull request (see the SELF-IMPROVING section above).

**Curve-fitting caveat**: picking whatever parameters scored best on historical data risks tuning
to noise that happened to exist in that specific stretch of history rather than to anything that
will keep working. Treat `tune.py`'s output as a hypothesis worth testing on paper, not a proven
result.

## Running it automatically, 24/7

Three separate GitHub Actions workflows, all free:

- **`.github/workflows/hourly-trade.yml`** - runs `trader.py` every hour, texts a daily summary
  image once a day (default 13:00 UTC, configurable), commits the updated trade log and state
  back to the repo.
- **`.github/workflows/watchdog.yml`** - runs independently every 3 hours, texts a plain warning
  if no successful run has completed in over `WATCHDOG_ALERT_AFTER_HOURS` hours.
- **`.github/workflows/monthly-retune.yml`** - runs on the 1st of each month, may open a pull
  request proposing updated strategy parameters (never auto-merged).

### Setting your secrets (keep these private - see below)
Set these once via the GitHub CLI (prompts you securely - the value never appears in your shell
history or in any file):

```bash
gh secret set ALPACA_API_KEY
gh secret set ALPACA_SECRET_KEY
gh secret set GMAIL_ADDRESS
gh secret set GMAIL_APP_PASSWORD
gh secret set PHONE_MMS_ADDRESS
```

### On repo privacy
This repo is set up as **private**. If you ever consider making it public (e.g. to remove GitHub
Actions' free-minutes cap), your phone number and email stay private either way, as long as they
stay in GitHub Secrets and never get hardcoded into a file: `.env` is git-ignored and never
committed, secrets are encrypted and never appear in the repo's code, and GitHub automatically
redacts any registered secret's value from Actions logs even if a script's output happens to
include it. This project's own code goes further and never prints the raw phone/email at all
(see `notify.py`/`watchdog.py`), as a second, independent layer on top of GitHub's own redaction.
What *would* become public if you switch: the code itself, and the trade log/performance history.

## Reviewing results

- `logs/trade_log.csv` has one row per decision (executed, skipped, dry-run, or failed), plus a
  linked row when a position closes, with the SMA values the strategy saw and the realized P/L.
- `state/open_brackets.json` shows which positions currently have live protective orders.
- `state/last_success.json` shows when the bot last completed a run successfully.
- Your [Alpaca paper dashboard](https://app.alpaca.markets/paper/dashboard/overview) shows live
  positions, P/L, and order history directly.

## Important limitations - please read

- **This is not a validated trading strategy.** SMA crossover is a simple, transparent, and free
  approach, but it's a basic technical indicator, not an edge over the market. Crypto is also
  meaningfully more volatile than the stock bot's watchlist - treat any paper-trading results as
  exploratory, not predictive of real performance.
- **Stay on paper trading.** Nothing in this project should be pointed at a live account. `paper=True`
  is hard-coded into `crypto_broker.py`. If you ever want to consider live trading, that requires
  a separately validated strategy, much more extensive testing, and a clear-eyed, explicit
  conversation and decision about money you could fully afford to lose.
- **The daily-loss limit is a circuit breaker, not a guarantee.** It stops *new* buys once the
  day's loss threshold is hit, but existing positions can still move against you between checks -
  the resting stop-loss order attached to each position is what protects it individually, and
  that keeps working even between runs since it lives on Alpaca's exchange, not in this code.
- **A market buy that never fills is a real, rare edge case this project doesn't fully
  automate around.** `position_tracker.py` waits up to 30 seconds for a buy to fill before placing
  its protective orders; if it somehow never fills in that window (essentially never happens for a
  market order on a liquid pair), the order is left open on Alpaca with no automatic protective
  orders attached, logged clearly, and would need a manual look on the Alpaca dashboard.
- **One open position per symbol at a time.** The bot won't add to a position it's already
  holding - it waits until that position closes (stop-loss, take-profit, or a signal exit) before
  considering a new entry in the same coin. This is a deliberate simplification to avoid merging
  multiple stop/target order pairs into one.
- **This is built for hourly decisions on a five-coin watchlist**, not high-frequency or
  scalping-style trading.
