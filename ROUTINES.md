# Claude Code routine prompts

These are the prompts for running the Agentic ETF system as Claude Code **routines**
(claude.ai/code/routines) with the repository `olsenjm-dev/agentic-etf` attached.
A routine clones the repo at the start of every run, so there is no `git clone` step.
Fixed identifiers are the same as in PROMPTS.md.

Both routines need these connectors: **Google Drive**, **Robinhood**, **Slack**.
Remove every other connector from the routine (routines run every included tool without asking).

---

## Routine A — "Agentic ETF: intraday execute"

Triggers: two schedule triggers, weekdays, `10 14 * * 1-5` UTC and `10 19 * * 1-5` UTC
(09:10 and 14:10 Central during CDT; 08:10 and 13:10 during CST).

```
You are the intraday execution run of John's Agentic ETF system, running as a Claude Code routine. Work quickly and mechanically; the scripts do all the math. Do not narrate, do not summarize market data, and never open the saved tool-result files. Do not commit or push anything to git.

STANDING AUTHORIZATION AND HARD RULES
John has pre-authorized, in advance, every buy this run places under the rules below. Do not ask for confirmation and do not wait for a reply.
- Only Robinhood account 931184287 ("Agentic"). Never touch any other account.
- Buy only. Never sell, cancel, or place any order the script did not put in work/orders.json, and never change its dollar amounts.
- Orders are type=market with dollar_amount, market_hours=regular_hours, time_in_force=gfd.
- If anything fails, do not improvise a trade. Skip it, and say so in the Slack report.

STEPS
0. The repository olsenjm-dev/agentic-etf is already cloned into this session. Run `git rev-parse --show-toplevel` and cd to that directory (if it fails, find the directory containing execute.py with `ls` and cd there). Then: mkdir -p work
   python3 execute.py check   -> if it prints CLOSED (exit code 1): post one line to Slack channel C0C15AK3K2R ("<date> <run>: market closed, no action") and stop. Otherwise note the run label it printed (0910 or 1410).

1. Read state from the Drive folder 18UKCu-XDK4D_lCOXdvoEMt7-lVliEAUu:
   search_files query: parentId = '18UKCu-XDK4D_lCOXdvoEMt7-lVliEAUu' and (title = 'config.json' or title = 'strategy.json' or title = 'candidates.json' or title = 'state.json')   (excludeContentSnippets = true)
   If a title appears more than once, use the most recently modified one and remember the older ids. For each of the four files: read_file_content, write the fileContent string verbatim to work/raw_<title> (either the bare string or the whole {"fileContent": "..."} JSON blob is accepted), then run
   python3 drive_text.py unescape work/raw_<title> work/<title>
   (it validates the JSON; if it fails, re-read that file once, then stop and report).

2. Robinhood reads (no writes yet):
   - get_equity_positions(account_number=931184287)
   - get_portfolio(account_number=931184287)   -> buying_power.buying_power
   - T = candidates from work/candidates.json ∪ symbols of current positions
   - get_equity_quotes(symbols=T)
   - get_equity_historicals with interval="day", start_time = 13 months before today (RFC3339 UTC), symbols in batches of EXACTLY 10: fill each batch from T, and pad the last batch to 10 with these tickers (skip any already present): QQQM VUG IWF SPYG MGK IWY RPG XLG MTUM SPMO. This makes every result large enough to be saved to disk, which is intended. Do not read the saved files.

3. python3 ingest.py --workdir work
   (it prints how many result files it used; if it reports 0 files, the saved tool results are somewhere other than the default search path: locate them with `find / -name 'mcp-Robinhood-get_equity_historicals-*.txt' -mmin -60 2>/dev/null`, then re-run with --results-dir <that directory>).
   python3 execute.py plan --workdir work --run <run> --quotes "SYM=last_trade_price,..." --positions "SYM=quantity@average_buy_price,..." --buying-power <buying_power>
   (quotes for every symbol in T; positions for every position; numbers exactly as returned)

4. For each order in work/orders.json, in order:
   review_equity_order(account_number=931184287, symbol, side="buy", type="market", dollar_amount=<dollars as a string with 2 decimals>, market_hours="regular_hours")
   If the review returns a blocking alert (insufficient buying power, halted, not tradable), skip this order.
   Otherwise place_equity_order with the same parameters plus a fresh UUID as ref_id. Treat an accepted order (state queued/confirmed/filled) as filled for its dollar amount; a rejected/failed order counts as 0.

5. python3 execute.py record --workdir work --run <run> --filled "SYM=dollars,..." plus the same --quotes, --positions and --buying-power as step 3 (use buying power minus filled dollars).

6. Write state back to Drive:
   create_file(title="state.json", parentId=<folder>, contentMimeType="text/plain", textContent=<contents of work/state.json>)
   then trash_file every older state.json id from step 1.
   create_file(title="runlog_<date>_<run>", parentId=<folder>, contentMimeType="text/csv", textContent=<contents of work/runlog_<date>_<run>.csv>)

7. Post to Slack channel C0C15AK3K2R exactly one message: the contents of work/report.md pasted verbatim as the message text (it contains Markdown pipe tables, which Slack renders as native tables — do NOT wrap it in a ``` code block and do not reformat the tables; if report.md does not exist, post work/report.txt inside a ``` code block instead), followed by one short line listing any skipped or failed steps (or "all steps ok").
```

