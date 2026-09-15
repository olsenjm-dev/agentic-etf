#!/usr/bin/env python3
"""
Quarterly parameter backtest for the Top-5 Growth ETF Screen. Analysis only - never trades.

  python3 backtest.py plan --workdir work [--today YYYY-MM-DD]
      Needs work/universe.csv. Prints the idempotency title, a 10-year daily fetch window and
      batches of exactly 10 symbols (3 calls each: adjustment_type split, all, none).

  python3 backtest.py run --workdir work [--today YYYY-MM-DD]
      Needs work/daily.csv.gz, work/universe.csv, work/strategy.json; optional performance.json,
      proposals.json. Reconstructs the monthly screen for every month-end the price history allows,
      tests <=10 one-group parameter variants, and auto-applies one only if it beats the current
      parameters by >= auto_apply_min_pp annualized with no worse max drawdown.
"""
import argparse
import copy
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E  # noqa: E402
from common import (BENCH, SCHEMA, add_months, batches, load_json, load_universe,  # noqa: E402
                    month_label, parse_date, rfc3339, rnd, run_month_first, save_json, to_bool,
                    utc_today, write_writes)

REPORT_LIMIT = 4900
WEIGHT_SETS = [(0.50, 0.30, 0.20), (0.10, 0.30, 0.60), (0.34, 0.33, 0.33)]


def today_arg(s):
    return parse_date(s) if s else utc_today()


def candidate_rows(rows):
    """Current universe rows that are active or on-deck (deactivated rows are left out)."""
    return [r for r in rows if to_bool(r.get("active")) or (r.get("eligible_from") and
                                                             not r.get("notes", "").startswith("DEACTIVATED"))]


def cmd_plan(a):
    today = today_arg(a.today)
    first = run_month_first(today)
    title = f"backtest_{month_label(today)}.json"
    spath = os.path.join(a.workdir, "strategy.json")
    if not os.path.exists(spath):
        print(f"SKIP: no strategy.json in Drive yet (the monthly screen has not run). Nothing tested. ({title} not written)")
        sys.exit(3)
    strat = load_json(spath)
    perf = load_json(os.path.join(a.workdir, "performance.json"), required=False, default={"lists": []})
    need = int(strat.get("backtest", {}).get("min_live_months", 3))
    have = len(perf.get("lists", []))
    if have < need:
        print(f"SKIP: only {have} live monthly screen(s) recorded; the backtest starts after {need}. Nothing tested.")
        sys.exit(3)
    rows = candidate_rows(load_universe(os.path.join(a.workdir, "universe.csv")))
    yrs = int(strat.get("backtest", {}).get("lookback_years", 10))
    start = pd.Timestamp(add_months(first, -12 * yrs)) - pd.Timedelta(days=10)
    plan = {"schema_version": SCHEMA, "run_month": month_label(today),
            "runlog_title": title,
            "start_time": rfc3339(start.date()), "end_time": rfc3339(first),
            "batches": batches([BENCH] + [r["ticker"] for r in rows])}
    save_json(os.path.join(a.workdir, "plan.json"), plan)
    print(f"idempotency check: Drive title '{plan['runlog_title']}'")
    print(f"historicals: interval=day start_time={plan['start_time']} end_time={plan['end_time']}")
    print(f"{len(plan['batches'])} batches x 3 calls (adjustment_type split, all, none):")
    for b in plan["batches"]:
        print("  " + json.dumps(b))


