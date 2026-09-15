"""
Screening engine shared by screen.py (one month) and backtest.py (many reconstructed months).

All returns are total returns from work/daily.csv.gz (see ingest.py). Units: returns and excesses
in percentage points (pp); expense_ratio and distribution_yield in percent.
"""
import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import BENCH, add_months, parse_date, to_bool, to_float  # noqa: E402


# ------------------------------------------------------------------ price panel
class Panel:
    def __init__(self, daily, universe_rows=None, allow_fallback=True):
        """daily: DataFrame(date,ticker,close,volume,div,tr,verified)."""
        daily = daily.copy()
        daily["date"] = pd.to_datetime(daily["date"])
        if BENCH not in set(daily["ticker"]):
            raise SystemExit(f"ERROR: no {BENCH} data - cannot screen")
        self.dates = pd.DatetimeIndex(sorted(daily.loc[daily.ticker == BENCH, "date"].unique()))
        piv = lambda col: daily.pivot_table(index="date", columns="ticker", values=col, aggfunc="last")  # noqa: E731
        self.close = piv("close").reindex(self.dates)
        self.volume = piv("volume").reindex(self.dates)
        tr = piv("tr").reindex(self.dates)
        ver = daily.groupby("ticker")["verified"].agg(lambda s: bool(pd.Series(s).astype(str).str.lower().eq("true").all()))
        self.verified = ver.to_dict()
        self.fallback = []
        yields = {}
        for r in universe_rows or []:
            y = to_float(r.get("distribution_yield"))
            if y is not None:
                yields[r["ticker"]] = y
        # Fallback: tickers whose dividends could not be verified -> price return + distribution_yield accrual.
        for t in tr.columns:
            if self.verified.get(t, False) or not allow_fallback:
                continue
            y = yields.get(t, 1.3 if t == BENCH else 0.0)
            px = self.close[t]
            ret = px / px.shift(1) - 1 + (y / 100.0) / 252.0
            first = px.first_valid_index()
            idx = ret.index
            val = (1 + ret.where(idx > first, 0.0)).fillna(1.0).cumprod()
            tr[t] = val.where(px.notna())
            self.fallback.append(t)
        self.tr = tr
        self.first_valid = {t: tr[t].first_valid_index() for t in tr.columns}
        self.last_valid = {t: tr[t].last_valid_index() for t in tr.columns}

    def on_or_before(self, d):
        d = pd.Timestamp(d)
        i = self.dates.searchsorted(d, side="right") - 1
        return self.dates[i] if i >= 0 else None

    def month_ends(self):
        s = pd.Series(self.dates, index=self.dates)
        return list(s.groupby([self.dates.year, self.dates.month]).max())

    def value(self, t, d, max_back=5):
        """TR level of t at trading date d (look back up to max_back sessions if missing)."""
        if t not in self.tr.columns or d is None:
            return None
        i = self.dates.get_loc(d)
        col = self.tr[t].to_numpy()
        for j in range(i, max(i - max_back, -1), -1):
            v = col[j]
            if v == v:
                return float(v)
        return None

    def median_dollar_volume(self, t, d, n=30):
        if t not in self.close.columns:
            return None
        i = self.dates.get_loc(d)
        px = self.close[t].iloc[max(0, i - n + 1): i + 1]
        vol = self.volume[t].iloc[max(0, i - n + 1): i + 1]
        dv = (px * vol).dropna()
        return float(dv.median()) if len(dv) else None


# ------------------------------------------------------------------ metrics
def windows(panel, A):
    A = pd.Timestamp(A)
    ad = A.date()
    return {
        "A": A,
        "s6": panel.on_or_before(pd.Timestamp(add_months(ad, -6))),
        "s1": panel.on_or_before(pd.Timestamp(add_months(ad, -12))),
        "s3": panel.on_or_before(pd.Timestamp(add_months(ad, -36))),
        "s5": panel.on_or_before(pd.Timestamp(add_months(ad, -60))),
    }


def fund_age_months(inception, A):
    d = parse_date(inception)
    if d is None:
        return None
    A = pd.Timestamp(A).date()
    m = (A.year - d.year) * 12 + (A.month - d.month) - (1 if A.day < d.day else 0)
    return m