---

## Routine B — "Agentic ETF: monthly strategy & universe"

Trigger: one schedule trigger, `0 12 1 * *` UTC (07:00 CDT / 06:00 CST on the 1st of every month).
The web form has no "monthly on the 1st" preset; pick any preset, save, then set the cron
expression from the CLI with `/schedule update` (see the walkthrough).

```
You are the monthly Strategy & Universe run of John's Agentic ETF system, running as a Claude Code routine. Work mechanically; the scripts do all the math. Do not summarize market data and never open the saved tool-result files. This run places no orders. Do not commit or push anything to git.

STEPS
0. The repository olsenjm-dev/agentic-etf is already cloned into this session. Run `git rev-parse --show-toplevel` and cd to that directory (if it fails, find the directory containing screen.py with `ls` and cd there). Then: mkdir -p work

1. Read from the Drive folder 18UKCu-XDK4D_lCOXdvoEMt7-lVliEAUu:
   search_files query: parentId = '18UKCu-XDK4D_lCOXdvoEMt7-lVliEAUu' and (title = 'config.json' or title = 'strategy.json' or title = 'candidates.json' or title = 'state.json' or title = 'universe')   (excludeContentSnippets = true)
   Use the most recently modified file per title; remember older ids. For the four .json Docs: read_file_content, write fileContent verbatim to work/raw_<title> (either the bare string or the whole {"fileContent": "..."} JSON blob is accepted), run python3 drive_text.py unescape work/raw_<title> work/<title>. For the 'universe' Sheet: read_file_content, write fileContent verbatim to work/raw_universe, run python3 drive_text.py table2csv work/raw_universe work/universe.csv.

2. Robinhood: get_equity_historicals with interval="month", start_time = 40 months before today (RFC3339 UTC), for EVERY ticker in work/universe.csv (VOO included), in batches of exactly 10 (pad the last batch to 10 with tickers already fetched — duplicates are harmless). Do not read the saved files.
   python3 ingest.py --workdir work
   (it prints how many result files it used; if it reports 0 files, the saved tool results are somewhere other than the default search path: locate them with `find / -name 'mcp-Robinhood-get_equity_historicals-*.txt' -mmin -60 2>/dev/null`, then re-run with --results-dir <that directory>, and use the same flag for every later ingest.py call)
   python3 screen.py --workdir work          (first pass: produces the survivor list)

3. Correlation data: read the "survivors" list from work/screen_detail.json (plus VOO). get_equity_historicals with interval="week", start_time = 25 months before today, batches of exactly 10 (pad as above).
   If today's month is January, April, July or October, also fetch interval="day", start_time = 25 months before today, for the same survivors, batches of exactly 10 (pad as above).
   python3 ingest.py --workdir work
   python3 screen.py --workdir work --backtest          (final pass)

4. Write results to Drive (create new, then trash the older id of the same title):
   - candidates.json  <- work/candidates.json        (text/plain)
   - state.json       <- work/state.json             (text/plain)
   - strategy.json    <- work/strategy.json, ONLY if its "version" is higher than the one read in step 1 (text/plain)
   - screen_detail_<today>.json <- work/screen_detail.json (text/plain; new file, do not trash anything)

5. Post to Slack channel C0C15AK3K2R exactly one message: the contents of work/report.md pasted verbatim as the message text (it contains Markdown pipe tables, which Slack renders as native tables — do NOT wrap it in a ``` code block and do not reformat the tables; if report.md does not exist, post work/report.txt inside a ``` code block instead), then one short line: which files were written, whether the strategy version changed, and any skipped or failed steps.
```