def variants(P):
    out = []
    base_w = (float(P["w_6m"]), float(P["w_1y"]), float(P["w_3y"]))
    n = 0
    for w in WEIGHT_SETS:
        if n == 2:
            break
        if all(abs(x - y) < 1e-9 for x, y in zip(w, base_w)):
            continue
        out.append(("weights", {"w_6m": w[0], "w_1y": w[1], "w_3y": w[2]}))
        n += 1
    ceil = float(P["resid_corr_ceiling"])
    for d in (-0.10, 0.10):
        v = round(min(max(float(P["resid_corr_max"]) + d, 0.30), ceil), 2)
        if abs(v - float(P["resid_corr_max"])) > 1e-9:
            out.append(("resid_corr_max", {"resid_corr_max": v}))
    for key in ("er_penalty", "incumbent_bonus"):
        b = float(P[key])
        for v in (0.0, round(b * 3, 2) if b > 0 else 1.0):
            if abs(v - b) > 1e-9:
                out.append((key, {key: v}))
    out.append(("scoring", {"scoring": "ir" if P.get("scoring", "raw") == "raw" else "raw"}))
    return out[:10]


def precompute(panel, rows, P, months):
    meta = E.universe_meta(rows)
    cache = {}
    for M in months:
        elig, _ = E.eligible_at(rows, M, P, panel, active_only=False)
        try:
            df, bench, gaps, rets = E.compute_metrics(panel, M, elig, P, meta)
        except SystemExit:
            continue
        surv, fails = E.stage_b(df, P)
        C = E.residual_corr(rets, surv) if len(surv) > 1 else pd.DataFrame(1.0, index=surv, columns=surv)
        cache[M] = {"df": df, "surv": surv, "fails": fails, "gaps": gaps, "C": C,
                    "cat": {t: meta[t].get("category", "") for t in df.index}}
    return cache


def simulate(panel, cache, months, P):
    prior = {}
    rets, cmp_rets, voo = [], [], []
    for i, M in enumerate(months[:-1]):
        c = cache.get(M)
        N = months[i + 1]
        if c is None:
            continue
        df = c["df"]
        sc = E.score(df, P, set(prior))
        sel, _, _, _ = E.select(c["surv"], sc, c["C"], c["cat"], df["mdd"].to_dict(), P)
        raw = E.score(df, P, set(), use_bonus=False)
        cmpl = E.rank_order(raw.loc[c["surv"]])[:int(P["top_n"])] if c["surv"] else []
        on_watch, _, _ = E.apply_exits(prior, sel, c["surv"], c["fails"], c["gaps"], {}, P)
        held = sel + sorted(on_watch)
        prior = {**{t: 0 for t in sel}, **on_watch}

        def fwd(ts):
            vals = []
            for t in ts:
                a, b = panel.value(t, M), panel.value(t, N)
                if a and b:
                    vals.append(b / a - 1)
            return sum(vals) / len(vals) if vals else 0.0   # empty list = cash (0%)
        rets.append(fwd(held))
        cmp_rets.append(fwd(cmpl))
        voo.append(fwd([BENCH]))
    return rets, cmp_rets, voo


def stats(r):
    if not r:
        return None, None
    eq, peak, mdd = 1.0, 1.0, 0.0
    for x in r:
        eq *= 1 + x
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    ann = eq ** (12 / len(r)) - 1
    return ann * 100, mdd * 100


