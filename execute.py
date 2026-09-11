#!/usr/bin/env python3
"""
Intraday execution script for the Agentic ETF system. Two phases:

  check   no files needed: prints run label and whether the NYSE is open now (exit 1 if closed)
  plan    compute indicator states for every ticker in prices_daily.csv, decide buys,
          write orders.json (what the task should place) and report.txt (draft)
  record  after orders are placed, record fills -> runlog_<date>_<run>.csv (write-once audit file),
          cooldowns + running dip-vs-DCA ledger in state.json, final report.txt

Inputs (workdir): config.json, strategy.json, candidates.json, state.json,
                  prices_daily.csv (>= 1 year of daily bars for candidates + holdings; from ingest.py)
Runtime values (small, passed on the command line by the task):
  --quotes   "SOXX=528.01,CIBR=94.19,..."          live last-trade prices
  --positions "ITA=0.104408@229.87,SMH=0.028988@551.95,..."   quantity@avg_cost
  --buying-power 64.00
  --run 0910|1410         label for the log
  --now  ISO timestamp override (default: now, America/Chicago)
record only:
  --filled "SOXX=1.00,XBI=2.00"   dollars actually filled per ticker (omit a ticker = nothing filled)
"""
import argparse, json, os, sys
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd

CT = ZoneInfo("America/Chicago"); ET = ZoneInfo("America/New_York")
IND = ["n_day_low", "pct_off_52w_high", "bollinger_lower"]
IND_SHORT = {"n_day_low": "L", "pct_off_52w_high": "H", "bollinger_lower": "B"}

# ----------------------------------------------------------------------------- market calendar (no dependency)

def easter(y):
    a=y%19; b=y//100; c=y%100; d=b//4; e=b%4; f=(b+8)//25; g=(b-f+1)//3
    h=(19*a+b-d-g+15)%30; i=c//4; k=c%4; l=(32+2*e+2*i-h-k)%7; m=(a+11*h+22*l)//451
    mo=(h+l-7*m+114)//31; da=((h+l-7*m+114)%31)+1
    return date(y, mo, da)

def nth_weekday(y, m, weekday, n):
    d = date(y, m, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)

def last_weekday(y, m, weekday):
    d = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)

def observed(d):
    if d.weekday() == 5: return d - timedelta(days=1)
    if d.weekday() == 6: return d + timedelta(days=1)
    return d

def nyse_holidays(y):
    return {observed(date(y,1,1)), nth_weekday(y,1,0,3), nth_weekday(y,2,0,3), easter(y)-timedelta(days=2),
            last_weekday(y,5,0), observed(date(y,6,19)), observed(date(y,7,4)), nth_weekday(y,9,0,1),
            nth_weekday(y,11,3,4), observed(date(y,12,25))}

def market_open_today(now_et):
    d = now_et.date()
    if d.weekday() >= 5 or d in nyse_holidays(d.year):
        return False
    t = now_et.time()
    return (t >= datetime.strptime("09:30", "%H:%M").time()) and (t <= datetime.strptime("16:00", "%H:%M").time())

# ----------------------------------------------------------------------------- helpers

def load_json(p, default=None):
    if os.path.exists(p):
        with open(p) as f: return json.load(f)
    return default if default is not None else {}

def save_json(p, o):
    with open(p, "w") as f: json.dump(o, f, indent=2, default=str)

def parse_kv(s, sep="="):
    out = {}
    for part in (s or "").split(","):
        part = part.strip()
        if not part: continue
        k, v = part.split(sep, 1)
        out[k.strip().upper()] = v.strip()
    return out

