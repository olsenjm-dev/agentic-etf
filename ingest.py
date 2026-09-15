#!/usr/bin/env python3
"""
Ingest Robinhood get_equity_historicals results that the harness saved to disk and build a
dividend-adjusted total-return (TR) series per ticker.

WHY THREE SERIES PER BATCH
Robinhood's adjustment_type="all" daily bars subtract future dividends as a flat dollar offset
(additive back-adjustment), and the offsets for dividends paid before a split are NOT split-adjusted.
Percent returns taken straight from those bars are wrong. So each batch of 10 symbols is fetched
three times - adjustment_type "split", "all" and "none" - and this script derives:
    raw dividend on day t   = offset[t-1] - offset[t],   offset = split_close - all_close
    split factor F[t]       = none_close / split_close
    split-adjusted dividend = raw dividend / F[t]
    TR[t] = TR[t-1] * (split_close[t] + div[t]) / split_close[t-1]
(Verified against published VOO/QQQ/VGT/VUG/SCHG calendar-year total returns.)

The saved files do not record which adjustment they hold, so the script identifies them from the
data: split vs all differ by a per-bar constant (same across O/H/L/C); none vs split differ by a
per-bar ratio. Where two adjustments are identical (no splits or no dividends) it does not matter.

Usage:
  python3 ingest.py --workdir work [--results-dir DIR] [--since-minutes 240] [--expect 3]
  --expect 1  (universe build): only split-adjusted prices are needed; no TR/dividend work.
Outputs:
  work/daily.csv.gz        date,ticker,close,volume,div,tr,verified
  work/ingest_report.json  per-ticker coverage + flags
"""
import argparse
import glob
import hashlib
import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import save_json  # noqa: E402

DEFAULT_GLOBS = [
    "/root/.claude/projects/*/*/tool-results/mcp-Robinhood-get_equity_historicals-*.txt",
    "/root/.claude/projects/*/tool-results/mcp-Robinhood-get_equity_historicals-*.txt",
    "/root/.claude/projects/*/*/tool-results/*get_equity_historicals*.txt",
]


def find_files(results_dir, since_minutes):
    pats = ([os.path.join(results_dir, "*get_equity_historicals*.txt")] if results_dir else DEFAULT_GLOBS)
    files = set()
    for p in pats:
        files.update(glob.glob(p))
    cutoff = time.time() - since_minutes * 60
    return sorted(f for f in files if os.path.getmtime(f) >= cutoff)


def parse_file(path):
    with open(path, encoding="utf-8") as fh:
        txt = fh.read()
    i = txt.find("{")
    d = json.loads(txt[i:])
    data = d.get("data", d)
    out = {}
    interval = None
    for r in data.get("results", []):
        interval = r.get("interval", interval)
        rows = []
        for b in r.get("bars", []):
            if b.get("interpolated"):
                continue
            rows.append((b["begins_at"][:10], float(b["open_price"]), float(b["high_price"]),
                         float(b["low_price"]), float(b["close_price"]), float(b.get("volume") or 0)))
        if rows:
            df = pd.DataFrame(rows, columns=["date", "o", "h", "l", "c", "v"]).drop_duplicates("date", keep="last")
            out[r["symbol"].upper()] = df.set_index("date").sort_index()
    return interval, out


def content_hash(series_map):
    h = hashlib.sha1()
    for s in sorted(series_map):
        df = series_map[s]
        h.update(s.encode())
        h.update(np.round(df[["o", "h", "l", "c"]].to_numpy(), 6).tobytes())
    return h.hexdigest()


def relation(X, Y, min_frac=0.99):
    """Return (add_ok, mul_ok, x_above) comparing two {symbol: df} maps over common symbols/dates.
    A relation holds when, for every symbol, >= min_frac of bars satisfy it (Robinhood occasionally
    publishes a one-bar glitch in the adjusted series)."""
    add_min = mul_min = 1.0
    above = 0
    for s in set(X) & set(Y):
        a, b = X[s].align(Y[s], join="inner")
        A = a[["o", "h", "l", "c"]].to_numpy()
        B = b[["o", "h", "l", "c"]].to_numpy()
        if len(A) == 0:
            continue
        d = A - B
        add_min = min(add_min, float(np.mean(np.nanmax(np.abs(d - d[:, [3]]), axis=1) <= 2e-5)))
        with np.errstate(divide="ignore", invalid="ignore"):
            r = A / B
            rel = np.nanmax(np.abs(r - r[:, [3]]), axis=1) / np.maximum(np.abs(r[:, 3]), 1e-12)
        mul_min = min(mul_min, float(np.mean(rel <= 2e-5)))
        above += float(np.nansum(d[:, 3]))
    return add_min >= min_frac, mul_min >= min_frac, above >= 0