def compute_metrics(panel, A, tickers, params, meta):
    """Return (DataFrame of metrics indexed by ticker, bench dict, gaps dict)."""
    W = windows(panel, A)
    A = W["A"]
    gaps = {}
    vb = {k: panel.value(BENCH, W[k]) for k in ("A", "s6", "s1", "s3")}
    if None in vb.values():
        raise SystemExit(f"ERROR: {BENCH} lacks data for the 3Y window ending {A.date()}")
    bench = {"r6": vb["A"] / vb["s6"] - 1, "r1": vb["A"] / vb["s1"] - 1, "r3": vb["A"] / vb["s3"] - 1}
    bench["cagr3"] = (1 + bench["r3"]) ** (1 / 3) - 1

    n = int(params["corr_window_days"])
    iA = panel.dates.get_loc(A)
    if iA - n < 0:
        raise SystemExit(f"ERROR: not enough {BENCH} history for a {n}-day correlation window")
    win = panel.dates[iA - n: iA + 1]
    tr_win = panel.tr.reindex(columns=list(dict.fromkeys(list(tickers) + [BENCH]))).loc[win]
    rets = tr_win.pct_change().iloc[1:]
    vret = rets[BENCH]

    rows = []
    for t in tickers:
        if t == BENCH:
            continue
        if t not in panel.tr.columns:
            gaps[t] = "no price data"
            continue
        v = {k: panel.value(t, W[k]) for k in ("A", "s6", "s1", "s3")}
        if v["A"] is None:
            gaps[t] = f"no price on {A.date()}"
            continue
        if None in (v["s6"], v["s1"], v["s3"]):
            gaps[t] = "price history shorter than 3Y window"
            continue
        r6, r1, r3 = v["A"] / v["s6"] - 1, v["A"] / v["s1"] - 1, v["A"] / v["s3"] - 1
        cagr3 = (1 + r3) ** (1 / 3) - 1
        fr = rets[t]
        ok = fr.notna() & vret.notna()
        if ok.sum() < int(0.8 * n):
            gaps[t] = f"only {int(ok.sum())} of {n} daily returns in correlation window"
            continue
        act = (fr[ok] - vret[ok])
        te = act.std(ddof=1) * math.sqrt(252)
        ir = (act.mean() * 252) / te if te > 0 else float("nan")
        # max drawdown over the longer of 3Y or available history up to mdd_max_years
        start_cap = panel.on_or_before(pd.Timestamp(add_months(A.date(), -12 * int(params["mdd_max_years"]))))
        fv = panel.first_valid.get(t)
        start = max(d for d in (start_cap, fv) if d is not None)
        start = min(start, W["s3"])
        seg = panel.tr[t].loc[start:A].dropna()
        mdd = float((seg / seg.cummax() - 1).min() * 100) if len(seg) else float("nan")
        mdd_years = (A - seg.index[0]).days / 365.25 if len(seg) else float("nan")
        m = meta.get(t, {})
        rows.append({
            "ticker": t,
            "category": m.get("category", ""),
            "region": m.get("region", ""),
            "er": to_float(m.get("expense_ratio")) or 0.0,
            "r6": r6 * 100, "r1": r1 * 100, "r3": r3 * 100,
            "ex6c": (r6 - bench["r6"]) * 100,
            "ex6a": ((1 + r6) ** 2 - (1 + bench["r6"]) ** 2) * 100,
            "ex1": (r1 - bench["r1"]) * 100,
            "ex3c": (r3 - bench["r3"]) * 100,
            "ex3a": (cagr3 - bench["cagr3"]) * 100,
            "ir": ir, "te": te * 100,
            "mdd": mdd, "mdd_years": mdd_years,
        })
    df = pd.DataFrame(rows).set_index("ticker") if rows else pd.DataFrame(
        columns=["category", "region", "er", "r6", "r1", "r3", "ex6c", "ex6a", "ex1", "ex3c", "ex3a",
                 "ir", "te", "mdd", "mdd_years"])
    bench_pp = {k: v * 100 for k, v in bench.items()}
    return df, bench_pp, gaps, rets


def stage_b(df, params):
    thr = float(params["excess_min_pp"])
    fails = {}
    for t, r in df.iterrows():
        bad = [f"{lbl} {r[col]:+.1f}pp" for lbl, col in (("6M", "ex6c"), ("1Y", "ex1"), ("3Y", "ex3c")) if not r[col] >= thr]
        if bad:
            fails[t] = "below VOO+{:.1f}pp on ".format(thr) + ", ".join(bad)
    survivors = [t for t in df.index if t not in fails]
    return survivors, fails


def score(df, params, incumbents, use_bonus=True):
    if params.get("scoring", "raw") == "ir":
        base = float(params.get("ir_scale", 10.0)) * df["ir"].fillna(0.0)
    else:
        base = (float(params["w_6m"]) * df["ex6a"] + float(params["w_1y"]) * df["ex1"]
                + float(params["w_3y"]) * df["ex3a"])
    s = base - float(params["er_penalty"]) * df["er"]
    if use_bonus:
        bonus = pd.Series([float(params["incumbent_bonus"]) if t in incumbents else 0.0 for t in df.index], index=df.index)
        s = s + bonus
    return s


