# Top-5 Growth ETF Screen (schema `top5-v1`)

This is a notification-only research system. It never places, reviews or cancels orders.
Three scheduled Claude tasks clone this repo. The scripts here do all the arithmetic, selection and
report text. The tasks only make the Drive, Robinhood and Slack tool calls and pass script output through.

| Task | Schedule (UTC) | Script |
|---|---|---|
| Annual Universe Build | `0 9 27 12 *` (Dec 27) | `universe.py` |
| Monthly Top-5 Screen | `0 9 1 * *` (1st of month) | `screen.py` |
| Quarterly Parameter Backtest | `0 9 28 3,6,9,12 *` | `backtest.py` |

Where things live:

- Drive folder "Agentic ETF" (`18UKCu-XDK4D_lCOXdvoEMt7-lVliEAUu`) holds the state and logs.
- Reports go to Slack `#agentic-trading` (`C0C15AK3K2R`).
- Robinhood account `931184287` is used only for tradability checks.

## Files in this folder

| File | Purpose |
|---|---|
| `common.py` | Constants (taxonomy, IDs), schema guard, dates, batching, universe CSV I/O |
| `ingest.py` | Turns saved Robinhood historicals files into a total-return series per ticker |
| `engine.py` | Stages A-E, shared by the monthly screen and the backtest |
| `screen.py` | `plan` / `run` for the monthly screen |
| `backtest.py` | `plan` / `run` for the quarterly backtest |
| `universe.py` | `plan` / `build` / `verify` for the annual universe |
| `drive_text.py` | Cleans Google Doc/Sheet text read back through the Drive connector |
| `strategy_default.json` | Starting parameters (the first monthly run copies them to Drive) |

## Drive files (every JSON file carries `"schema_version": "top5-v1"`; a mismatch is a hard fail)

| Title | Written by | Notes |
|---|---|---|
| `universe` (Sheet) | annual build; monthly (only when it flips on-deck funds or deactivates funds) | 17 columns, see `common.UNIVERSE_COLUMNS` |
| `strategy.json` | first monthly run (bootstrap); quarterly backtest (auto-apply only) | never edited by the monthly screen |
| `current.json` | monthly | top five, on-watch, comparator, previous state |
| `performance.json` | monthly | one entry per list; forward 1/3/6/12-month returns: list, VOO, unconstrained |
| `proposals.json` | first monthly run (empty); backtest | never auto-applied |
| `history_YYYY-MM.json` | monthly | full ranked list with metrics |
| `cache_prices_YYYY-MM.json` | monthly | compact month-end record (close, TTM dividends, window returns) |
| `runlog_YYYY-MM.json` | monthly, written last | also the idempotency marker |
| `backtest_YYYY-MM.json` | backtest, written last | idempotency marker |
| `universe_build_YYYY-MM-DD.json` | annual build | build report |

The Drive connector cannot overwrite a file. To replace one, the task creates the new file and then trashes the older files with the same title.

## Total return: how dividends are handled

Robinhood's `adjustment_type="all"` daily bars cause two problems:

- They subtract dividends as a flat dollar offset instead of scaling prices. Returns computed from those bars come out too high (VGT 2023 shows +81% instead of +53%).
- Dividends paid before a split are not adjusted for the split.

So each batch of 10 symbols is fetched three times: `split`, `all` and `none`. `ingest.py` then works out:

- the raw dividend: the day-to-day change in (split − all)
- the split factor: none ÷ split
- a total-return (TR) index from split-adjusted prices plus split-adjusted dividends

It also cancels Robinhood's multi-bar glitches (a jump followed by an equal and opposite jump). It drops any payout larger than 5% of price unless the unadjusted price gapped down at the open by at least half the payout.

- **Checked against published figures:** VOO 2017–2025 calendar-year total returns all match (e.g. −18.19% for 2022, +26.32% for 2023, +24.98% for 2024). So do QQQ 2017–2024 and VGT/VUG/SCHG 2022–2023.
- **Fallback:** if a ticker's dividends can't be verified within the last 5 years, that ticker uses price return plus the `distribution_yield` accrual. The report header names it.

