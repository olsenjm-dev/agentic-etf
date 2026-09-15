#!/usr/bin/env python3
"""
Monthly Top-5 Growth ETF Screen (notification only - never trades).

  python3 screen.py plan --workdir work [--today YYYY-MM-DD]
      Needs work/universe.csv (+ optional work/current.json, work/performance.json).
      Prints the anchor month, the idempotency runlog title, the Robinhood fetch window and the
      batches of exactly 10 symbols. Writes work/plan.json.

  python3 screen.py run --workdir work [--today YYYY-MM-DD] [--untradable T1,T2] [--bootstrap-strategy]
      Needs work/daily.csv.gz (ingest.py), work/universe.csv, work/strategy.json
      (or --bootstrap-strategy to start from strategy_default.json), optional current.json,
      performance.json, proposals.json. Writes everything to work/out/ and lists the Drive writes.

All arithmetic, selection and report text come from this script.
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E  # noqa: E402
from common import (BENCH, HERE, SCHEMA, add_months, batches, die, load_json, load_universe,  # noqa: E402
                    month_label, norm_month, parse_date, prior_month_label, rfc3339, rnd, run_month_first,
                    save_json, to_bool, utc_today, write_universe, write_writes)

HORIZONS = [1, 3, 6, 12]
REPORT_LIMIT = 4900


def today_arg(s):
    return parse_date(s) if s else utc_today()


def held_from_current(cur):
    """{ticker: months_outside} for the list in force BEFORE this anchor month."""
    if not cur:
        return {}
    return {**{t: 0 for t in cur.get("top5", [])}, **{t: int(m) for t, m in cur.get("on_watch", {}).items()}}


def prior_state(cur, anchor_month):
    """If current.json already reflects this anchor month (a retried run), use the state it replaced."""
    if cur and cur.get("anchor_month") == anchor_month:
        return cur.get("previous") or {}
    return cur or {}


# ------------------------------------------------------------------ plan
def cmd_plan(a):
    wd = a.workdir
    today = today_arg(a.today)
    rows = load_universe(os.path.join(wd, "universe.csv"))
    cur = load_json(os.path.join(wd, "current.json"), required=False)
    perf = load_json(os.path.join(wd, "performance.json"), required=False, default={"lists": []})
    anchor_month = prior_month_label(today)
    first = run_month_first(today)
    tick = []
    for r in rows:
        if to_bool(r.get("active")):
            tick.append(r["ticker"])
        else:
            ef = norm_month(r.get("eligible_from", ""))
            if ef and ef <= anchor_month:
                tick.append(r["ticker"])
    cur = prior_state(cur, anchor_month)
    tick += list(held_from_current(cur))
    for e in perf.get("lists", [])[-13:]:
        tick += e.get("held", []) + e.get("comparator", [])
    tick = [BENCH] + [t for t in tick if t != BENCH]
    start = add_months(first, -60)
    start = start.replace(day=1)
    start = pd.Timestamp(start) - pd.Timedelta(days=10)
    plan = {
        "schema_version": SCHEMA,
        "anchor_month": anchor_month,
        "runlog_title": f"runlog_{anchor_month}.json",
        "start_time": rfc3339(start.date()),
        "end_time": rfc3339(first),
        "batches": batches(tick),
    }
    save_json(os.path.join(wd, "plan.json"), plan)
    print(f"anchor month: {anchor_month}   idempotency check: Drive title '{plan['runlog_title']}'")
    print(f"historicals: interval=day start_time={plan['start_time']} end_time={plan['end_time']}")
    print(f"{len(plan['batches'])} batches x 3 calls (adjustment_type split, all, none):")
    for b in plan["batches"]:
        print("  " + json.dumps(b))


# ------------------------------------------------------------------ run
def fmt(x, w, d=1, sign=True):
    if x is None or x != x:
        return "n/a".rjust(w)
    return (f"{x:+.{d}f}" if sign else f"{x:.{d}f}").rjust(w)


def fwd_return(panel, tickers, d0, d1):
    vals = []
    missing = []
    for t in tickers:
        v0, v1 = panel.value(t, d0), panel.value(t, d1)
        if v0 is None or v1 is None:
            missing.append(t)
        else:
            vals.append(v1 / v0 - 1)
    if not vals:
        return None, missing
    return sum(vals) / len(vals) * 100, missing


def month_end(panel, month):
    """Last trading day in YYYY-MM (None if the month is not complete in the data)."""
    y, m = int(month[:4]), int(month[5:7])
    nxt = add_months(datetime(y, m, 1).date(), 1)
    d = panel.on_or_before(pd.Timestamp(nxt) - pd.Timedelta(days=1))
    if d is None or month_label(d.date()) != month:
        return None
    return d


def update_performance(panel, perf, entry, A):
    lists = [e for e in perf.get("lists", []) if e["month"] != entry["month"]]
    lists.append(entry)
    lists.sort(key=lambda e: e["month"])
    gaps = []
    Am = month_label(A.date())
    for e in lists:
        fwd = e.setdefault("fwd", {})
        d0 = pd.Timestamp(e["anchor"])
        for h in HORIZONS:
            if str(h) in fwd:
                continue
            tm = month_label(add_months(datetime.strptime(e["month"] + "-01", "%Y-%m-%d").date(), h))
            if tm > Am:
                continue
            d1 = month_end(panel, tm)
            if d1 is None or d0 not in panel.dates:
                gaps.append(f"fwd {h}M for {e['month']} list: no price calendar")
                continue
            lr, m1 = fwd_return(panel, e["held"], d0, d1)
            cr, m2 = fwd_return(panel, e["comparator"], d0, d1)
            vr, _ = fwd_return(panel, [BENCH], d0, d1)
            if m1 or m2:
                gaps.append(f"fwd {h}M for {e['month']}: no data for {' '.join(sorted(set(m1 + m2)))}")
            fwd[str(h)] = [rnd(lr), rnd(vr), rnd(cr)]
    perf["lists"] = lists
    parts = []
    for h in HORIZONS:
        src = month_label(add_months(A.date().replace(day=1), -h))
        e = next((x for x in lists if x["month"] == src), None)
        v = (e or {}).get("fwd", {}).get(str(h))
        if v:
            parts.append(f"{h}M " + "|".join("n/a" if x is None else f"{x:+.1f}" for x in v))
        else:
            parts.append(f"{h}M n/a")
    return perf, "  ".join(parts), gaps


def cmd_run(a):
    wd = a.workdir
    out = os.path.join(wd, "out")
    os.makedirs(out, exist_ok=True)
    today = today_arg(a.today)
    anchor_month = prior_month_label(today)
    first = run_month_first(today)
    writes = []

    # ---- inputs
    spath = os.path.join(wd, "strategy.json")
    bootstrapped = False
    if not os.path.exists(spath):
        if not a.bootstrap_strategy:
            die("work/strategy.json missing - pass --bootstrap-strategy only if Drive has no strategy.json")
        shutil.copy(os.path.join(HERE, "strategy_default.json"), spath)
        bootstrapped = True
    strat = load_json(spath)
    P = strat["params"]
    rows = load_universe(os.path.join(wd, "universe.csv"))
    cur_raw = load_json(os.path.join(wd, "current.json"), required=False)
    cur = prior_state(cur_raw, anchor_month)
    perf = load_json(os.path.join(wd, "performance.json"), required=False, default=None)
    props = load_json(os.path.join(wd, "proposals.json"), required=False, default=None)
    daily = pd.read_csv(os.path.join(wd, "daily.csv.gz"))
    panel = E.Panel(daily, rows)
    A = panel.on_or_before(pd.Timestamp(first) - pd.Timedelta(days=1))
    if A is None or month_label(A.date()) != anchor_month:
        die(f"price data does not reach {anchor_month} (last {BENCH} bar {panel.dates[-1].date()})")
    tr_basis = ("dividend-adjusted (split/dividend series from Robinhood)" if not panel.fallback else
                "dividend-adjusted, EXCEPT price + distribution_yield fallback for: " + " ".join(sorted(panel.fallback)))

    # ---- universe maintenance: on-deck flips, hard-eligibility deactivations
    untradable = {t.strip().upper() for t in (a.untradable or "").split(",") if t.strip()}
    flips, deact = [], {}
    changed = False
    for r in rows:
        t = r["ticker"]
        if not to_bool(r.get("active")):
            ef = norm_month(r.get("eligible_from"))
            if ef and ef <= anchor_month and not (r.get("notes", "").startswith("DEACTIVATED")):
                r["active"] = "TRUE"
                flips.append(t)
                changed = True
            else:
                continue
        reason = None
        if t in untradable:
            reason = "not tradable on Robinhood"
        elif t not in panel.tr.columns or panel.last_valid.get(t) is None:
            reason = "no price data (delisted?)"
        elif (A - panel.last_valid[t]).days > 7:
            reason = f"no price since {panel.last_valid[t].date()} (delisted?)"
        else:
            mdv = panel.median_dollar_volume(t, A)
            if mdv is not None:
                r["avg_dollar_volume_30d"] = str(int(round(mdv)))   # saved only if the sheet is rewritten anyway
            if mdv is None or mdv < float(P["volume_floor_usd"]):
                reason = f"30-day median dollar volume ${(mdv or 0) / 1e6:.2f}M < ${float(P['volume_floor_usd']) / 1e6:.2f}M"
        if reason:
            r["active"] = "FALSE"
            r["eligible_from"] = ""
            r["notes"] = f"DEACTIVATED {A.date()}: {reason}. " + r.get("notes", "")
            deact[t] = reason
            changed = True
    inactive_prior = {t: "inactive in universe" for t in held_from_current(cur)
                      if t not in deact and not any(r["ticker"] == t and to_bool(r.get("active")) for r in rows)}
    deact_all = {**inactive_prior, **deact}

    # ---- Stage A
    elig, young = E.eligible_at(rows, A, P, panel)
    meta = E.universe_meta(rows)
    df, bench, gaps, rets = E.compute_metrics(panel, A, elig, P, meta)

    # ---- Stage B
    survivors, fails = E.stage_b(df, P)

    # ---- Stage C
    prior = held_from_current(cur)
    sc = E.score(df, P, set(prior))
    sc_raw = E.score(df, P, set(), use_bonus=False)
    df["score"] = sc
    ranked_all = E.rank_order(sc.loc[survivors]) if survivors else []

    # ---- Stage D
    cat = {t: meta[t].get("category", "") for t in df.index}
    mdd = df["mdd"].to_dict()
    C = E.residual_corr(rets, survivors) if len(survivors) > 1 else pd.DataFrame(1.0, index=survivors, columns=survivors)
    selected, steps, cmax, use_cat = E.select(survivors, sc, C, cat, mdd, P)
    comparator = E.rank_order(sc_raw.loc[survivors])[:int(P["top_n"])] if survivors else []

    # ---- Stage E
    on_watch, removed, added = E.apply_exits(prior, selected, survivors, fails, gaps, deact_all, P)
    held = selected + sorted(on_watch)

    # ---- near misses (ranks 6-8 by score among survivors not selected)
    nm = [t for t in ranked_all if t not in selected][:3]
    near = [(ranked_all.index(t) + 1, t, float(sc[t]),
             E.why_not(t, selected, sc, C, cat, cmax, use_cat, float(P["tie_band"]))) for t in nm]

    # ---- performance
    entry = {"month": anchor_month, "anchor": A.date().isoformat(), "held": held, "comparator": comparator,
             "strategy_version": strat.get("version")}
    perf_obj = perf or {"lists": []}
    perf_obj, fwd_line, perf_gaps = update_performance(panel, perf_obj, entry, A)

    # ---- proposals
    pending = [p for p in (props or {}).get("proposals", []) if p.get("status") == "pending"]

    # ---- outputs
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    top5_rows = []
    for i, t in enumerate(selected, 1):
        r = df.loc[t]
        top5_rows.append({"rank": i, "ticker": t, "name": meta[t].get("name", ""), "category": cat[t],
                          "region": r["region"], "score": rnd(sc[t]), "ex6a": rnd(r["ex6a"]), "ex1": rnd(r["ex1"]),
                          "ex3a": rnd(r["ex3a"]), "ir": rnd(r["ir"]), "mdd": rnd(r["mdd"]),
                          "expense_ratio": rnd(r["er"], 3),
                          "fractional_eligible": to_bool(meta[t].get("fractional_eligible")),
                          "status": "held" if t in prior else "new"})
    current = {
        "anchor_month": anchor_month, "anchor_date": A.date().isoformat(), "generated": now,
        "strategy_version": strat.get("version"), "tr_basis": tr_basis,
        "top5": selected, "on_watch": on_watch, "held": held, "comparator": comparator,
        "detail": top5_rows,
        "previous": {k: v for k, v in (cur or {}).items() if k in ("anchor_month", "top5", "on_watch")},
    }
    save_json(os.path.join(out, "current.json"), current)
    save_json(os.path.join(out, "performance.json"), perf_obj)

    cols = ["status", "score", "ex6a", "ex1", "ex3a", "ex6c", "ex3c", "ir", "te", "mdd", "mdd_years", "er",
            "category", "region"]
    hist_rows = []
    for t in df.index:
        st = ("selected" if t in selected else "on-watch" if t in on_watch else "survivor" if t in survivors
              else "failed-B")
        r = df.loc[t]
        hist_rows.append([t, st, rnd(sc[t]), rnd(r["ex6a"]), rnd(r["ex1"]), rnd(r["ex3a"]), rnd(r["ex6c"]),
                          rnd(r["ex3c"]), rnd(r["ir"]), rnd(r["te"]), rnd(r["mdd"]), rnd(r["mdd_years"], 1),
                          rnd(r["er"], 3), r["category"], r["region"]])
    hist_rows.sort(key=lambda x: (-(x[2] if x[1] != "failed-B" else -1e9), x[0]))
    history = {
        "anchor_month": anchor_month, "anchor_date": A.date().isoformat(), "strategy_version": strat.get("version"),
        "params": P, "tr_basis": tr_basis,
        "bench": {k: rnd(v) for k, v in bench.items()},
        "columns": ["ticker"] + cols,
        "rows": hist_rows,
        "rank_order": ranked_all,
        "selected": selected, "comparator": comparator, "on_watch": on_watch,
        "relaxation": steps, "stage_b_failures": fails, "not_eligible_young": young, "data_gaps": gaps,
        "resid_corr_selected": {t: {s: rnd(C.at[t, s]) for s in selected if s != t} for t in selected},
    }
    save_json(os.path.join(out, f"history_{anchor_month}.json"), history)

    cache = {"anchor_month": anchor_month, "anchor_date": A.date().isoformat(),
             "columns": ["ticker", "close", "div_ttm", "tr_6m", "tr_1y", "tr_3y", "verified"], "rows": []}
    W = E.windows(panel, A)
    iA = panel.dates.get_loc(A)
    for t in sorted(set(df.index) | {BENCH}):
        v = {k: panel.value(t, W[k]) for k in ("A", "s6", "s1", "s3")}
        px = panel.close[t].iloc[iA] if t in panel.close.columns else None
        dsum = None
        if t in panel.close.columns:
            dd = daily[(daily.ticker == t)]
            dd = dd[(pd.to_datetime(dd.date) > W["s1"]) & (pd.to_datetime(dd.date) <= A)]
            dsum = float(dd["div"].sum())
        tr = [rnd((v["A"] / v[k] - 1) * 100) if v["A"] and v[k] else None for k in ("s6", "s1", "s3")]
        cache["rows"].append([t, rnd(px, 4), rnd(dsum, 4)] + tr + [bool(panel.verified.get(t, False))])
    save_json(os.path.join(out, f"cache_prices_{anchor_month}.json"), cache)

    if changed:
        write_universe(os.path.join(out, "universe.csv"), rows)

    # ---- report
    L = []
    L.append(f"TOP-5 GROWTH ETF SCREEN  anchor {A.date()}  (run {today}, strategy v{strat.get('version')})")
    L.append("Notification only - no orders. Total return: " + tr_basis)
    n_active = sum(1 for r in rows if to_bool(r.get("active")))
    L.append(f"Universe {n_active} active | eligible {len(df)} | passed filter {len(survivors)} | "
             f"list {len(selected)} + {len(on_watch)} on-watch")
    L.append(f"VOO: 6M {bench['r6']:+.1f}%  1Y {bench['r1']:+.1f}%  3Y {bench['r3']:+.1f}% "
             f"({bench['cagr3']:+.1f}%/yr)")
    L.append("")
    L.append(" # TICKER CATEGORY         REG     6Mx    1Yx    3Yx   SCORE    IR  MDD(yrs)   ER  STATUS")
    reg_abbr = {"US": "US", "developed-ex-US": "DXU", "emerging": "EM", "global": "GL"}

    def line(rank, t, status):
        r = df.loc[t]
        return (f"{rank:>2} {t:<6} {cat[t][:16]:<16} {reg_abbr.get(r['region'], r['region'][:3]):<4}"
                f"{fmt(r['ex6a'], 7)}{fmt(r['ex1'], 7)}{fmt(r['ex3a'], 7)}{fmt(sc[t], 8, 2, False)}"
                f"{fmt(r['ir'], 6, 2, False)} {fmt(r['mdd'], 6)}({r['mdd_years']:.0f}) {r['er']:5.2f}  {status}")
    for i, t in enumerate(selected, 1):
        L.append(line(i, t, "held" if t in prior else "NEW"))
    for t in sorted(on_watch):
        if t in df.index:
            L.append(line(ranked_all.index(t) + 1 if t in ranked_all else 0, t, f"on-watch {on_watch[t]}/{P['exit_months_outside']}"))
        else:
            L.append(f"   {t:<6} (not evaluated this month)                              on-watch {on_watch[t]}/{P['exit_months_outside']}")
    if not selected:
        L.append("   (no fund beat VOO on all three windows this month)")
    L.append("x = excess vs VOO in pp (6M annualized, 1Y, 3Y CAGR). MDD % over years shown. # = rank by score.")
    L.append("")
    L.append("Additions: " + (", ".join(f"{t} (entered the top five)" for t in added) or "none."))
    L.append("Removals: " + ("; ".join(f"{t} - {why}" for t, why in removed) or "none."))
    L.append("On-watch: " + (", ".join(f"{t} ({m} month outside the top five; removed at {P['exit_months_outside']})"
                                       for t, m in sorted(on_watch.items())) or "none."))
    L.append("Near misses: " + ("; ".join(f"#{k} {t} score {s:.2f} - {why}" for k, t, s, why in near) or "none."))
    L.append("Relaxation: " + ("; ".join(steps) if steps else
                               f"none (resid corr <= {float(P['resid_corr_max']):.2f}, one fund per category)."))
    if len(selected) < int(P["top_n"]):
        L.append(f"Short list: only {len(selected)} fund(s) qualified this month.")
    if flips:
        L.append("On-deck funds now eligible: " + ", ".join(flips) + ".")
    if deact:
        L.append("Deactivated: " + "; ".join(f"{t} - {w}" for t, w in deact.items()))
    gap_items = [f"{t}: {w}" for t, w in sorted(gaps.items())] + perf_gaps
    L.append("Data gaps: " + ("; ".join(gap_items) if gap_items else "none."))
    L.append(f"Proposals pending review: {len(pending)}" + (" - " + "; ".join(p.get("summary", p.get("id", "")) for p in pending[:3]) if pending else "."))
    L.append(f"Unconstrained top five: {' '.join(comparator) or 'none'}")
    L.append("Fwd return, list|VOO|unconstrained (%): " + fwd_line)
    if bootstrapped:
        L.append("Setup: strategy.json created from the repo defaults (v1).")
    body = "\n".join(L)
    if len(body) + 8 > REPORT_LIMIT:
        body = body[:REPORT_LIMIT - 40] + "\n...(truncated; see history file)"
    report = "```\n" + body + "\n```\n"
    with open(os.path.join(out, "report.txt"), "w", encoding="utf-8") as fh:
        fh.write(report)

    # ---- writes (runlog last = idempotency marker)
    writes.append({"title": "current.json", "path": "out/current.json", "mime": "text/plain", "replace": True})
    writes.append({"title": "performance.json", "path": "out/performance.json", "mime": "text/plain", "replace": True})
    writes.append({"title": f"history_{anchor_month}.json", "path": f"out/history_{anchor_month}.json", "mime": "text/plain", "replace": True})
    writes.append({"title": f"cache_prices_{anchor_month}.json", "path": f"out/cache_prices_{anchor_month}.json", "mime": "text/plain", "replace": True})
    if changed:
        writes.append({"title": "universe", "path": "out/universe.csv", "mime": "text/csv", "replace": True})
    if bootstrapped:
        shutil.copy(spath, os.path.join(out, "strategy.json"))
        writes.append({"title": "strategy.json", "path": "out/strategy.json", "mime": "text/plain", "replace": False})
    if props is None:
        save_json(os.path.join(out, "proposals.json"), {"proposals": []})
        writes.append({"title": "proposals.json", "path": "out/proposals.json", "mime": "text/plain", "replace": False})
    runlog = {
        "anchor_month": anchor_month, "anchor_date": A.date().isoformat(), "run_utc": now,
        "strategy_version": strat.get("version"), "tr_basis": tr_basis,
        "counts": {"universe_active": n_active, "eligible": int(len(df)), "survivors": len(survivors),
                   "selected": len(selected), "on_watch": len(on_watch)},
        "selected": selected, "added": added, "removed": removed, "relaxation": steps,
        "flips": flips, "deactivated": deact, "untradable_input": sorted(untradable),
        "data_gaps": gaps, "perf_gaps": perf_gaps,
        "files": [w["title"] for w in writes] + [f"runlog_{anchor_month}.json"],
        "retry_of_same_month": bool(cur_raw and cur_raw.get("anchor_month") == anchor_month),
    }
    save_json(os.path.join(out, f"runlog_{anchor_month}.json"), runlog)
    writes.append({"title": f"runlog_{anchor_month}.json", "path": f"out/runlog_{anchor_month}.json", "mime": "text/plain", "replace": False})
    write_writes(wd, writes)
    print(f"TRADABILITY CHECK LIST: {json.dumps(held)}")
    print(f"report: {os.path.join(out, 'report.txt')} ({len(report)} chars)")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--workdir", default="work")
    p.add_argument("--today")
    r = sub.add_parser("run")
    r.add_argument("--workdir", default="work")
    r.add_argument("--today")
    r.add_argument("--untradable", default="")
    r.add_argument("--bootstrap-strategy", action="store_true")
    a = ap.parse_args()
    {"plan": cmd_plan, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
