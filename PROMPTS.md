# Top-5 Growth ETF Screen: Scheduled Task Prompts (v7)

These tasks are notification-only and never place trades. Nothing here is investment advice.

Shared references:
- Drive folder `18UKCu-XDK4D_lCOXdvoEMt7-lVliEAUu` ("Agentic ETF")
- Slack channel `C0C15AK3K2R` (#agentic-trading)
- Robinhood account `931184287`, used only for tradability checks
- GitHub repo `olsenjm-dev/agentic-etf` (public); the scripts are at the repo root
- Every JSON file carries `"schema_version": "top5-v1"`. A missing or wrong value is a hard fail.

Changes from v6:
- `screen.py`'s monthly report is now plain Slack markdown with a real `|` pipe table (rendered natively by Slack), matching the style used by the "All Accounts Buy Signal Check" task. It is no longer wrapped in a ``` code block - a fixed-width monospace table only lines up inside a code block, which renders unreadably on mobile Slack.

Changes from v5:
- The scripts live at the repo root (no `top5/` folder); all three prompts now clone and run from `/tmp/agentic-etf`.
- `ingest.py` no longer crashes when a ticker appears in more than one batch with `--expect 1` (annual build).
- `universe.py` flags the active count only when it falls outside 60-150. The annual build still widens the enumeration only when fewer than 60 funds are active.

Changes from v4:
- The annual build moved to Dec 27, one day before the December backtest.
- The monthly schedule is now valid cron.
- The first monthly run creates `strategy.json` and `proposals.json`.
- The backtest skips until 3 live months exist.
- Prices come as three Robinhood series per batch (split, all, none). The dividend-adjusted series alone is not reliable.

---

## Task 1: Annual Universe Build
**Schedule:** `0 9 27 12 *` (UTC, Dec 27)

```
Build (or rebuild from scratch) the annual ETF universe for John's Top-5 Growth ETF Screen. This is research only: never place, review or cancel any order. John is not present, so do not ask questions. Make reasonable choices and note them in the Slack summary.

Fixed IDs: Drive folder 18UKCu-XDK4D_lCOXdvoEMt7-lVliEAUu ("Agentic ETF"). Slack channel C0C15AK3K2R. Robinhood account 931184287, used for get_equity_tradability only. Repo https://github.com/olsenjm-dev/agentic-etf (scripts at the repo root).
If a step fails in a way these instructions don't cover, stop and post one Slack message saying which step failed and the error line. Never trash the existing universe unless the new one has passed verification.

1. SETUP
   git clone --depth 1 https://github.com/olsenjm-dev/agentic-etf.git /tmp/agentic-etf
   cd /tmp/agentic-etf && mkdir -p /tmp/w
   Run every command below from /tmp/agentic-etf with W=/tmp/w.

2. EXISTING STATE
   Call search_files with query "parentId = '18UKCu-XDK4D_lCOXdvoEMt7-lVliEAUu'", excludeContentSnippets=true, pageSize=100. Follow next_page_token until the list is complete, and remember each file's title, id and modifiedTime.
   If a file titled universe_build_<today YYYY-MM-DD>.json already exists, post "Universe build already ran today - nothing done." and stop.
   If a Sheet titled "universe" exists, take the newest copy and call read_file_content on it. Write the returned fileContent verbatim to /tmp/w/raw_universe, then run:
     python3 drive_text.py table2csv /tmp/w/raw_universe /tmp/w/prev_universe.csv
   Use last year's list only to report what was dropped. Never copy rows forward from it.

3. FRESH ENUMERATION
   Use web search and web fetch. Build a new candidate list of US-listed ETFs with a growth, thematic or growth-sector mandate. Sources: current screener or category listings (large/mid/small-cap growth, technology, semiconductors, health care/biotech, communication/consumer discretionary, thematic, momentum/quality factor, developed ex-US growth, emerging-markets growth, global growth) and the issuers' own product lineups (iShares, Vanguard, SPDR, Invesco, Schwab, Fidelity, First Trust, Global X, VanEck, ARK, WisdomTree, Capital Group, JPMorgan, Franklin, Pacer, Amplify, Roundhill and others the listings surface). Aim for 150-200 candidates; covering too few funds is worse than covering too many. Do not include VOO.
   Mark these as excluded now, with exclude_reason set: leveraged/inverse, single-stock, covered-call/derivative-income, buffered/defined-outcome, commodity, currency, managed-futures, crypto-holding.

4. VERIFY EACH CANDIDATE ON THE ISSUER'S OWN PAGE
   From the issuer's page, record: expense ratio (%), distribution yield (% - use the trailing-12-month distribution yield; if only an SEC yield is shown, use it and say so in notes), inception date, AUM (USD) and stated objective.
   Assign each fund exactly one category from this fixed list (never invent a new one): broad-growth, smid-growth, technology, semiconductors, health-biotech, consumer-comm, thematic, factor, dev-ex-us-growth, em-growth, global-growth.
   Assign mandate from the stated objective: growth, thematic or growth-sector. Anything else (value, dividend, blend, income...) goes in the mandate field as-is (e.g. "dividend"), and the script will exclude it.
   Assign region: US, developed-ex-US, emerging or global.
   notes: purely factual text about what the fund holds. No predictive or evaluative words, and no "|" character.
   Write /tmp/w/candidates.csv with exactly this header:
     ticker,name,mandate,category,region,expense_ratio,distribution_yield,inception,aum,source_url,notes,exclude_reason
   Append rows as you go, using Python's csv module so commas and quotes are handled. source_url is the issuer page you verified against. Dates are YYYY-MM-DD; aum is a plain number (e.g. 1250000000).
   python3 universe.py plan --workdir /tmp/w
   If it prints "FIX candidates.csv", correct those rows and run it again.

5. ROBINHOOD CHECKS (plan prints the batches)
   a) For each tradability batch, call get_equity_tradability(account_number="931184287", symbols=<batch>).
      UNTRADABLE = every symbol with tradeable=false, state other than "active", or missing from results.
      NONFRACTIONAL = every symbol whose fractional_tradability is not "tradable".
   b) For each historicals batch (always exactly 10 symbols), call get_equity_historicals once with symbols=<batch>, interval="day", adjustment_type="split", and start_time/end_time exactly as printed. Each result is saved to a file automatically; this is expected. Do not open those files.
   python3 ingest.py --workdir /tmp/w --expect 1
   (If ingest reports 0 files, run the find command it prints, then re-run with --results-dir <that directory>.)

6. BUILD
   python3 universe.py build --workdir /tmp/w --untradable <UNTRADABLE comma list> --nonfractional <NONFRACTIONAL comma list> --prev /tmp/w/prev_universe.csv
   (leave out any flag whose list is empty, and leave out --prev if step 2 found no universe)
   The script applies every build-time rule:
   - exclusions: mandate, tradability, AUM < $250M, 30-day median dollar volume < $5M, inception < 24 months
   - funds 24-39 months old go on-deck (active FALSE, eligible_from = the month they reach 39 months)
   - it computes beta_3y (reference only) and writes /tmp/w/out/universe.csv and /tmp/w/out/summary.txt
   If fewer than 60 funds are active, go back to step 3, widen the enumeration once, and rebuild.

7. WRITE AND VERIFY
   Call create_file(title="universe", parentId="18UKCu-XDK4D_lCOXdvoEMt7-lVliEAUu", contentMimeType="text/csv", textContent=<exact contents of /tmp/w/out/universe.csv>).
   Read the new file back once with read_file_content(<new id>). Write the fileContent to /tmp/w/raw_readback, then run:
     python3 drive_text.py table2csv /tmp/w/raw_readback /tmp/w/readback.csv
     python3 universe.py verify --workdir /tmp/w --readback /tmp/w/readback.csv
   If verify fails: trash only the NEW sheet, post the error to Slack, and stop.
   If it prints VERIFIED:
   - trash_file every OLDER file titled "universe" from step 2
   - create_file(title="universe_build_<today YYYY-MM-DD>.json", parentId=<folder>, contentMimeType="text/plain", textContent=<exact contents of /tmp/w/out/build_report.json>)

8. SLACK
   Post one message to C0C15AK3K2R: the exact contents of /tmp/w/out/summary.txt (already inside a ``` code block). After it, add at most three short lines: sources used for the enumeration, and any assumptions (for example, SEC yield used instead of distribution yield for N funds).
