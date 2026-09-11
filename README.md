# Agentic ETF

Scripts for an automated ETF dip-buying system that runs as scheduled cloud tasks in Claude,
trades a Robinhood agentic account, keeps state in a Google Drive folder, and reports to Slack.

Nothing in this repo is secret. Account numbers, state, logs, and configuration live in Drive.

## How the pieces fit

| Where | What |
|---|---|
| this repo (public) | `screen.py`, `execute.py`, `ingest.py`, `drive_text.py` |
| Google Drive folder **Agentic ETF** | `config.json`, `strategy.json`, `candidates.json`, `state.json` (Docs), `universe` (Sheet), `runlog_<date>_<run>` files (write-once), `screen_detail_<date>.json` (monthly record) |
| Robinhood | data (historicals, quotes, positions, buying power) and order placement |
| Slack `#agentic-trading` | one monospaced report per run |

A scheduled task clones this repo, reads the small state files from Drive, pulls market data
from Robinhood, runs the scripts, places any orders, writes the state back, and posts to Slack.

## Token economics (why it is built this way)

Every byte a connector returns lands in the model's context, and every byte the model writes to
disk costs output tokens. Two facts shape the design:

1. **Large connector results are saved to disk, not shown to the model.** Above roughly 50k
   characters, the harness writes the result to a file. `ingest.py` reads those files directly.
   So every `get_equity_historicals` call is made with **exactly 10 symbols** (pad with universe
   tickers if needed) and at least a year of daily bars, guaranteeing it lands on disk and costs
   nothing. A single-ticker call would come back inline and cost ~15k tokens. Never read the
   saved result files.
2. **Small values are passed on the command line.** Quotes, positions, and buying power are a
   dozen numbers; the task transcribes them as `--quotes`, `--positions`, `--buying-power`.

Drive's connector cannot overwrite content, so a state write is `create_file` (new) followed by
`trash_file` (old). Docs come back with markdown escapes; `drive_text.py` strips them and
validates JSON.

## Scripts

### `execute.py` (intraday, 09:10 and 14:10 CT on market days)

```
python3 execute.py check                      # no files needed; exit 1 if NYSE closed
python3 ingest.py --workdir work              # saved Robinhood results -> work/prices_daily.csv
python3 execute.py plan   --workdir work --run 1410 --quotes "SOXX=528.01,..." \
        --positions "ITA=0.104408@229.87,..." --buying-power 64
#   -> work/orders.json (what to place), work/report.txt (draft)
python3 execute.py record --workdir work --run 1410 --filled "SOXX=1.00" (same --quotes/--positions/--buying-power)
#   -> work/state.json (cooldowns + ledger), work/runlog_<date>_<run>.csv, work/report.txt (final)
```

Indicators (parameters in `strategy.json`): price at/below the prior N-day low; price at least
X% below the prior 52-week high; price at/below the lower Bollinger band. Each tripped indicator
funds `dollars_per_indicator`, at most once per ticker per indicator per trading day. Buys stop
when buying power would fall below `buying_power_floor`. Only tickers in `candidates.json` are
bought; holdings that are not candidates are reported, never sold.

### `screen.py` (monthly, 1st of the month 07:00 CT)

```
python3 ingest.py --workdir work              # monthly bars for the whole universe (+ weekly/daily when fetched)
python3 screen.py --workdir work [--backtest]
#   -> work/candidates.json, work/screen_detail.json, work/state.json, work/strategy.json (if changed), work/report.txt
```

Screen: keep ETFs that beat VOO on trailing 1y and 3y; drop any with 3+ consecutive months
under VOO; cluster survivors by return correlation (average linkage, threshold in config); in each
cluster choose the highest `mean(annualized 1y, 3y) - expense_ratio`, so a pricier near-twin must
out-earn its fee gap. Reports the dip-buy vs blind-DCA cost basis from the running ledger. In
backtest months (Jan/Apr/Jul/Oct) it grid-searches indicator parameters on 2 years of daily bars
for all survivors and auto-applies a change only if it improves the DCA advantage by at least
`auto_apply_min_improvement_pp` without a worse drawdown.

### `ingest.py`

Finds `mcp-Robinhood-get_equity_historicals-*.txt` files the harness saved (under
`/root/.claude/projects/*/tool-results/`, modified in the last 3 hours) and writes
`prices_daily.csv` / `prices_weekly.csv` / `prices_monthly.csv` (date,ticker,close,high,low,volume).

### `drive_text.py`

`unescape IN OUT` for Docs (JSON validated when OUT ends in .json); `table2csv IN OUT` for Sheets.

## Files in Drive

- `config.json` – account, channel, folder id, execution and screening parameters (edit by hand)
- `universe` (Sheet) – seed ETF list with expense ratios (edit by hand)
- `strategy.json` – indicator parameters + version history (scripts update on auto-apply)
- `candidates.json` – current candidate per correlation group (monthly)
- `state.json` – cooldowns and the running dip-vs-DCA ledger (every run)
- `runlog_YYYY-MM-DD_RUN` – one per run, never re-read
- `screen_detail_YYYY-MM-DD.json` – full monthly screen stats

## Local test

```
mkdir work && cp config.json strategy.json universe.csv work/
python3 ingest.py --workdir work --since-minutes 600
python3 screen.py --workdir work --today 2026-09-01
python3 execute.py plan --workdir work --run 1410 --quotes "SOXX=528.01" --buying-power 64 --now 2026-09-11T14:10:00-05:00
```
