#!/usr/bin/env python3
"""
Annual universe build helpers for the Top-5 Growth ETF Screen.

The task researches candidates (fresh enumeration + issuer pages) and writes work/candidates.csv:
  ticker,name,mandate,category,region,expense_ratio,distribution_yield,inception,aum,source_url,notes,exclude_reason
    expense_ratio, distribution_yield : percent (0.04 means 0.04%)
    inception                         : YYYY-MM-DD (issuer page)
    aum                               : USD (issuer page), e.g. 1250000000
    exclude_reason                    : blank, or why the fund is structurally excluded
                                        (leveraged, single-stock, covered-call, buffered, commodity, ...)

  python3 universe.py plan  --workdir work
      validates candidates.csv; prints tradability batches (<=10, account 931184287) and
      historicals batches (exactly 10, adjustment_type split only, interval day).
  python3 universe.py build --workdir work [--untradable T1,T2] [--nonfractional T3] [--prev work/prev_universe.csv]
      applies every exclusion rule, computes beta_3y / 30-day median dollar volume from Robinhood
      bars (run `python3 ingest.py --workdir work --expect 1` first), writes work/out/universe.csv,
      work/out/summary.txt and work/out/build_report.json.
  python3 universe.py verify --workdir work --readback work/readback.csv
      read-back check of the Sheet as stored in Drive (drive_text.py table2csv output).
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import Counter

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (BENCH, MANDATES, REGIONS, TAXONOMY, UNIVERSE_COLUMNS,  # noqa: E402
                    add_months, batches, die, month_label, parse_date, rfc3339, save_json, to_bool,
                    to_float, utc_today, write_universe)

CAND_COLS = ["ticker", "name", "mandate", "category", "region", "expense_ratio", "distribution_yield",
             "inception", "aum", "source_url", "notes", "exclude_reason"]
EVALUATIVE = re.compile(
    r"\b(best|top[- ]performing|outperform\w*|underperform\w*|strong(er|est)?|weak(er|est)?|promising|attractive|"
    r"expected|expect\w*|will|should|likely|poised|opportunit\w*|winner\w*|superior|excellent|great|"
    r"undervalued|overvalued|momentum play|bullish|bearish|recommend\w*|forecast\w*|predict\w*)\b", re.I)
MIN_AGE, ONDECK_AGE = 39, 24
AUM_MIN, DV_MIN = 250e6, 5e6


def load_candidates(path):
    if not os.path.exists(path):
        die(f"{path} not found")
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        die("candidates.csv is empty")
    miss = [c for c in CAND_COLS if c not in rows[0]]
    if miss:
        die(f"candidates.csv missing columns {miss}")
    out, errs, seen = [], [], set()
    for i, r in enumerate(rows, 2):
        r = {k: (v or "").strip() for k, v in r.items() if k}
        t = r["ticker"].upper()
        r["ticker"] = t
        if not t:
            continue
        if t in seen:
            errs.append(f"line {i}: duplicate ticker {t}")
        seen.add(t)
        if t == BENCH:
            errs.append(f"line {i}: {BENCH} is the benchmark - leave it out")
        if not r["exclude_reason"]:
            if r["category"] not in TAXONOMY:
                errs.append(f"line {i} {t}: category {r['category']!r} not in taxonomy {TAXONOMY}")
            if r["region"] not in REGIONS:
                errs.append(f"line {i} {t}: region {r['region']!r} not in {REGIONS}")
            if not r["mandate"]:
                errs.append(f"line {i} {t}: mandate is blank")
            for c in ("expense_ratio", "distribution_yield"):
                v = to_float(r[c])
                if v is None or not (0 <= v <= (3 if c == "expense_ratio" else 25)):
                    errs.append(f"line {i} {t}: {c} {r[c]!r} missing or out of range (percent units)")
            if parse_date(r["inception"]) is None:
                errs.append(f"line {i} {t}: inception {r['inception']!r} not YYYY-MM-DD")
            if to_float(r["aum"]) is None:
                errs.append(f"line {i} {t}: aum {r['aum']!r} not a number (USD)")
            if not r["source_url"].startswith("http"):
                errs.append(f"line {i} {t}: source_url missing")
            if "|" in r["notes"] or "|" in r["name"]:
                errs.append(f"line {i} {t}: '|' not allowed in name/notes")
            m = EVALUATIVE.search(r["notes"])
            if m:
                errs.append(f"line {i} {t}: notes must be factual - remove {m.group(0)!r}")
        out.append(r)
    return out, errs


def cmd_plan(a):
    rows, errs = load_candidates(os.path.join(a.workdir, "candidates.csv"))
    if errs:
        print("FIX candidates.csv:\n  " + "\n  ".join(errs))
        sys.exit(1)
    live = [r["ticker"] for r in rows if not r["exclude_reason"]]
    today = utc_today()
    start = add_months(today.replace(day=1), -38)
    print(f"{len(rows)} candidates, {len(live)} not structurally excluded")
    print("tradability batches (get_equity_tradability, account_number 931184287):")
    for i in range(0, len(live), 10):
        print("  " + json.dumps(live[i:i + 10]))
    print(f"historicals batches (get_equity_historicals interval=day adjustment_type=split "
          f"start_time={rfc3339(start)} end_time={rfc3339(today)}):")
    for b in batches([BENCH] + live):
        print("  " + json.dumps(b))


def cmd_build(a):
    wd = a.workdir
    out = os.path.join(wd, "out")
    os.makedirs(out, exist_ok=True)
    rows, errs = load_candidates(os.path.join(wd, "candidates.csv"))
    if errs:
        print("FIX candidates.csv:\n  " + "\n  ".join(errs))
        sys.exit(1)
    today = utc_today()
    untradable = {t.strip().upper() for t in a.untradable.split(",") if t.strip()}
    nonfrac = {t.strip().upper() for t in a.nonfractional.split(",") if t.strip()}
    daily = pd.read_csv(os.path.join(wd, "daily.csv.gz"))
    daily["date"] = pd.to_datetime(daily["date"])
    px = daily.pivot_table(index="date", columns="ticker", values="close", aggfunc="last").sort_index()
    vol = daily.pivot_table(index="date", columns="ticker", values="volume", aggfunc="last").sort_index()
    if BENCH not in px.columns:
        die("no VOO bars - fetch VOO in the historicals batches")
    vdates = px[BENCH].dropna().index
    last = vdates[-1]
    b_start = vdates[vdates.searchsorted(pd.Timestamp(add_months(last.date(), -36)))]
    vret = px[BENCH].loc[b_start:last].pct_change()

    keep, excluded = [], []
    for r in rows:
        t = r["ticker"]
        reason = None
        age = None
        inc = parse_date(r["inception"])
        if r["exclude_reason"]:
            reason = r["exclude_reason"]
        elif r["mandate"] not in MANDATES:
            reason = f"mandate '{r['mandate']}' is not growth/thematic/growth-sector"
        elif t in untradable:
            reason = "failed Robinhood tradability check"
        elif to_float(r["aum"]) < AUM_MIN:
            reason = f"AUM ${to_float(r['aum']) / 1e6:.0f}M < $250M"
        else:
            age = (today.year - inc.year) * 12 + (today.month - inc.month) - (1 if today.day < inc.day else 0)
            if age < ONDECK_AGE:
                reason = f"inception {inc} (<{ONDECK_AGE} months)"
            elif t not in px.columns or px[t].dropna().empty:
                reason = "no Robinhood price data"
            else:
                s = px[t].dropna().tail(30)
                dv = (s * vol[t].reindex(s.index)).median()
                r["avg_dollar_volume_30d"] = str(int(round(dv))) if dv == dv else ""
                if not dv == dv or dv < DV_MIN:
                    reason = f"30-day median dollar volume ${(dv if dv == dv else 0) / 1e6:.2f}M < $5.00M"
        if reason:
            excluded.append((t, reason))
            continue
        fr = px[t].loc[b_start:last].pct_change()
        ok = fr.notna() & vret.notna()
        if ok.sum() >= 250:
            x, y = vret[ok].to_numpy(), fr[ok].to_numpy()
            r["beta_3y"] = f"{np.cov(x, y, ddof=1)[0, 1] / np.var(x, ddof=1):.2f}"
        else:
            r["beta_3y"] = ""
        if age >= MIN_AGE:
            r["active"], r["eligible_from"] = "TRUE", ""
        else:
            r["active"] = "FALSE"
            r["eligible_from"] = month_label(add_months(inc, MIN_AGE))
        r["fractional_eligible"] = "FALSE" if t in nonfrac else "TRUE"
        r["last_verified"] = today.isoformat()
        r["expense_ratio"] = f"{to_float(r['expense_ratio']):.3g}"
        r["distribution_yield"] = f"{to_float(r['distribution_yield']):.3g}"
        r["aum"] = str(int(round(to_float(r["aum"]))))
        keep.append(r)

    keep.sort(key=lambda r: (TAXONOMY.index(r["category"]), r["ticker"]))
    write_universe(os.path.join(out, "universe.csv"), keep)
    active = [r for r in keep if r["active"] == "TRUE"]
    ondeck = [r for r in keep if r["active"] != "TRUE"]
    bad = [r["ticker"] for r in keep if not r.get("mandate") or not r.get("category")]
    cats = Counter(r["category"] for r in active)

    prev_drops = []
    if a.prev and os.path.exists(a.prev):
        with open(a.prev, newline="", encoding="utf-8") as fh:
            prev = [x for x in csv.DictReader(fh) if (x.get("ticker") or "").strip()]
        now_t = {r["ticker"] for r in keep}
        why = dict(excluded)
        for p in prev:
            t = p["ticker"].strip().upper()
            if t not in now_t and (to_bool(p.get("active")) or p.get("eligible_from")):
                prev_drops.append((t, why.get(t, "not found in this year's enumeration")))
    first_build = not (a.prev and os.path.exists(a.prev))

    L = [f"ETF UNIVERSE BUILD {today} (annual)",
         f"Active: {len(active)} tickers | on-deck: {len(ondeck)} | excluded: {len(excluded)}"]
    if not 60 <= len(active) <= 120:
        L.append(f"NOTE: active count {len(active)} is outside the 60-120 target.")
    L.append("By category: " + ", ".join(f"{c} {cats.get(c, 0)}" for c in TAXONOMY))
    if ondeck:
        L.append("On-deck: " + ", ".join(f"{r['ticker']} (eligible {r['eligible_from']})" for r in ondeck))
    if first_build:
        L.append("Dropped since last year: n/a (first build).")
    else:
        L.append("Dropped since last year: " + ("; ".join(f"{t} - {w}" for t, w in prev_drops) or "none."))
    if bad:
        L.append("ERROR rows missing mandate/category: " + " ".join(bad))
    summary = "```\n" + "\n".join(L) + "\n```\n"
    if len(summary) > 4900:
        summary = summary[:4850] + "\n...(truncated)\n```\n"
    with open(os.path.join(out, "summary.txt"), "w", encoding="utf-8") as fh:
        fh.write(summary)
    save_json(os.path.join(out, "build_report.json"), {
        "date": today.isoformat(), "active": len(active), "on_deck": len(ondeck),
        "excluded": excluded, "dropped_since_last_year": prev_drops, "categories": dict(cats),
        "rows": len(keep)})
    print(summary)
    print(f"universe.csv: {len(keep)} rows -> create Drive Sheet title 'universe' "
          f"(contentMimeType text/csv) from {os.path.join(out, 'universe.csv')}")
    if bad:
        sys.exit(1)


def cmd_verify(a):
    with open(a.readback, newline="", encoding="utf-8") as fh:
        rb = list(csv.reader(fh))
    if not rb:
        die("read-back is empty")
    hdr = [h.strip() for h in rb[0]]
    if hdr != UNIVERSE_COLUMNS:
        die(f"read-back header mismatch: {hdr}")
    data = [dict(zip(hdr, r)) for r in rb[1:] if any(c.strip() for c in r)]
    with open(os.path.join(a.workdir, "out", "universe.csv"), newline="", encoding="utf-8") as fh:
        want = list(csv.DictReader(fh))
    bad = [d.get("ticker", "?") for d in data if not d.get("mandate", "").strip() or not d.get("category", "").strip()]
    if len(data) != len(want):
        die(f"read-back has {len(data)} rows, expected {len(want)}")
    if {d["ticker"] for d in data} != {w["ticker"] for w in want}:
        die("read-back tickers differ from the built universe")
    if bad:
        die("rows missing mandate/category: " + " ".join(bad))
    print(f"VERIFIED: {len(data)} rows, every row has mandate and category")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--workdir", default="work")
    b = sub.add_parser("build")
    b.add_argument("--workdir", default="work")
    b.add_argument("--untradable", default="")
    b.add_argument("--nonfractional", default="")
    b.add_argument("--prev", default="")
    v = sub.add_parser("verify")
    v.add_argument("--workdir", default="work")
    v.add_argument("--readback", required=True)
    a = ap.parse_args()
    {"plan": cmd_plan, "build": cmd_build, "verify": cmd_verify}[a.cmd](a)


if __name__ == "__main__":
    main()