def cmd_run(a):
    wd = a.workdir
    out = os.path.join(wd, "out")
    os.makedirs(out, exist_ok=True)
    today = today_arg(a.today)
    first = run_month_first(today)
    run_month = month_label(today)
    spath = os.path.join(wd, "strategy.json")
    strat = load_json(spath)
    P = strat["params"]
    B = strat.get("backtest", {})
    rows = candidate_rows(load_universe(os.path.join(wd, "universe.csv")))
    perf = load_json(os.path.join(wd, "performance.json"), required=False, default={"lists": []})
    props_raw = load_json(os.path.join(wd, "proposals.json"), required=False, default=None)
    props = copy.deepcopy(props_raw) if props_raw else {"proposals": []}
    live_months = len(perf.get("lists", []))
    daily = pd.read_csv(os.path.join(wd, "daily.csv.gz"))
    panel = E.Panel(daily, rows)
    A = panel.on_or_before(pd.Timestamp(first) - pd.Timedelta(days=1))
    first_ok = pd.Timestamp(add_months(panel.dates[0].date(), 37))
    months = [m for m in panel.month_ends() if first_ok <= m <= A and panel.dates.get_loc(m) >= int(P["corr_window_days"])]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    L = [f"TOP-5 PARAMETER BACKTEST  run {today}  (strategy v{strat.get('version')})",
         "Analysis only - no orders.",
         "CAUTION: survivorship-biased. The universe was assembled in 2026 from funds that survived to 2026, so",
         "reconstructed results flatter every variant; the monthly list itself is unaffected."]
    if live_months < int(B.get("noise_warning_live_months", 24)):
        L.append(f"Only {live_months} live month(s) recorded (<{B.get('noise_warning_live_months', 24)}): treat differences as more likely noise than signal.")
    writes = []
    record = {"run_month": run_month, "run_utc": now, "strategy_version_before": strat.get("version"),
              "live_months": live_months, "tr_basis_fallback": sorted(panel.fallback)}
    applied = None
    if len(months) - 1 < int(B.get("min_months", 12)):
        L.append(f"SKIPPED: only {max(len(months) - 1, 0)} reconstructable month(s); need {B.get('min_months', 12)}.")
        record["skipped"] = True
        results = []
    else:
        cache = precompute(panel, rows, P, months)
        base_r, cmp_r, voo_r = simulate(panel, cache, months, P)
        base = stats(base_r)
        results = []
        for group, change in variants(P):
            P2 = {**P, **change}
            r, _, _ = simulate(panel, cache, months, P2)
            ann, mdd = stats(r)
            results.append({"group": group, "change": change, "ann": ann, "mdd": mdd,
                            "delta": ann - base[0]})
        gate = float(B.get("auto_apply_min_pp", 2.0))
        need_mdd = bool(B.get("require_mdd_not_worse", True))
        qual = [x for x in results if x["delta"] >= gate and (not need_mdd or x["mdd"] >= base[1] - 1e-9)]
        period = f"{months[0].date()}..{months[-1].date()} ({len(base_r)} monthly steps)"
        L.append(f"Reconstruction: {period}; {len(rows)} universe funds (eligibility by inception at each date).")
        L.append("")
        L.append(f"{'VARIANT':<44} {'ANN%':>7} {'dANN':>6} {'MDD%':>7}  RESULT")
        L.append(f"{'current parameters':<44} {base[0]:7.2f} {'':>6} {base[1]:7.2f}")
        va, vm = stats(voo_r)
        ca, cm = stats(cmp_r)
        L.append(f"{'(VOO buy-and-hold, reference)':<44} {va:7.2f} {'':>6} {vm:7.2f}")
        L.append(f"{'(unconstrained top-5, reference)':<44} {ca:7.2f} {'':>6} {cm:7.2f}")
        if qual:
            applied = max(qual, key=lambda x: (x["delta"], x["mdd"]))
        for x in results:
            desc = x["group"] + ": " + ", ".join(f"{k}={v}" for k, v in x["change"].items())
            tag = ("AUTO-APPLIED" if x is applied else
                   "qualifies (another applied)" if x in qual else
                   "proposal" if x["delta"] >= float(B.get("min_proposal_pp", 0.5)) else
                   "gain below proposal floor" if x["delta"] > 0 else "no gain")
            L.append(f"{desc[:44]:<44} {x['ann']:7.2f} {x['delta']:+6.2f} {x['mdd']:7.2f}  {tag}")
        L.append(f"Gate: >= +{gate:.1f}pp annualized{' and MDD not worse' if need_mdd else ''}; "
                 f"proposals need >= +{float(B.get('min_proposal_pp', 0.5)):.1f}pp.")
        # proposals: every non-applied variant with a gain
        ev_base = {"base_ann": rnd(base[0]), "base_mdd": rnd(base[1]), "period": period}
        for x in results:
            if x is applied or x["delta"] < float(B.get("min_proposal_pp", 0.5)):
                continue
            summ = f"{x['group']} -> " + ", ".join(f"{k}={v}" for k, v in x["change"].items())
            ev = {**ev_base, "ann": rnd(x["ann"]), "mdd": rnd(x["mdd"]), "delta_pp": rnd(x["delta"]),
                  "run_month": run_month, "strategy_version": strat.get("version")}
            existing = next((p for p in props["proposals"] if p.get("status") == "pending"
                             and p.get("change") == x["change"]), None)
            if existing:
                existing["evidence"] = ev
                existing["updated"] = run_month
            else:
                props["proposals"].append({"id": f"bt-{run_month}-{len(props['proposals']) + 1}",
                                           "created": run_month, "source": "quarterly backtest",
                                           "summary": summ, "change": x["change"], "evidence": ev,
                                           "status": "pending"})
        if applied:
            old = {k: P.get(k) for k in applied["change"]}
            strat["params"] = {**P, **applied["change"]}
            strat["version"] = int(strat.get("version", 1)) + 1
            strat["as_of"] = today.isoformat()
            strat.setdefault("changelog", []).append({
                "version": strat["version"], "date": today.isoformat(),
                "change": {k: [old[k], v] for k, v in applied["change"].items()},
                "evidence": {**ev_base, "new_ann": rnd(applied["ann"]), "new_mdd": rnd(applied["mdd"]),
                             "delta_pp": rnd(applied["delta"]), "survivorship_biased": True}})
            for p in props["proposals"]:
                if p.get("status") == "pending" and p.get("change") == applied["change"]:
                    p["status"] = "applied"
                    p["note"] = f"auto-applied as v{strat['version']} on {today}"
                elif p.get("status") == "pending":
                    p["status"] = "superseded"
                    p["note"] = f"parameters changed to v{strat['version']} on {today}"
            save_json(os.path.join(out, "strategy.json"), strat)
            writes.append({"title": "strategy.json", "path": "out/strategy.json", "mime": "text/plain", "replace": True})
            L.append(f"APPLIED: {applied['group']} " + ", ".join(f"{k} {old[k]} -> {v}" for k, v in applied["change"].items())
                     + f"; strategy.json now v{strat['version']}.")
        else:
            L.append("Applied: nothing.")
        record.update({"period": period, "base": ev_base, "results": [
            {**x, "ann": rnd(x["ann"]), "mdd": rnd(x["mdd"]), "delta": rnd(x["delta"])} for x in results],
            "applied": ({"change": applied["change"], "delta": rnd(applied["delta"])} if applied else None)})
    pending = [p for p in props["proposals"] if p.get("status") == "pending"]
    L.append(f"Waiting in proposals.json: {len(pending)}" + ("" if not pending else " - " + "; ".join(p["summary"] for p in pending[:6])))
    if panel.fallback:
        L.append("Dividend data unverified (price + yield fallback) for: " + " ".join(sorted(panel.fallback)))
    if props != props_raw:
        save_json(os.path.join(out, "proposals.json"), props)
        writes.append({"title": "proposals.json", "path": "out/proposals.json", "mime": "text/plain", "replace": True})
    record["strategy_version_after"] = strat.get("version")
    record["files"] = [w["title"] for w in writes] + [f"backtest_{run_month}.json"]
    save_json(os.path.join(out, f"backtest_{run_month}.json"), record)
    writes.append({"title": f"backtest_{run_month}.json", "path": f"out/backtest_{run_month}.json", "mime": "text/plain", "replace": False})
    body = "\n".join(L)
    if len(body) > REPORT_LIMIT:
        body = body[:REPORT_LIMIT - 40] + "\n...(truncated)"
    with open(os.path.join(out, "report.txt"), "w", encoding="utf-8") as fh:
        fh.write("```\n" + body + "\n```\n")
    write_writes(wd, writes)
    print(f"report: {os.path.join(out, 'report.txt')}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "run"):
        p = sub.add_parser(name)
        p.add_argument("--workdir", default="work")
        p.add_argument("--today")
    a = ap.parse_args()
    {"plan": cmd_plan, "run": cmd_run}[a.cmd](a)


if __name__ == "__main__":
    main()