def indicators_for(daily, price, today, P):
    """daily: DataFrame for one ticker (close, high, low) ascending; bars dated today are excluded from 'prior'."""
    prior = daily[daily.index.date < today]
    out, detail = {}, {}
    n = int(P["n_day_low"]["n"])
    lowN = float(prior["low"].tail(n).min()) if len(prior) >= n else np.nan
    out["n_day_low"] = bool(P["n_day_low"].get("enabled", True) and price <= lowN)
    detail["low_n"] = lowN
    hi = float(prior["high"].tail(252).max()) if len(prior) >= 120 else np.nan
    thr = hi * (1 - float(P["pct_off_52w_high"]["pct"]) / 100.0)
    out["pct_off_52w_high"] = bool(P["pct_off_52w_high"].get("enabled", True) and price <= thr)
    detail["high_52w"] = hi; detail["off_high_pct"] = (price / hi - 1) * 100 if hi else np.nan
    per, k = int(P["bollinger_lower"]["period"]), float(P["bollinger_lower"]["num_std"])
    closes = list(prior["close"].tail(per - 1)) + [price]
    if len(closes) >= per:
        arr = np.array(closes); lower = arr.mean() - k * arr.std(ddof=0)
    else:
        lower = np.nan
    out["bollinger_lower"] = bool(P["bollinger_lower"].get("enabled", True) and price <= lower)
    detail["bb_lower"] = lower
    return out, detail

# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["check", "plan", "record"])
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--quotes", default=""); ap.add_argument("--positions", default="")
    ap.add_argument("--buying-power", type=float, default=None)
    ap.add_argument("--run", default=""); ap.add_argument("--now", default=None)
    ap.add_argument("--filled", default="")
    ap.add_argument("--force-open", action="store_true", help="bypass market-open check (testing)")
    a = ap.parse_args()
    W = a.workdir
    now = datetime.fromisoformat(a.now).astimezone(CT) if a.now else datetime.now(CT)
    now_et = now.astimezone(ET); today = now_et.date()

    if a.phase == "check":   # needs no files: is the market open right now? (exit 0 = open, 1 = closed)
        run = a.run or ("0910" if now.hour < 12 else "1410")
        is_open = a.force_open or market_open_today(now_et)
        print(f"{now.strftime('%a %Y-%m-%d %H:%M %Z')} run {run}: market {'OPEN' if is_open else 'CLOSED'}")
        sys.exit(0 if is_open else 1)

    cfg = load_json(os.path.join(W, "config.json")); ex = cfg["execution"]
    strat = load_json(os.path.join(W, "strategy.json")); P = strat["indicators"]
    cand = load_json(os.path.join(W, "candidates.json"), {"candidates": []})
    state = load_json(os.path.join(W, "state.json"), {"cooldowns": {}, "ledger": {"tickers": {}}})
    candidates = [c.upper() for c in cand.get("candidates", [])]
    quotes = {k: float(v) for k, v in parse_kv(a.quotes).items()}
    positions = {}
    for k, v in parse_kv(a.positions).items():
        q, c = (v.split("@") + ["nan"])[:2]
        positions[k] = {"qty": float(q), "avg_cost": float(c)}
    bp = a.buying_power

    if a.phase == "plan":
        plan_path = os.path.join(W, "orders.json")
        if not (a.force_open or market_open_today(now_et)):
            msg = f"AGENTIC ETF {now.strftime('%Y-%m-%d %H:%M %Z')} run {a.run}: market closed - no action."
            save_json(plan_path, {"orders": [], "market_open": False, "now": now.isoformat()})
            open(os.path.join(W, "report.txt"), "w").write(msg); print(msg); return
        px = pd.read_csv(os.path.join(W, "prices_daily.csv"), parse_dates=["date"])
        px["ticker"] = px["ticker"].str.upper()
        by = {t: g.set_index("date").sort_index()[["close", "high", "low"]] for t, g in px.groupby("ticker")}
        cds = state.setdefault("cooldowns", {})
        rows, orders = [], []
        tickers = sorted(set(candidates) | set(positions) | set(quotes))
        for t in tickers:
            price = quotes.get(t)
            if price is None or t not in by:
                rows.append({"ticker": t, "price": price, "flags": {}, "detail": {}, "n": 0, "dollars": 0.0,
                             "note": "no quote" if price is None else "no daily bars"}); continue
            flags, detail = indicators_for(by[t], price, today, P)
            eligible = [i for i in IND if flags[i] and cds.get(t, {}).get(i) != today.isoformat()]
            dollars = 0.0
            if t in candidates and eligible:
                dollars = round(len(eligible) * float(ex["dollars_per_indicator"]), 2)
            rows.append({"ticker": t, "price": price, "flags": flags, "detail": detail,
                         "n": sum(flags.values()), "dollars": dollars, "eligible": eligible,
                         "note": "" if t in candidates else "not a candidate"})
        # cash floor: most-tripped first
        floor = float(ex["buying_power_floor"]); avail = (bp if bp is not None else 0.0) - floor
        for r in sorted([r for r in rows if r["dollars"] > 0], key=lambda r: -r["n"]):
            if r["dollars"] <= avail + 1e-9:
                avail -= r["dollars"]
                orders.append({"ticker": r["ticker"], "dollars": r["dollars"], "indicators": r["eligible"],
                               "type": ex["order_type"], "market_hours": ex["market_hours"]})
            else:
                r["note"] = f"skipped: buying power floor ${floor:.0f}"; r["dollars"] = 0.0
        save_json(plan_path, {"orders": orders, "market_open": True, "now": now.isoformat(), "run": a.run,
                              "rows": rows, "buying_power": bp})
        print(json.dumps({"orders": orders}, indent=1))
        write_report(W, cfg, cand, rows, orders, positions, quotes, bp, now, a.run, strat, filled=None)
        return

    # record phase
    plan = load_json(os.path.join(W, "orders.json"), {})
    rows = plan.get("rows", [])
    filled = {k: float(v) for k, v in parse_kv(a.filled).items()}
    cds = state.setdefault("cooldowns", {})
    log_rows = []
    for r in rows:
        if r["ticker"] not in candidates:
            continue  # run log holds candidates only, so the DCA counterfactual starts when a ticker becomes one
        f = filled.get(r["ticker"], 0.0)
        planned = r.get("dollars", 0.0)
        if f > 0:
            for i in r.get("eligible", []):
                cds.setdefault(r["ticker"], {})[i] = today.isoformat()
        note = r.get("note", "")
        if planned > 0 and f <= 0: note = "ORDER NOT FILLED"
        elif planned > 0 and abs(f - planned) > 0.005: note = f"partial: planned {planned:.2f}"
        log_rows.append({"ts": now.isoformat(), "run": a.run, "ticker": r["ticker"], "price": r.get("price"),
                         **{i: int(bool(r.get("flags", {}).get(i, False))) for i in IND},
                         "n_tripped": r.get("n", 0), "dollars": f, "note": note})
        r["filled"] = f
    # prune cooldowns older than today
    for t in list(cds):
        cds[t] = {i: d for i, d in cds[t].items() if d == today.isoformat()}
        if not cds[t]: del cds[t]
    # running ledger for the dip-vs-DCA counterfactual (so old logs never need re-reading)
    ledger = state.setdefault("ledger", {"tickers": {}})
    for r in log_rows:
        if r["price"] in (None, 0) or (isinstance(r["price"], float) and np.isnan(r["price"])):
            continue
        L = ledger["tickers"].setdefault(r["ticker"], {"runs": 0, "sum_inv_price": 0.0, "actual_dollars": 0.0, "actual_shares": 0.0})
        L["runs"] += 1
        L["sum_inv_price"] += 1.0 / float(r["price"])
        L["actual_dollars"] += float(r["dollars"])
        L["actual_shares"] += float(r["dollars"]) / float(r["price"])
    save_json(os.path.join(W, "state.json"), state)
    # write-once audit log for this run (upload as-is; never re-read by the tasks)
    logp = os.path.join(W, f"runlog_{today.isoformat()}_{a.run}.csv")
    pd.DataFrame(log_rows).to_csv(logp, index=False)
    write_report(W, cfg, cand, rows, plan.get("orders", []), positions, quotes, bp, now, a.run, strat, filled=filled)