def jumpier(X, Y):
    """Among two ratio-related maps, return True if X shows the larger return jumps where the ratio changes (X = unadjusted)."""
    sx = sy = 0.0
    for s in set(X) & set(Y):
        a, b = X[s]["c"].align(Y[s]["c"], join="inner")
        ratio = (a / b).round(6)
        chg = ratio.diff().abs() > 1e-4
        if chg.any():
            sx += float(np.log(a / a.shift(1)).abs()[chg].sum())
            sy += float(np.log(b / b.shift(1)).abs()[chg].sum())
    return sx >= sy


def classify(datasets, n_files, expect):
    """datasets: list of distinct {symbol: df}. Returns (split, all, none, notes)."""
    notes = []
    k = len(datasets)
    if expect == 1:
        return datasets[-1], None, None, notes
    if k == 1:
        if n_files >= 3:
            return datasets[0], datasets[0], datasets[0], notes          # no splits, no dividends
        notes.append(f"only {n_files} file(s) for this batch; dividends/splits unverified")
        return datasets[0], None, None, notes
    if k == 2:
        X, Y = datasets
        add_ok, mul_ok, x_above = relation(X, Y)
        if add_ok and not mul_ok:
            s, a = (X, Y) if x_above else (Y, X)
            if n_files >= 3:
                return s, a, s, notes                                      # none == split (no splits)
            notes.append("unadjusted series missing; dividends not split-corrected")
            return s, a, None, notes
        if mul_ok and not add_ok:
            n, s = (X, Y) if jumpier(X, Y) else (Y, X)
            if n_files >= 3:
                return s, s, n, notes                                      # all == split (no dividends)
            notes.append("dividend-adjusted series missing; dividends unverified")
            return s, None, n, notes
        notes.append("two series with no recognizable relation; using the later one as split-adjusted, dividends unverified")
        return datasets[-1], None, None, notes
    # k >= 3: try every assignment of three distinct datasets
    import itertools
    best = None
    for combo in itertools.combinations(range(k), 3):
        for n_i, s_i, a_i in itertools.permutations(combo):
            add_ok, _, s_above = relation(datasets[s_i], datasets[a_i])
            _, mul_ok, _ = relation(datasets[n_i], datasets[s_i])
            if add_ok and s_above and mul_ok:
                best = (n_i, s_i, a_i)
                break
        if best:
            break
    if k > 3:
        notes.append(f"{k} distinct result sets for one batch (repeated calls?)")
    if not best:
        notes.append("could not identify split/all/none series; dividends unverified")
        return datasets[-1], None, None, notes
    n_i, s_i, a_i = best
    return datasets[s_i], datasets[a_i], datasets[n_i], notes


