#!/usr/bin/env python3
"""
Ingest oversized Robinhood tool results that the harness saved to disk
(files named mcp-Robinhood-<tool>-<id>.txt under a tool-results directory)
and normalize them into CSVs in the working directory.

Nothing here passes through the model: the scheduled task calls the Robinhood
tool with a range large enough that the result is written to disk, then runs
this script.

Handled tools:
  get_equity_historicals  -> prices_<interval>.csv   (date,ticker,close,high,low,volume)
  get_equity_fundamentals -> fundamentals.csv        (ticker,high_52w,low_52w,avg_volume)
  get_equity_quotes       -> quotes.csv              (ticker,price,updated_at)
  get_equity_positions    -> positions.csv           (ticker,quantity,avg_cost)

Usage:
  python3 ingest.py [--workdir work] [--results-dir DIR] [--since-minutes 180]
If --results-dir is omitted, every */tool-results directory under /root/.claude/projects is searched.
Only files modified within --since-minutes are used (so stale files from earlier runs are ignored).
"""
import argparse, glob, json, os, time
import pandas as pd

def find_files(results_dir, since_minutes):
    pats = [os.path.join(results_dir, "mcp-Robinhood-*.txt")] if results_dir else \
           ["/root/.claude/projects/*/*/tool-results/mcp-Robinhood-*.txt",
            "/root/.claude/projects/*/tool-results/mcp-Robinhood-*.txt"]
    files = []
    for p in pats:
        files += glob.glob(p)
    cutoff = time.time() - since_minutes * 60
    return sorted(f for f in files if os.path.getmtime(f) >= cutoff)

def load(f):
    with open(f) as fh:
        txt = fh.read()
    # some harness wrappers prepend a note; find the first '{'
    i = txt.find("{")
    return json.loads(txt[i:])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--results-dir", default=None)
    ap.add_argument("--since-minutes", type=int, default=180)
    a = ap.parse_args()
    os.makedirs(a.workdir, exist_ok=True)

    hist, funda, quotes, pos = {}, [], [], []
    used = []
    for f in find_files(a.results_dir, a.since_minutes):
        name = os.path.basename(f)
        try:
            d = load(f)
        except Exception as e:
            print(f"skip {name}: {e}"); continue
        data = d.get("data", d)
        if "get_equity_historicals" in name:
            for r in data.get("results", []):
                iv = r.get("interval", "unknown")
                rows = hist.setdefault(iv, [])
                for b in r.get("bars", []):
                    if b.get("interpolated"):
                        continue
                    rows.append({"date": b["begins_at"][:10], "ticker": r["symbol"].upper(),
                                 "close": float(b["close_price"]), "high": float(b["high_price"]),
                                 "low": float(b["low_price"]), "volume": b.get("volume")})
            used.append(name)
        elif "get_equity_fundamentals" in name:
            for r in data.get("results", data if isinstance(data, list) else []):
                funda.append({"ticker": r.get("symbol", "").upper(),
                              "high_52w": r.get("high_52_weeks"), "low_52w": r.get("low_52_weeks"),
                              "avg_volume": r.get("average_volume") or r.get("average_volume_30_days")})
            used.append(name)
        elif "get_equity_quotes" in name:
            for r in data.get("results", []):
                q = r.get("quote", r)
                quotes.append({"ticker": q.get("symbol", "").upper(), "price": q.get("last_trade_price"),
                               "updated_at": q.get("venue_last_trade_time")})
            used.append(name)
        elif "get_equity_positions" in name:
            for r in data.get("results", []):
                pos.append({"ticker": (r.get("symbol") or "").upper(), "quantity": r.get("quantity"),
                            "avg_cost": r.get("average_buy_price")})
            used.append(name)

    for iv, rows in hist.items():
        df = pd.DataFrame(rows).drop_duplicates(["date", "ticker"], keep="last").sort_values(["ticker", "date"])
        out = os.path.join(a.workdir, f"prices_{'daily' if iv == 'day' else 'weekly' if iv == 'week' else 'monthly' if iv == 'month' else iv}.csv")
        df.to_csv(out, index=False)
        print(f"wrote {out}: {df.ticker.nunique()} tickers, {len(df)} bars, {df.date.min()}..{df.date.max()}")
    if funda:
        pd.DataFrame(funda).to_csv(os.path.join(a.workdir, "fundamentals.csv"), index=False); print(f"wrote fundamentals.csv ({len(funda)})")
    if quotes:
        pd.DataFrame(quotes).to_csv(os.path.join(a.workdir, "quotes.csv"), index=False); print(f"wrote quotes.csv ({len(quotes)})")
    if pos:
        pd.DataFrame(pos).to_csv(os.path.join(a.workdir, "positions.csv"), index=False); print(f"wrote positions.csv ({len(pos)})")
    print(f"used {len(used)} result files")

if __name__ == "__main__":
    main()