def write_report(W, cfg, cand, rows, orders, positions, quotes, bp, now, run, strat, filled):
    candidates = set(c.upper() for c in cand.get("candidates", []))
    L = [f"AGENTIC ETF  {now.strftime('%a %Y-%m-%d %H:%M %Z')}  run {run}  strategy v{strat.get('version')}  ({'PLAN' if filled is None else 'FILLED'})"]
    L.append(f"{'ticker':<6} {'cand':<4} {'shares':>9} {'price':>8} {'equity':>8} {'avgcost':>8} {'P/L%':>6}  {'L H B':<5} {'off-hi':>6} {'buy$':>5}  note")
    tot_eq = 0.0
    for r in sorted(rows, key=lambda r: (r["ticker"] not in candidates, r["ticker"])):
        t = r["ticker"]; p = positions.get(t, {}); q = p.get("qty", 0.0); price = r.get("price") or 0.0
        eq = q * price; tot_eq += eq
        pl = (price / p["avg_cost"] - 1) * 100 if p.get("avg_cost") and price else np.nan
        fl = r.get("flags", {}); flag_s = " ".join(IND_SHORT[i] if fl.get(i) else "." for i in IND) if fl else "-"
        off = r.get("detail", {}).get("off_high_pct", np.nan)
        dollars = r.get("dollars", 0.0) if filled is None else r.get("filled", 0.0)
        L.append(f"{t:<6} {'Y' if t in candidates else '-':<4} {q:>9.6f} {price:>8.2f} {eq:>8.2f} "
                 f"{(p.get('avg_cost') or 0):>8.2f} {('%+.1f' % pl) if not np.isnan(pl) else '-':>6}  {flag_s:<5} "
                 f"{('%+.1f' % off) if not (isinstance(off, float) and np.isnan(off)) else '-':>6} {dollars:>5.2f}  {r.get('note','')}")
    L.append("")
    total_buy = sum(o["dollars"] for o in orders) if filled is None else sum(filled.values())
    L.append(f"L=at/below {strat['indicators']['n_day_low']['n']}-day low  H={strat['indicators']['pct_off_52w_high']['pct']:.0f}% off 52w high  B=below lower Bollinger({strat['indicators']['bollinger_lower']['period']},{strat['indicators']['bollinger_lower']['num_std']})")
    L.append(f"equity {tot_eq:.2f}  buying power {bp if bp is not None else float('nan'):.2f}  buys this run {total_buy:.2f}  floor {cfg['execution']['buying_power_floor']:.0f}")
    dropped = sorted(t for t in positions if t not in candidates)
    if dropped:
        L.append(f"NOTICE holdings not in current candidate list (no auto-sell): {', '.join(dropped)}")
    rep = "\n".join(L)
    open(os.path.join(W, "report.txt"), "w").write(rep)
    print(rep)

    # report.md: same content as a Markdown pipe table (Slack renders it as a native table;
    # a ``` code block wraps unreadably on phones).
    M = [f"*AGENTIC ETF {now.strftime('%a %Y-%m-%d %H:%M %Z')}* · run {run} · strategy v{strat.get('version')} · {'PLAN' if filled is None else 'FILLED'}", ""]
    M.append("| Ticker | Cand | Shares | Price | Equity | Avg cost | P/L % | L H B | Off-hi | Buy $ | Note |")
    M.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in sorted(rows, key=lambda r: (r["ticker"] not in candidates, r["ticker"])):
        t = r["ticker"]; p = positions.get(t, {}); q = p.get("qty", 0.0); price = r.get("price") or 0.0
        pl = (price / p["avg_cost"] - 1) * 100 if p.get("avg_cost") and price else np.nan
        fl = r.get("flags", {}); flag_s = " ".join(IND_SHORT[i] if fl.get(i) else "." for i in IND) if fl else "-"
        off = r.get("detail", {}).get("off_high_pct", np.nan)
        dollars = r.get("dollars", 0.0) if filled is None else r.get("filled", 0.0)
        M.append(f"| {t} | {'Y' if t in candidates else '-'} | {q:.6f} | {price:.2f} | {q*price:.2f} | {(p.get('avg_cost') or 0):.2f} | "
                 f"{('%+.1f' % pl) if not np.isnan(pl) else '-'} | {flag_s} | "
                 f"{('%+.1f' % off) if not (isinstance(off, float) and np.isnan(off)) else '-'} | {dollars:.2f} | {r.get('note','')} |")
    M.append("")
    M.append(L[-3] if dropped else L[-2])   # indicator legend line
    M.append(f"Equity {tot_eq:.2f} · buying power {bp if bp is not None else float('nan'):.2f} · buys this run {total_buy:.2f} · floor {cfg['execution']['buying_power_floor']:.0f}")
    if dropped:
        M.append(f"*NOTICE* holdings not in current candidate list (no auto-sell): {', '.join(dropped)}")
    open(os.path.join(W, "report.md"), "w").write("\n".join(M))

if __name__ == "__main__":
    main()