```

---

## Task 2: Monthly Top-5 Growth ETF Screen
**Schedule:** `0 9 1 * *` (UTC, the 1st of every month)

```
Run the monthly Top-5 Growth ETF screen for John's notification-only research system. Never place, review or cancel any order. John is not present, so do not ask questions.
Work mechanically. Every number, selection and sentence in the report comes from the repo scripts. Do not compute, summarize or reword market data yourself, and never open the saved Robinhood result files.

Fixed IDs: Drive folder 18UKCu-XDK4D_lCOXdvoEMt7-lVliEAUu ("Agentic ETF"). Slack channel C0C15AK3K2R. Robinhood account 931184287 (tradability check only). Repo https://github.com/olsenjm-dev/agentic-etf (scripts at the repo root).
If a step fails in a way these instructions don't cover, stop and post one Slack message to C0C15AK3K2R saying which step failed and the error line. Never post partial results as if they were complete.

1. SETUP
   git clone --depth 1 https://github.com/olsenjm-dev/agentic-etf.git /tmp/agentic-etf
   cd /tmp/agentic-etf && mkdir -p /tmp/w
   Run every command below from /tmp/agentic-etf.

2. READ DRIVE STATE
   Call search_files with query "parentId = '18UKCu-XDK4D_lCOXdvoEMt7-lVliEAUu'", excludeContentSnippets=true, pageSize=100. Follow next_page_token until the list is complete, and remember each file's title, id and modifiedTime.
   - If no file is titled "universe": post "Top-5 screen: no universe sheet in Drive - the Annual Universe Build task must run first. Nothing done." and stop.
   - Read the newest copy of each of these that exists: universe, strategy.json, current.json, performance.json, proposals.json. Do not read any other file.
     For each one, call read_file_content and write the returned fileContent verbatim to /tmp/w/raw_<title>. Then run:
       python3 drive_text.py table2csv /tmp/w/raw_universe /tmp/w/universe.csv
       python3 drive_text.py unescape /tmp/w/raw_<name>.json /tmp/w/<name>.json    (once for each JSON file you read)
     A schema_version error is a hard fail: post it and stop without writing anything.

