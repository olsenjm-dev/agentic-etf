"""Shared constants and helpers for the Top-5 Growth ETF Screen (schema top5-v1)."""
import csv
import json
import os
import sys
from datetime import date, datetime, timezone

SCHEMA = "top5-v1"
FOLDER_ID = "18UKCu-XDK4D_lCOXdvoEMt7-lVliEAUu"
SLACK_CHANNEL = "C0C15AK3K2R"
BENCH = "VOO"

TAXONOMY = [
    "broad-growth", "smid-growth", "technology", "semiconductors", "health-biotech",
    "consumer-comm", "thematic", "factor", "dev-ex-us-growth", "em-growth", "global-growth",
]
MANDATES = ["growth", "thematic", "growth-sector"]
REGIONS = ["US", "developed-ex-US", "emerging", "global"]

UNIVERSE_COLUMNS = [
    "ticker", "name", "mandate", "category", "region", "expense_ratio", "distribution_yield",
    "beta_3y", "inception", "eligible_from", "avg_dollar_volume_30d", "aum",
    "fractional_eligible", "active", "source_url", "last_verified", "notes",
]

# Long-history, liquid tickers used only to pad a Robinhood batch to exactly 10 symbols.
PAD = ["VOO", "SPY", "QQQ", "IVV", "VTI", "IWM", "DIA", "VUG", "IWF", "XLK", "VGT", "SCHG"]

HERE = os.path.dirname(os.path.abspath(__file__))


def die(msg, code=2):
    print(f"ERROR: {msg}")
    sys.exit(code)


# ---------------------------------------------------------------- JSON with schema guard
def load_json(path, required=True, default=None):
    if not os.path.exists(path):
        if required:
            die(f"{path} not found")
        return default
    with open(path, encoding="utf-8") as fh:
        try:
            obj = json.load(fh)
        except json.JSONDecodeError as e:
            die(f"{path} is not valid JSON ({e})")
    sv = obj.get("schema_version") if isinstance(obj, dict) else None
    if sv != SCHEMA:
        die(f"{path}: schema_version is {sv!r}, expected {SCHEMA!r} - hard fail, nothing written")
    return obj


def save_json(path, obj):
    obj = dict(obj)
    obj["schema_version"] = SCHEMA
    # schema_version first for readability
    ordered = {"schema_version": SCHEMA}
    ordered.update({k: v for k, v in obj.items() if k != "schema_version"})
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(ordered, fh, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        fh.write("\n")


def rnd(x, n=2):
    """Round floats for compact JSON; None/NaN -> None."""
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return x
    if xf != xf or xf in (float("inf"), float("-inf")):
        return None
    return round(xf, n)


# ---------------------------------------------------------------- dates
def utc_today():
    return datetime.now(timezone.utc).date()


def parse_date(s):
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            d = datetime.strptime(s, fmt).date()
            return d
        except ValueError:
            pass
    return None


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    import calendar
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def month_label(d):
    return f"{d.year:04d}-{d.month:02d}"


def run_month_first(today):
    return date(today.year, today.month, 1)


def prior_month_label(today):
    return month_label(add_months(run_month_first(today), -1))


def norm_month(s):
    """'2027-03', '2027-03-01', '3/1/2027' -> '2027-03'; blank -> ''."""
    d = parse_date(s)
    return month_label(d) if d else ""


def rfc3339(d):
    return f"{d.isoformat()}T00:00:00Z"


def batches(tickers, size=10, pad=PAD):
    """De-duplicate (order kept), split into chunks of `size`, pad the last chunk to exactly `size`."""
    seen, uniq = set(), []
    for t in tickers:
        t = t.strip().upper()
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    out = [uniq[i:i + size] for i in range(0, len(uniq), size)]
    if out and len(out[-1]) < size:
        last = out[-1]
        for p in pad:
            if len(last) >= size:
                break
            if p not in last:
                last.append(p)
    return out


# ---------------------------------------------------------------- universe CSV
def to_bool(v):
    return str(v).strip().lower() in ("true", "1", "yes", "y", "t")


def to_float(v):
    if v is None:
        return None
    s = str(v).strip().replace("$", "").replace(",", "").replace("%", "")
    if s == "" or s.lower() in ("na", "n/a", "none", "null", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_universe(path):
    if not os.path.exists(path):
        die(f"universe file {path} not found - run the annual universe build first")
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        die(f"{path} has no rows")
    missing = [c for c in ("ticker", "category", "active", "inception") if c not in rows[0]]
    if missing:
        die(f"{path} is missing columns {missing}")
    out = []
    for r in rows:
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if not r.get("ticker"):
            continue
        r["ticker"] = r["ticker"].upper()
        out.append(r)
    return out


def write_universe(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=UNIVERSE_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in UNIVERSE_COLUMNS})


def write_writes(workdir, items):
    """writes.json tells the task exactly which Drive files to create (and which older titles to trash)."""
    save_json(os.path.join(workdir, "out", "writes.json"), {"writes": items})
    print("DRIVE WRITES (in this order):")
    for i, it in enumerate(items, 1):
        print(f"  {i}. title={it['title']!r} file={it['path']} contentMimeType={it['mime']} "
              f"{'replace (trash older same-title files after create)' if it['replace'] else 'new file'}")