The saved result files don't record which adjustment they hold, so the script tells them apart from the data:

- split vs all differ by a constant on each bar
- none vs split differ by a ratio on each bar

## Monthly rules (screen.py + engine.py)

**Anchor date:** the last trading day before the 1st of the run month.

**Windows:**

- 6M, 1Y and 3Y are snapped to the nearest earlier trading day.
- Correlation uses the trailing 504 trading days.
- Max drawdown uses available history up to 5 years (never less than 3), and the report shows the years used.

**A. Eligibility**

- The fund must be active in the universe and at least 39 months past inception.
- On-deck funds turn active once their `eligible_from` month is reached.
- Deactivation happens only for hard failures: no price data or stale data, a 30-day median dollar volume under $5M, or failing the Robinhood tradability check on the final list.

**B. Hard filter (never relaxed):** the fund's cumulative total return must beat VOO by at least `excess_min_pp` (0.5) on 6M, 1Y and 3Y.

**C. Score**

`w_6m·ex6 + w_1y·ex1 + w_3y·ex3 − er_penalty·ER + incumbent_bonus`

- `ex6` is the 6M excess, annualized as (1+r)² − (1+r_VOO)².
- `ex1` is the 1Y excess.
- `ex3` is the 3Y CAGR difference.
- ER is the expense ratio in percent.
- The incumbent bonus applies to funds currently on the list or on-watch.
- The information ratio (IR) is reported but doesn't count toward the score, unless `scoring="ir"`.

**D. Selection (greedy)**

1. Regress daily returns on VOO and build the correlation matrix of the residuals.
2. Walk down the ranking, adding a fund only if its residual correlation with every fund already picked is ≤ `resid_corr_max`, and its category differs from theirs.
3. Within `tie_band` of the best eligible score, the fund with the smaller max drawdown wins.
4. If fewer than 5 are picked, relax the correlation cap in 0.05 steps up to `resid_corr_ceiling`, then drop the category rule, then publish the short list. Every step is reported.
5. If 5 or fewer funds pass Stage B, all of them are published as-is.

**E. Exits**

- A fund that fails Stage B is removed immediately.
- A fund that is deactivated in the universe is removed immediately.
- The first month outside the top five puts a fund on-watch. It stays on the list and still gets the incumbent bonus. The second consecutive month outside removes it.

**Unconstrained comparator:** the top 5 by raw score (no incumbent bonus), with no correlation or category rule. Its forward returns are tracked the same way as the list's.

**Performance:** equal-weight buy-and-hold forward returns over 1, 3, 6 and 12 months for the held list (top five plus on-watch), the comparator and VOO. The report line shows each horizon's return for the list published that many months earlier.

**Retries:** if `current.json` already holds this anchor month, the script uses the `previous` block stored inside it, so a retried run doesn't count a month outside twice.

## Quarterly backtest (backtest.py)

**When it skips:**

- `strategy.json` doesn't exist yet.
- Fewer than `min_live_months` (3) monthly screens have been recorded.
- Fewer than 12 month-ends can be reconstructed.

**How it runs**

- It pulls 10 years of daily data itself; no price caches are needed.
- It reconstructs the monthly screen at every month-end where 37 months of data exist. Eligibility at each date is based on inception.
- It tests up to 10 variants. Each variant changes only one group: the three weights, `resid_corr_max`, `er_penalty`, `incumbent_bonus`, or raw vs IR scoring.

**Auto-apply**

- Only the best variant is applied, and only if it beats the current parameters by at least 2.0 pp annualized with a max drawdown no worse.
- Applying bumps the `strategy.json` version and adds a changelog entry with the numbers.

**Proposals:** other variants that gain at least `min_proposal_pp` (0.5 pp) go to `proposals.json` as pending.

The report always carries a survivorship-bias warning. Until 24 live months exist, it also warns that differences are more likely noise than signal.

## Local test

```
python3 screen.py plan --workdir work --today 2026-10-01
python3 ingest.py --workdir work --results-dir <dir with saved historicals files> --since-minutes 100000
python3 screen.py run --workdir work --today 2026-10-01 --bootstrap-strategy
```