def build_symbol(sym, S, A, N):
    """Return DataFrame(date, close, volume, div, tr, verified) and flags."""
    flags = []
    df = S[["c", "v"]].rename(columns={"c": "close", "v": "volume"}).copy()
    df["div"] = 0.0
    verified = A is not None and N is not None
    if A is not None:
        a = A["c"].reindex(df.index)
        off = (df["close"] - a).round(6)
        raw = (off.shift(1) - off).fillna(0.0).to_numpy().copy()
        raw[np.abs(raw) <= 1e-4] = 0.0
        px = df["close"].to_numpy().copy()
        # 1) cancel excursions: a jump followed within 15 bars by an opposite jump of the same size
        #    (Robinhood's dividend-adjusted series contains such multi-bar glitches, mostly pre-2020)
        nz = list(np.flatnonzero(raw))
        cancelled = 0
        for j in nz:
            if raw[j] == 0.0:
                continue
            for i in range(j - 1, max(j - 16, -1), -1):
                if raw[i] != 0.0 and np.sign(raw[i]) != np.sign(raw[j]) and \
                        abs(raw[i] + raw[j]) <= max(1e-3, 0.03 * abs(raw[j])):
                    raw[i] = raw[j] = 0.0
                    cancelled += 2
                    break
        # 2) anything still implausible is dropped: an offset increase, or a payout > 5% of price that
        #    is not confirmed by an opening gap down of at least half the payout in unadjusted prices
        U = N if N is not None else S
        prev_close = U["c"].reindex(df.index).shift(1).to_numpy()
        open_now = U["o"].reindex(df.index).to_numpy()
        with np.errstate(invalid="ignore"):
            big = raw > 0.05 * px
            gap_ok = (prev_close - open_now) >= 0.5 * raw
        bad = (raw < 0) | (big & ~gap_ok)
        cutoff = str((pd.Timestamp(df.index[-1]) - pd.DateOffset(years=5)).date())
        recent_bad = [d for d in df.index[bad] if d >= cutoff]
        if cancelled:
            flags.append(f"{cancelled} glitch jumps in dividend-adjusted series cancelled")
        if bad.any():
            flags.append(f"{int(bad.sum())} implausible dividend value(s) dropped (last {df.index[bad][-1]})")
        if recent_bad:
            flags.append("anomaly inside the last 5 years; dividends unverified")
            verified = False
        raw[bad] = 0.0
        raw = pd.Series(raw, index=df.index)
        if N is not None:
            F = (N["c"].reindex(df.index) / df["close"]).round(6).fillna(1.0)
            splits = F.diff().abs() > 1e-3
            if splits.any():
                flags.append("split(s) on " + ",".join(F.index[splits.fillna(False)].tolist()))
            df["div"] = raw / F
        else:
            df["div"] = raw
    p = df["close"].to_numpy()
    dv = df["div"].to_numpy()
    tr = np.ones(len(p))
    for i in range(1, len(p)):
        tr[i] = tr[i - 1] * (p[i] + dv[i]) / p[i - 1]
    df["tr"] = tr
    df["verified"] = bool(verified)
    df.index.name = "date"
    return df.reset_index(), flags, verified


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--results-dir", default=None)
    ap.add_argument("--since-minutes", type=int, default=240)
    ap.add_argument("--expect", type=int, default=3, help="calls per batch: 3 (split/all/none) or 1 (split only)")
    ap.add_argument("--interval", default="day")
    a = ap.parse_args()
    os.makedirs(a.workdir, exist_ok=True)

    files = find_files(a.results_dir, a.since_minutes)
    if not files:
        print("ERROR: 0 saved historicals files found. Locate them with:\n"
              "  find / -name '*get_equity_historicals*.txt' -mmin -240 2>/dev/null\n"
              "then re-run with --results-dir <that directory>.")
        sys.exit(2)

    groups = {}
    for f in files:
        try:
            interval, data = parse_file(f)
        except Exception as e:  # noqa: BLE001
            print(f"skip {os.path.basename(f)}: {e}")
            continue
        if not data or (interval and interval != a.interval):
            continue
        firsts = [df.index[0] for df in data.values()]
        lasts = [df.index[-1] for df in data.values()]
        key = (tuple(sorted(data)), min(firsts)[:7], max(lasts))
        g = groups.setdefault(key, {"files": [], "sets": {}})
        g["files"].append(os.path.basename(f))
        g["sets"].setdefault(content_hash(data), data)

    report = {"files_used": sum(len(g["files"]) for g in groups.values()), "batches": [], "tickers": {}}
    frames = {}
    for key, g in groups.items():
        sets = list(g["sets"].values())
        S, A, N, notes = classify(sets, len(g["files"]), a.expect)
        report["batches"].append({"symbols": list(key[0]), "files": len(g["files"]),
                                  "distinct": len(sets), "notes": notes})
        for sym in S:
            df, flags, ver = build_symbol(sym, S[sym],
                                          A.get(sym) if A else None,
                                          N.get(sym) if N else None)
            prev = frames.get(sym)
            # keep the longest / most verified version if a symbol appears in several batches
            if prev is not None:
                pv = report["tickers"][sym]
                if (pv["verified"], pv["bars"]) >= (ver, len(df)):
                    continue
            frames[sym] = df
            report["tickers"][sym] = {"first": df["date"].iloc[0], "last": df["date"].iloc[-1],
                                      "bars": int(len(df)), "verified": bool(ver) if a.expect == 3 else None,
                                      "flags": flags + notes}

    if not frames:
        print("ERROR: no usable bars in the saved files")
        sys.exit(2)
    out = pd.concat([f.assign(ticker=s) for s, f in frames.items()], ignore_index=True)
    out = out[["date", "ticker", "close", "volume", "div", "tr", "verified"]]
    out.to_csv(os.path.join(a.workdir, "daily.csv.gz"), index=False, compression="gzip")
    save_json(os.path.join(a.workdir, "ingest_report.json"), report)

    unver = sorted(s for s, t in report["tickers"].items() if a.expect == 3 and not t["verified"])
    print(f"ingest: {report['files_used']} files, {len(groups)} batches, {len(frames)} tickers, "
          f"{out['date'].min()}..{out['date'].max()}")
    for b in report["batches"]:
        if b["notes"] or (a.expect == 3 and b["files"] != 3):
            print(f"  batch {','.join(b['symbols'])}: files={b['files']} distinct={b['distinct']} {'; '.join(b['notes'])}")
    if unver:
        print(f"  dividends unverified for: {' '.join(unver)}")


if __name__ == "__main__":
    main()
