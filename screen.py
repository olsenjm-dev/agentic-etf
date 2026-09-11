#!/usr/bin/env python3
"""
Monthly Strategy & Universe script for the Agentic ETF system.

Runs entirely on local files in a working directory (default: ./work).
The scheduled task is responsible for fetching data from Robinhood and
writing the CSV inputs; this script does all the arithmetic so that no
reasoning tokens are spent on math.

Inputs (in workdir):
  universe.csv          ticker,name,category,expense_ratio_pct,note
  config.json           see config.json
  strategy.json         current dip-indicator parameters
  state.json            (optional) prune streaks + cooldowns; created if missing
  prices_monthly.csv    date,ticker,close   -- month-end closes, >= 37 months, all universe tickers + VOO
  prices_weekly.csv     date,ticker,close   -- (optional) weekly closes for survivors + VOO, used for correlation
  volumes.csv           ticker,avg_volume   -- (optional) average daily volume for liquidity floor
  (the running dip-vs-DCA ledger lives in state.json["ledger"], maintained by execute.py)
  prices_daily.csv      date,ticker,close,high,low -- (optional) only in backtest months

Outputs (in workdir):
  candidates.json       chosen candidate per group (small; read by every intraday run)
  screen_detail.json    full per-ticker stats, exclusions (monthly record only)
  state.json            updated prune streaks (cooldowns and ledger untouched)
  strategy.json         updated only if a backtest passed the auto-apply gate
  report.txt            Slack-ready monospaced report

Usage:  python3 screen.py [--workdir DIR] [--backtest] [--force-backtest]
"""
import argparse, json, math, os, sys, glob
from datetime import date, datetime
import numpy as np
import pandas as pd

# ----------------------------------------------------------------------------- io helpers

def load_json(path, default=None):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default if default is not None else {}

def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, default=str)

def load_prices(path):
    """Long CSV (date,ticker,close[,high,low]) -> wide DataFrame of closes indexed by date."""
    df = pd.read_csv(path, parse_dates=["date"])
    df["ticker"] = df["ticker"].str.upper().str.strip()
    wide = df.pivot_table(index="date", columns="ticker", values="close", aggfunc="last").sort_index()
    return wide

# ----------------------------------------------------------------------------- screening

def total_return(wide, ticker, months):
    s = wide[ticker].dropna()
    if len(s) < months + 1:
        return np.nan
    return s.iloc[-1] / s.iloc[-1 - months] - 1.0

def annualized(tr, years):
    if pd.isna(tr):
        return np.nan
    return (1.0 + tr) ** (1.0 / years) - 1.0

def monthly_returns(wide):
    return wide.pct_change().dropna(how="all")

def consecutive_months_under(mret, ticker, bench):
    """Count of most-recent consecutive months where ticker's monthly return < benchmark's."""
    diff = (mret[ticker] - mret[bench]).dropna()
    n = 0
    for v in diff.iloc[::-1]:
        if v < 0:
            n += 1
        else:
            break
    return n

def correlation_groups(ret, threshold):
    """
    Average-linkage agglomerative clustering on 1 - corr.
    Merge while the average pairwise correlation between two clusters >= threshold.
    Returns list of lists of tickers (deterministic order).
    """
    tickers = list(ret.columns)
    if len(tickers) <= 1:
        return [tickers]
    corr = ret.corr().values
    clusters = [[i] for i in range(len(tickers))]
    while True:
        best, bi, bj = -1.0, -1, -1
        for a in range(len(clusters)):
            for b in range(a + 1, len(clusters)):
                vals = [corr[i, j] for i in clusters[a] for j in clusters[b]]
                avg = float(np.mean(vals))
                if avg > best:
                    best, bi, bj = avg, a, b
        if best < threshold or bi < 0:
            break
        clusters[bi] = clusters[bi] + clusters[bj]
        del clusters[bj]
    groups = [sorted(tickers[i] for i in c) for c in clusters]
    groups.sort(key=lambda g: (-len(g), g[0]))
    return groups

def group_cross_corr(ret, groups):
    """Average correlation between each pair of groups (for the report)."""
    corr = ret.corr()
    out = []
    for a in range(len(groups)):
        for b in range(a + 1, len(groups)):
            vals = [corr.loc[i, j] for i in groups[a] for j in groups[b]]
            out.append((a + 1, b + 1, float(np.mean(vals))))
    return out