3. PLAN AND IDEMPOTENCY
   python3 screen.py plan --workdir /tmp/w
   The plan prints a runlog title (e.g. runlog_2026-09.json). If a file with exactly that title is in the step-2 listing, post "Top-5 screen: <title> already exists - this anchor month already ran. Nothing done." and stop.

4. PRICES
   For EACH batch the plan prints (always exactly 10 symbols), make three get_equity_historicals calls with symbols=<that batch exactly>, interval="day", and start_time/end_time exactly as printed. Use adjustment_type="split" for the first call, "all" for the second and "none" for the third.
   Do not change batches or dates. Each result is saved to a file automatically; this is expected. Do not open those files. If a call returns an actual error (not the saved-to-file notice), retry that call once.
   python3 ingest.py --workdir /tmp/w
   (If it reports 0 files, run the find command it prints, then re-run with --results-dir <that directory>.)

5. SCREEN
   python3 screen.py run --workdir /tmp/w [--bootstrap-strategy ONLY if step 2 found no strategy.json]
   The output ends with "TRADABILITY CHECK LIST [...]". Call get_equity_tradability(account_number="931184287", symbols=<that list>).
   If any symbol has tradeable=false, a state other than "active", or is missing from the results, re-run the same command with --untradable SYM1,SYM2 added. Do this at most twice.

6. WRITE TO DRIVE
   Write files in the exact order printed under DRIVE WRITES (the same list is in /tmp/w/out/writes.json). For each entry:
   - Call create_file(title=<title>, parentId="18UKCu-XDK4D_lCOXdvoEMt7-lVliEAUu", contentMimeType=<mime>, textContent=<exact contents of /tmp/w/<path>>). Print the file with cat and copy it exactly.
   - If the entry says "replace", then trash_file every OLDER file with that same title from step 2. Never trash the file you just created, and never trash anything else.
   The runlog is written last on purpose. If any earlier write fails, stop before writing the runlog.