def residual_corr(rets, survivors):
    v = rets[BENCH]
    res = {}
    for t in survivors:
        f = rets[t]
        ok = f.notna() & v.notna()
        x, y = v[ok].to_numpy(), f[ok].to_numpy()
        b = np.cov(x, y, ddof=1)[0, 1] / np.var(x, ddof=1)
        a = y.mean() - b * x.mean()
        e = pd.Series(np.nan, index=rets.index)
        e[ok] = y - (a + b * x)
        res[t] = e
    R = pd.DataFrame(res)
    C = R.corr(min_periods=200)
    return C.fillna(1.0)


def rank_order(sc):
    return sorted(sc.index, key=lambda t: (-round(float(sc[t]), 9), t))


def select(survivors, sc, C, cat, mdd, params):
    """Greedy deterministic selection with relaxation. Returns (selected, steps, corr_used, cat_rule)."""
    n = int(params["top_n"])
    ranked = rank_order(sc.loc[survivors]) if survivors else []
    steps = []
    if len(ranked) <= n:
        if ranked:
            steps.append(f"only {len(ranked)} fund(s) passed the hard filter - short list published as-is")
        return ranked, steps, None, None
    tie = float(params["tie_band"])

    def walk(cmax, use_cat):
        sel = []
        while len(sel) < n:
            cats = {cat.get(s, "") for s in sel}
            passing = [t for t in ranked if t not in sel
                       and all(C.at[t, s] <= cmax + 1e-9 for s in sel)
                       and (not use_cat or cat.get(t, "") not in cats)]
            if not passing:
                break
            top = passing[0]
            band = [t for t in passing if float(sc[top]) - float(sc[t]) <= tie + 1e-9]
            pick = min(band, key=lambda t: (-(mdd.get(t) if mdd.get(t) == mdd.get(t) else -999), -float(sc[t]), t))
            sel.append(pick)
        return sel

    cmax = float(params["resid_corr_max"])
    ceiling = float(params["resid_corr_ceiling"])
    step = float(params["resid_corr_step"])
    sel = walk(cmax, True)
    while len(sel) < n and cmax < ceiling - 1e-9:
        cmax = round(min(cmax + step, ceiling), 4)
        sel = walk(cmax, True)
        steps.append(f"resid_corr_max relaxed to {cmax:.2f} -> {len(sel)} selected")
    use_cat = True
    if len(sel) < n:
        use_cat = False
        sel = walk(cmax, False)
        steps.append(f"category rule dropped (resid_corr_max {cmax:.2f}) -> {len(sel)} selected")
    if len(sel) < n:
        steps.append(f"short list published ({len(sel)} of {n})")
    return sel, steps, cmax, use_cat


def why_not(t, sel, sc, C, cat, cmax, use_cat, tie):
    if cmax is not None:
        blockers = [s for s in sel if C.at[t, s] > cmax + 1e-9]
        if blockers:
            s = max(blockers, key=lambda s: C.at[t, s])
            return f"resid corr {C.at[t, s]:.2f} w/ {s}"
        if use_cat:
            same = [s for s in sel if cat.get(s) == cat.get(t)]
            if same:
                return f"same category as {same[0]}"
    return "score below cut"


def apply_exits(prior, selected, survivors, fails, gaps, deactivated, params):
    """prior: {ticker: months_outside}. Returns (on_watch{t:m}, removed[(t,reason)], added[t])."""
    limit = int(params["exit_months_outside"])
    on_watch, removed = {}, []
    for t, m in sorted(prior.items()):
        if t in selected:
            continue
        if t in deactivated:
            removed.append((t, f"universe deactivation ({deactivated[t]})"))
        elif t in fails:
            removed.append((t, f"failed hard filter: {fails[t]}"))
        else:
            m2 = int(m) + 1
            why = "outside top five" if t in survivors else f"not evaluated: {gaps.get(t, 'not eligible')}"
            if m2 >= limit:
                removed.append((t, f"{why} {m2} consecutive months"))
            else:
                on_watch[t] = m2
    added = [t for t in selected if t not in prior]
    return on_watch, removed, added


def universe_meta(rows):
    return {r["ticker"]: r for r in rows}


def eligible_at(rows, A, params, panel, active_only=True):
    """Stage A: rows active (or on-deck already eligible) with >= min_history_months since inception."""
    out, young = [], {}
    for r in rows:
        if active_only and not to_bool(r.get("active")):
            continue
        age = fund_age_months(r.get("inception"), A)
        if age is None:
            fv = panel.first_valid.get(r["ticker"])
            age = fund_age_months(fv.date().isoformat(), A) if fv is not None else None
        if age is None or age < int(params["min_history_months"]):
            young[r["ticker"]] = age
            continue
        out.append(r["ticker"])
    return out, young