# ----------------------------------------------------------------------------- ledger / counterfactual

def fold_runlogs_into_ledger(workdir, ledger):
    """
    Each runlog row records, for every candidate at every run: price, and dollars actually bought.
    Actual:         sum(dollars), sum(dollars/price)  -> cost basis
    Counterfactual: same total dollars spread evenly over every run observed -> blind-DCA cost basis.
    We keep per-ticker running sums so old logs never need to be re-read.
    """
    files = sorted(glob.glob(os.path.join(workdir, "runlog_*.csv")))
    folded = set(ledger.get("folded_files", []))
    for path in files:
        name = os.path.basename(path)
        if name in folded:
            continue
        df = pd.read_csv(path)
        if df.empty:
            folded.add(name); continue
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df["dollars"] = pd.to_numeric(df.get("dollars", 0), errors="coerce").fillna(0.0)
        df = df.dropna(subset=["price"])
        for t, g in df.groupby("ticker"):
            L = ledger.setdefault("tickers", {}).setdefault(t, {
                "runs": 0, "sum_inv_price": 0.0, "actual_dollars": 0.0, "actual_shares": 0.0})
            L["runs"] += int(len(g))
            L["sum_inv_price"] += float((1.0 / g["price"]).sum())
            L["actual_dollars"] += float(g["dollars"].sum())
            L["actual_shares"] += float((g["dollars"] / g["price"]).sum())
        folded.add(name)
    ledger["folded_files"] = sorted(folded)
    return ledger

def counterfactual_table(ledger):
    rows = []
    for t, L in ledger.get("tickers", {}).items():
        if L["actual_dollars"] <= 0 or L["runs"] == 0:
            continue
        actual_cb = L["actual_dollars"] / L["actual_shares"]
        # blind DCA: same dollars, equal slice every run -> shares = D/runs * sum(1/p)
        cf_shares = (L["actual_dollars"] / L["runs"]) * L["sum_inv_price"]
        cf_cb = L["actual_dollars"] / cf_shares
        rows.append({"ticker": t, "dollars": L["actual_dollars"], "dip_cost_basis": actual_cb,
                     "dca_cost_basis": cf_cb, "advantage_pct": (cf_cb / actual_cb - 1.0) * 100.0})
    return pd.DataFrame(rows)

# ----------------------------------------------------------------------------- backtest of dip rules

def indicator_flags(daily, p):
    """daily: DataFrame with close, high, low for one ticker (ascending). Returns DataFrame of bool flags."""
    c, h, l = daily["close"], daily["high"], daily["low"]
    n = int(p["n_day_low"]["n"])
    prior_low = l.shift(1).rolling(n).min()
    f1 = c <= prior_low
    hi52 = h.shift(1).rolling(252, min_periods=120).max()
    f2 = c <= hi52 * (1.0 - float(p["pct_off_52w_high"]["pct"]) / 100.0)
    per, k = int(p["bollinger_lower"]["period"]), float(p["bollinger_lower"]["num_std"])
    sma = c.rolling(per).mean(); sd = c.rolling(per).std(ddof=0)
    f3 = c <= sma - k * sd
    out = pd.DataFrame({"n_day_low": f1, "pct_off_52w_high": f2, "bollinger_lower": f3}).fillna(False)
    for k_ in out.columns:
        if not p[k_].get("enabled", True):
            out[k_] = False
    return out

MIN_BARS = 300

def backtest_params(daily_by_ticker, params, dollars_per_ind):
    """
    Simulate: each day, buy dollars_per_ind per tripped indicator (once per day per indicator).
    Metric: advantage of dip cost basis over blind DCA of the same dollars across all days (pp),
    averaged across tickers; plus worst drawdown of position value vs cost.
    """
    advs, dds = [], []
    for t, d in daily_by_ticker.items():
        d = d.dropna().sort_index()
        if len(d) < MIN_BARS:
            continue
        flags = indicator_flags(d, params)
        n_trip = flags.sum(axis=1)
        dollars = n_trip * dollars_per_ind
        total = float(dollars.sum())
        if total <= 0:
            continue
        shares = (dollars / d["close"]).cumsum()
        cost = dollars.cumsum()
        actual_cb = total / float(shares.iloc[-1])
        cf_shares = (total / len(d)) * float((1.0 / d["close"]).sum())
        cf_cb = total / cf_shares
        advs.append((cf_cb / actual_cb - 1.0) * 100.0)
        value = shares * d["close"]
        pnl = (value / cost.replace(0, np.nan) - 1.0).fillna(0.0)
        dds.append(float(pnl.min()))
    if not advs:
        return None
    return {"advantage_pp": float(np.mean(advs)), "worst_drawdown": float(np.min(dds)), "tickers": len(advs)}