7. SLACK
   Post one message to C0C15AK3K2R whose text is exactly the contents of /tmp/w/out/report.txt (plain Slack markdown containing a real `|` pipe table - do NOT wrap it in a ``` code block and do NOT escape the table's structural pipes; send it exactly as written). If step 5 used --untradable, or step 6 had a problem, add one short line after it saying so.

For reference only (the scripts implement all of this; do not re-implement it):
- Anchor date: the last trading day of the prior month.
- Stage A: active funds at least 39 months old; on-deck funds turn active once their eligible_from month is reached.
- Stage B: never relaxed. The fund's total return must beat VOO by at least excess_min_pp on 6M, 1Y and 3Y.
- Stage C: score = weighted excess (6M annualized, 1Y, 3Y CAGR) - er_penalty x ER + incumbent_bonus. Information ratio is reported only.
- Stage D: greedy selection on residual correlation (vs VOO, 504 days) and category, with a max-drawdown tiebreak and reported relaxation steps.
- Stage E: a fund is removed on a Stage B failure or universe deactivation. Its first month outside the top five puts it on-watch; a second consecutive month removes it.
- Also tracked: an unconstrained top-5 comparator, and forward returns (list vs VOO vs comparator). strategy.json is read-only here, apart from the one-time bootstrap. proposals.json is never applied.
```

---

## Task 3: Quarterly Parameter Backtest
**Schedule:** `0 9 28 3,6,9,12 *` (UTC; runs a few days before the next monthly screen)

```
Run the quarterly parameter backtest for John's Top-5 Growth ETF Screen. This is analysis only: never place, review or cancel any order. John is not present, so do not ask questions.
strategy.json may change ONLY through the script's auto-apply rule: at least +2.0 pp annualized over the current parameters with a max drawdown no worse. Everything else goes to proposals.json for manual review. Do not compute or judge results yourself; the script does that.

Fixed IDs: Drive folder 18UKCu-XDK4D_lCOXdvoEMt7-lVliEAUu ("Agentic ETF"). Slack channel C0C15AK3K2R. Repo https://github.com/olsenjm-dev/agentic-etf (scripts at the repo root).
If a step fails in a way these instructions don't cover, stop and post one Slack message saying which step failed and the error line.

1. SETUP
   git clone --depth 1 https://github.com/olsenjm-dev/agentic-etf.git /tmp/agentic-etf
   cd /tmp/agentic-etf && mkdir -p /tmp/w

2. READ DRIVE STATE
   Call search_files with query "parentId = '18UKCu-XDK4D_lCOXdvoEMt7-lVliEAUu'", excludeContentSnippets=true, pageSize=100. Follow next_page_token until the list is complete, and remember each file's title, id and modifiedTime.
   Read the newest copy of each of these that exists: universe, strategy.json, performance.json, proposals.json. Do not read any other file.
   For each one, call read_file_content and write the fileContent verbatim to /tmp/w/raw_<title>. Then run:
     python3 drive_text.py table2csv /tmp/w/raw_universe /tmp/w/universe.csv
     python3 drive_text.py unescape /tmp/w/raw_<name>.json /tmp/w/<name>.json   (once for each JSON file read)
   A schema_version error is a hard fail: post it and stop. If there is no universe sheet, post that and stop.

3. PLAN
   python3 backtest.py plan --workdir /tmp/w
   - If it prints a line starting with "SKIP:", post "Top-5 quarterly backtest <today>: " plus that line to Slack and stop. Write nothing.
   - If the printed idempotency title (backtest_YYYY-MM.json) is already in the step-2 listing, post "Top-5 backtest already ran this quarter - nothing done." and stop.

4. PRICES
   For EACH printed batch (always exactly 10 symbols), make three get_equity_historicals calls with symbols=<batch exactly>, interval="day", and start_time/end_time exactly as printed. Use adjustment_type="split" for the first call, "all" for the second and "none" for the third.
   Results are saved to files automatically; do not open them. Retry an actual error once.
   python3 ingest.py --workdir /tmp/w
   (If it reports 0 files, run the find command it prints, then re-run with --results-dir <that directory>.)

5. RUN
   python3 backtest.py run --workdir /tmp/w
   The script handles everything:
   - It reconstructs the monthly screen for every month-end the 10-year history allows and tests at most 10 variants. Each variant changes one group: the three weights, resid_corr_max, er_penalty, incumbent_bonus, or raw vs information-ratio scoring.
   - If a variant qualifies, it auto-applies the best one: bumps the strategy.json version and appends a changelog entry with the numbers.
   - Other variants with a gain of at least 0.5 pp go to proposals.json.
   - The report states the survivorship-bias caveat and the early-history noise warning.

6. WRITE TO DRIVE
   Write files in the exact order printed under DRIVE WRITES (also in /tmp/w/out/writes.json). For each entry:
   - Call create_file(title=<title>, parentId="18UKCu-XDK4D_lCOXdvoEMt7-lVliEAUu", contentMimeType=<mime>, textContent=<exact contents of /tmp/w/<path>>).
   - If the entry says "replace", then trash_file every OLDER file with that same title from step 2. Never trash anything else.
   The backtest record is written last.

7. SLACK
   Post one message to C0C15AK3K2R whose text is exactly the contents of /tmp/w/out/report.txt (already inside a ``` code block). It lists the candidates tested, the result for each, what was auto-applied (if anything), and what is waiting in proposals.json.
```