def parameter_grid(base):
    """Small neighbourhood grid around the current parameters (keeps the search honest and cheap)."""
    grid = []
    for n in sorted({3, 5, 10, base["n_day_low"]["n"]}):
        for pct in sorted({5.0, 10.0, 15.0, 20.0, float(base["pct_off_52w_high"]["pct"])}):
            for per, k in {(20, 2.0), (20, 2.5), (10, 2.0), (50, 2.0),
                           (int(base["bollinger_lower"]["period"]), float(base["bollinger_lower"]["num_std"]))}:
                p = json.loads(json.dumps(base))
                p["n_day_low"]["n"] = n
                p["pct_off_52w_high"]["pct"] = pct
                p["bollinger_lower"]["period"] = per
                p["bollinger_lower"]["num_std"] = k
                grid.append(p)
    return grid

def run_backtest(workdir, cfg, strategy, candidates, today):
    path = os.path.join(workdir, "prices_daily.csv")
    if not os.path.exists(path):
        return None, "backtest skipped: prices_daily.csv not present"
    df = pd.read_csv(path, parse_dates=["date"])
    df["ticker"] = df["ticker"].str.upper().str.strip()
    by = {t: g.set_index("date")[["close", "high", "low"]] for t, g in df.groupby("ticker") if t in candidates}
    if not by:
        return None, "backtest skipped: no candidate tickers in prices_daily.csv"
    dpi = float(cfg["execution"]["dollars_per_indicator"])
    base = strategy["indicators"]
    current = backtest_params(by, base, dpi)
    if current is None:
        return None, "backtest skipped: fewer than %d daily bars" % MIN_BARS + " for every candidate"
    best, best_p = current, base
    for p in parameter_grid(base):
        r = backtest_params(by, p, dpi)
        if r is None:
            continue
        if r["advantage_pp"] > best["advantage_pp"] + 1e-9:
            best, best_p = r, p
    gate = cfg["strategy_adaptation"]
    improvement = best["advantage_pp"] - current["advantage_pp"]
    dd_ok = (not gate["auto_apply_require_drawdown_not_worse"]) or (best["worst_drawdown"] >= current["worst_drawdown"] - 1e-9)
    applied = improvement >= float(gate["auto_apply_min_improvement_pp"]) and dd_ok and best_p != base
    msg = (f"backtest ({len(by)} tickers): current adv {current['advantage_pp']:+.2f}pp dd {current['worst_drawdown']*100:.1f}% | "
           f"best adv {best['advantage_pp']:+.2f}pp dd {best['worst_drawdown']*100:.1f}% | "
           f"improvement {improvement:+.2f}pp -> {'APPLIED' if applied else 'not applied'}")
    if applied:
        strategy["indicators"] = best_p
        strategy["version"] = int(strategy.get("version", 1)) + 1
        strategy["as_of"] = today.isoformat()
        strategy.setdefault("history", []).append({
            "version": strategy["version"], "as_of": today.isoformat(),
            "reason": msg, "params": best_p})
        save_json(os.path.join(workdir, "strategy.json"), strategy)
    return applied, msg

# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--backtest", action="store_true", help="run backtest if this is a backtest month")
    ap.add_argument("--force-backtest", action="store_true")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD override")
    ap.add_argument("--fold-local-logs", action="store_true", help="offline use: fold runlog_*.csv files in workdir into the ledger")
    a = ap.parse_args()
    W = a.workdir
    today = date.fromisoformat(a.today) if a.today else date.today()

    cfg = load_json(os.path.join(W, "config.json"))
    strategy = load_json(os.path.join(W, "strategy.json"))
    state = load_json(os.path.join(W, "state.json"), {"cooldowns": {}, "ledger": {"tickers": {}}})
    uni = pd.read_csv(os.path.join(W, "universe.csv"))
    uni["ticker"] = uni["ticker"].str.upper().str.strip()
    bench = cfg.get("benchmark", "VOO")
    sc = cfg["screening"]

    monthly = load_prices(os.path.join(W, "prices_monthly.csv"))
    if bench not in monthly.columns:
        sys.exit(f"benchmark {bench} missing from prices_monthly.csv")
    mret = monthly_returns(monthly)

    # optional liquidity data
    vol = {}
    vp = os.path.join(W, "volumes.csv")
    if os.path.exists(vp):
        v = pd.read_csv(vp); vol = dict(zip(v["ticker"].str.upper(), pd.to_numeric(v["avg_volume"], errors="coerce")))

    rows, excluded = [], []
    b1 = total_return(monthly, bench, 12); b3 = total_return(monthly, bench, 36)
    for _, u in uni.iterrows():
        t = u["ticker"]
        if t == bench or str(u.get("category", "")) == "benchmark":
            continue
        if t not in monthly.columns:
            excluded.append((t, "no price data")); continue
        r1, r3 = total_return(monthly, t, 12), total_return(monthly, t, 36)
        if pd.isna(r1) or pd.isna(r3):
            excluded.append((t, "insufficient history (<37 months)")); continue
        if t in vol and not pd.isna(vol[t]) and vol[t] < sc["min_avg_daily_volume_shares"]:
            excluded.append((t, f"avg volume {vol[t]:,.0f} < floor")); continue
        streak = consecutive_months_under(mret, t, bench)
        beat1, beat3 = r1 > b1, r3 > b3
        pruned = streak >= int(sc["prune_after_consecutive_months_under_benchmark"])
        er = float(u["expense_ratio_pct"]) / 100.0
        score = (annualized(r1, 1) + annualized(r3, 3)) / 2.0 - float(sc["fee_weight"]) * er
        passes = (beat1 or not sc["require_beat_benchmark_1y"]) and (beat3 or not sc["require_beat_benchmark_3y"]) and not pruned
        rows.append({"ticker": t, "category": u["category"], "er_pct": float(u["expense_ratio_pct"]),
                     "ret_1y": r1, "ret_3y": r3, "beat_1y": beat1, "beat_3y": beat3,
                     "under_streak": streak, "pruned": pruned, "score": score, "passes": passes})
    stats = pd.DataFrame(rows).set_index("ticker")
    survivors = list(stats.index[stats["passes"]])

    # correlation grouping on survivors
    wp = os.path.join(W, "prices_weekly.csv")
    if os.path.exists(wp):
        weekly = load_prices(wp)
        src = weekly.tail(int(sc["correlation_window_weeks"]))
        corr_src = "weekly"
    else:
        src = monthly.tail(36); corr_src = "monthly (weekly file absent)"
    avail = [t for t in survivors if t in src.columns]
    ret = src[avail].pct_change().dropna(how="all")
    groups = correlation_groups(ret, float(sc["correlation_group_threshold"])) if avail else []
    cross = group_cross_corr(ret, groups) if len(groups) > 1 else []

    chosen, group_out = [], []
    for gi, g in enumerate(groups, 1):
        ranked = stats.loc[g].sort_values("score", ascending=False)
        best = ranked.index[0]
        chosen.append(best)
        group_out.append({"group": gi, "members": g, "chosen": best,
                          "scores": {t: round(float(stats.loc[t, "score"]) * 100, 2) for t in g}})

    # holdings that fell out (execute.py also checks, but flag here for the monthly report)
    prev = load_json(os.path.join(W, "candidates.json"), {})
    prev_chosen = set(prev.get("candidates", []))
    dropped = sorted(prev_chosen - set(chosen))
    added = sorted(set(chosen) - prev_chosen)

    # counterfactual from the running ledger kept in state.json by execute.py
    # (local runlog_*.csv files, if any are present, are folded in too — useful for offline analysis)
    ledger = state.setdefault("ledger", {"tickers": {}})
    if a.fold_local_logs:
        ledger = fold_runlogs_into_ledger(W, ledger)
    cf = counterfactual_table(ledger)

    # backtest (quarterly by default)
    bt_msg, bt_applied = "backtest not scheduled this month", None
    if a.force_backtest or (a.backtest and today.month in cfg["strategy_adaptation"]["backtest_months"]):
        bt_applied, bt_msg = run_backtest(W, cfg, strategy, survivors, today)

    # outputs
    out = {"as_of": today.isoformat(), "benchmark": bench,
           "benchmark_ret_1y": b1, "benchmark_ret_3y": b3,
           "candidates": chosen, "groups": group_out, "survivors": survivors,
           "added": added, "dropped": dropped, "correlation_source": corr_src,
           "strategy_version": strategy.get("version"),
           "stats": {t: {k: (None if (isinstance(v, float) and math.isnan(v)) else (bool(v) if isinstance(v, (np.bool_, bool)) else (float(v) if isinstance(v, (float, np.floating)) else (int(v) if isinstance(v, (np.integer,)) else v))))
                         for k, v in r.items()} for t, r in stats.to_dict("index").items()},
           "excluded": excluded}
    # candidates.json stays small (it is re-read by every intraday run); details go to screen_detail.json
    small = {k: out[k] for k in ("as_of", "benchmark", "candidates", "groups", "added", "dropped", "strategy_version")}
    save_json(os.path.join(W, "candidates.json"), small)
    save_json(os.path.join(W, "screen_detail.json"), out)
    save_json(os.path.join(W, "state.json"), state)

    # report
    L = []
    L.append(f"AGENTIC ETF - MONTHLY SCREEN  {today.isoformat()}   strategy v{strategy.get('version')}")
    L.append(f"{bench}: 1y {b1*100:+.1f}%  3y {b3*100:+.1f}%   universe {len(stats)}  survivors {len(survivors)}  groups {len(groups)}")
    L.append("")
    L.append("CANDIDATES (one per correlation group; score = avg ann. return - ER)")
    L.append(f"{'grp':>3} {'ticker':<6} {'score':>7} {'1y':>7} {'3y':>7} {'ER':>5}  members")
    for g in group_out:
        t = g["chosen"]; s = stats.loc[t]
        L.append(f"{g['group']:>3} {t:<6} {s['score']*100:>6.1f}% {s['ret_1y']*100:>6.1f}% {s['ret_3y']*100:>6.1f}% {s['er_pct']:>4.2f}  {', '.join(m for m in g['members'] if m != t) or '-'}")
    if cross:
        L.append("")
        L.append("Between-group avg correlation: " + "  ".join(f"g{a_}-g{b_} {c:.2f}" for a_, b_, c in cross))
    if added or dropped:
        L.append("")
        L.append(f"ADDED: {', '.join(added) or '-'}   DROPPED: {', '.join(dropped) or '-'}  (holdings in dropped are NOT sold - notice only)")
    L.append("")
    L.append("PRUNED / FAILED SCREEN")
    fails = stats[~stats["passes"]].sort_values("score", ascending=False)
    for t, s in fails.iterrows():
        if s["pruned"]:
            why = "pruned: %d mo under %s" % (s["under_streak"], bench)
        else:
            why = "under %s on " % bench + "/".join(x for x, ok in (("1y", s["beat_1y"]), ("3y", s["beat_3y"])) if not ok)
        L.append(f"    {t:<6} 1y {s['ret_1y']*100:>6.1f}% 3y {s['ret_3y']*100:>6.1f}%  {why}")
    if excluded:
        L.append("EXCLUDED: " + ", ".join(f"{t} ({r})" for t, r in excluded))
    L.append("")
    L.append("DIP-BUY vs BLIND-DCA (same dollars, every run since logging began)")
    if cf.empty:
        L.append("    no purchases logged yet")
    else:
        L.append(f"    {'ticker':<6} {'$ spent':>8} {'dip basis':>10} {'dca basis':>10} {'adv':>7}")
        for _, r in cf.sort_values("advantage_pct", ascending=False).iterrows():
            L.append(f"    {r['ticker']:<6} {r['dollars']:>8.2f} {r['dip_cost_basis']:>10.2f} {r['dca_cost_basis']:>10.2f} {r['advantage_pct']:>+6.2f}%")
        tot = cf["dollars"].sum()
        w = float((cf["advantage_pct"] * cf["dollars"]).sum() / tot) if tot else 0.0
        L.append(f"    weighted advantage {w:+.2f}%  ($ {tot:.2f} total)")
    L.append("")
    L.append("STRATEGY: " + bt_msg)
    report = "\n".join(L)
    with open(os.path.join(W, "report.txt"), "w") as f:
        f.write(report)
    print(report)

if __name__ == "__main__":
    main()
