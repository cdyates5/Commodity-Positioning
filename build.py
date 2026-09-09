#!/usr/bin/env python3
"""
Commodity Positioning Monitor — build script.

Fetches CFTC Commitments of Traders data + commodity futures prices, aggregates
positioning by complex (All / Energy / Metals / Agriculture), and renders two
self-contained HTML dashboards into docs/.

  docs/managed-money.html   Managed money net as % of MM gross (longs + shorts)   [Disaggregated report, 2006+]
  docs/speculative.html     Non-commercial net as % of total open interest        [Legacy report, 2000+]
  docs/index.html           Landing page linking both

Run locally:   python build.py
In CI:         see .github/workflows/update.yml
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
DATA = ROOT / "data"
CACHE = DATA / "cot_cache"
PRICE_CACHE = DATA / "prices.csv"
TEMPLATE = ROOT / "template.html"

UA = {"User-Agent": "Mozilla/5.0 (compatible; commodity-positioning-monitor/1.0)"}
FIRST_LEGACY_YEAR = 2000          # legacy COT (non-commercial) history floor we use
FIRST_DISAGG_YEAR = 2017          # disaggregated annual files; 2006-2016 comes from one combined file

# --------------------------------------------------------------------------------------
# Constituents: key -> (category, yahoo ticker, contract multiplier, price-quote unit)
# unit 0.01 = contract quoted in US cents (grains, softs, livestock) -> convert to dollars
# --------------------------------------------------------------------------------------
CONSTITUENTS = {
    # Energy
    "WTI Crude":        ("Energy", "CL=F", 1000, 1.0),
    "Natural Gas":      ("Energy", "NG=F", 10000, 1.0),
    "RBOB Gasoline":    ("Energy", "RB=F", 42000, 1.0),
    "Heating Oil/ULSD": ("Energy", "HO=F", 42000, 1.0),
    # Metals
    "Gold":      ("Metals", "GC=F", 100, 1.0),
    "Silver":    ("Metals", "SI=F", 5000, 1.0),
    "Copper":    ("Metals", "HG=F", 25000, 1.0),
    "Platinum":  ("Metals", "PL=F", 50, 1.0),
    "Palladium": ("Metals", "PA=F", 100, 1.0),
    # Agriculture
    "Corn":          ("Agriculture", "ZC=F", 5000, 0.01),
    "Soybeans":      ("Agriculture", "ZS=F", 5000, 0.01),
    "Soybean Oil":   ("Agriculture", "ZL=F", 60000, 0.01),
    "Soybean Meal":  ("Agriculture", "ZM=F", 100, 1.0),
    "Wheat SRW":     ("Agriculture", "ZW=F", 5000, 0.01),
    "Wheat HRW":     ("Agriculture", "KE=F", 5000, 0.01),
    "Wheat HRS":     ("Agriculture", "MW=F", 5000, 0.01),   # positioning only (no Yahoo price)
    "Sugar #11":     ("Agriculture", "SB=F", 112000, 0.01),
    "Coffee":        ("Agriculture", "KC=F", 37500, 0.01),
    "Cocoa":         ("Agriculture", "CC=F", 10, 1.0),
    "Cotton":        ("Agriculture", "CT=F", 50000, 0.01),
    "Live Cattle":   ("Agriculture", "LE=F", 40000, 0.01),
    "Lean Hogs":     ("Agriculture", "HE=F", 40000, 0.01),
    "Feeder Cattle": ("Agriculture", "GF=F", 50000, 0.01),
}
CATS = {c: [k for k, v in CONSTITUENTS.items() if v[0] == c] for c in ("Energy", "Metals", "Agriculture")}
CATS["All"] = list(CONSTITUENTS)
GSCI = "^SPGSCI"


def classify(name: str) -> str | None:
    """Map a CFTC market name to a constituent key. Handles ~25 years of renames
    (NYBOT->ICE, unleaded->RBOB, WHEAT->WHEAT-SRW, quoted 'SWEET', etc)."""
    u = str(name).strip().upper()
    nymex = "NEW YORK MERCANTILE" in u
    # Energy
    if u == "WTI-PHYSICAL - NEW YORK MERCANTILE EXCHANGE":
        return "WTI Crude"
    if u.startswith("CRUDE OIL, LIGHT") and nymex and "E-MINY" not in u and "FINANCIAL" not in u:
        return "WTI Crude"
    if u in ("NATURAL GAS - NEW YORK MERCANTILE EXCHANGE", "NAT GAS NYME - NEW YORK MERCANTILE EXCHANGE"):
        return "Natural Gas"
    if nymex and not any(x in u for x in ("FINANCIAL", "CALENDAR", "UP-DOWN", "A2 PL", "CRACK", "MINI")):
        if "UNLEADED GASOLINE, N.Y. HARBOR" in u or "GASOLINE BLENDSTOCK (RBOB)" in u or u.startswith("GASOLINE RBOB"):
            return "RBOB Gasoline"
        if ("NO. 2 HEATING OIL, N.Y. HARBOR" in u or "HEATING OIL, NY HARBOR-ULSD" in u
                or "HEATING OIL- NY HARBOR-ULSD" in u or u.startswith("NY HARBOR ULSD")):
            if "RDAM" not in u and "GASOIL" not in u:
                return "Heating Oil/ULSD"
    # Metals
    if u == "GOLD - COMMODITY EXCHANGE INC.":
        return "Gold"
    if u == "SILVER - COMMODITY EXCHANGE INC.":
        return "Silver"
    if u in ("COPPER-GRADE #1 - COMMODITY EXCHANGE INC.", "COPPER- #1 - COMMODITY EXCHANGE INC."):
        return "Copper"
    if u == "PLATINUM - NEW YORK MERCANTILE EXCHANGE":
        return "Platinum"
    if u == "PALLADIUM - NEW YORK MERCANTILE EXCHANGE":
        return "Palladium"
    # Grains / oilseeds
    if u == "CORN - CHICAGO BOARD OF TRADE":
        return "Corn"
    if u == "SOYBEANS - CHICAGO BOARD OF TRADE":
        return "Soybeans"
    if u == "SOYBEAN OIL - CHICAGO BOARD OF TRADE":
        return "Soybean Oil"
    if u == "SOYBEAN MEAL - CHICAGO BOARD OF TRADE":
        return "Soybean Meal"
    if u in ("WHEAT - CHICAGO BOARD OF TRADE", "WHEAT-SRW - CHICAGO BOARD OF TRADE"):
        return "Wheat SRW"
    if u in ("WHEAT - KANSAS CITY BOARD OF TRADE", "WHEAT-HRW - CHICAGO BOARD OF TRADE",
             "WHEAT-HRW - KANSAS CITY BOARD OF TRADE"):
        return "Wheat HRW"
    if u.startswith("WHEAT - MINNEAPOLIS") or u.startswith("WHEAT-HRSPRING"):
        return "Wheat HRS"
    # Softs
    if u.startswith("SUGAR NO. 11 -"):
        return "Sugar #11"
    if u.startswith("COFFEE C -"):
        return "Coffee"
    if u.startswith("COCOA -"):
        return "Cocoa"
    if u.startswith("COTTON NO. 2 -"):
        return "Cotton"
    # Livestock
    if u == "LIVE CATTLE - CHICAGO MERCANTILE EXCHANGE":
        return "Live Cattle"
    if u == "LEAN HOGS - CHICAGO MERCANTILE EXCHANGE":
        return "Lean Hogs"
    if u == "FEEDER CATTLE - CHICAGO MERCANTILE EXCHANGE":
        return "Feeder Cattle"
    return None


# --------------------------------------------------------------------------------------
# Download helpers
# --------------------------------------------------------------------------------------
def fetch_zip(url: str, cache_name: str, refresh: bool) -> bytes | None:
    """Download a CFTC zip, caching historical years on disk. Current-year files are
    always re-fetched (refresh=True) so new weekly reports are picked up."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / cache_name
    if path.exists() and not refresh:
        return path.read_bytes()
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=120, headers=UA)
            if r.status_code == 200 and len(r.content) > 1000:
                path.write_bytes(r.content)
                print(f"  fetched {cache_name} ({len(r.content)//1024} KB)")
                return r.content
            print(f"  {cache_name}: HTTP {r.status_code}")
            return path.read_bytes() if path.exists() else None
        except Exception as e:                                   # network hiccup -> retry
            print(f"  {cache_name} attempt {attempt+1} failed: {e}")
            time.sleep(3)
    return path.read_bytes() if path.exists() else None


def read_zip_frames(blob: bytes, usecols, encoding="latin-1") -> list[pd.DataFrame]:
    out = []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for nm in z.namelist():
            if not nm.lower().endswith(".txt"):
                continue
            with z.open(nm) as fh:
                out.append(pd.read_csv(fh, usecols=usecols, encoding=encoding, low_memory=False))
    return out


def load_disaggregated(this_year: int) -> pd.DataFrame:
    """Managed Money positions. 2006-2016 ships as one combined file; 2017+ annually."""
    print("Loading disaggregated (managed money) COT...")
    cols = ["Market_and_Exchange_Names", "Report_Date_as_YYYY-MM-DD", "Open_Interest_All",
            "M_Money_Positions_Long_All", "M_Money_Positions_Short_All"]
    frames = []
    blob = fetch_zip("https://www.cftc.gov/files/dea/history/fut_disagg_txt_hist_2006_2016.zip",
                     "disagg_2006_2016.zip", refresh=False)
    if blob:
        frames += read_zip_frames(blob, cols)
    for y in range(FIRST_DISAGG_YEAR, this_year + 1):
        blob = fetch_zip(f"https://www.cftc.gov/files/dea/history/fut_disagg_txt_{y}.zip",
                         f"disagg_{y}.zip", refresh=(y >= this_year - 1))
        if blob:
            frames += read_zip_frames(blob, cols)
    if not frames:
        raise SystemExit("No disaggregated COT data could be loaded.")
    df = pd.concat(frames, ignore_index=True)
    df.columns = ["name", "date", "oi", "long", "short"]
    df["key"] = df["name"].map(classify)
    return _tidy(df)


def load_legacy(this_year: int) -> pd.DataFrame:
    """Non-commercial (large speculator) positions, one file per year."""
    print("Loading legacy (non-commercial) COT...")
    cols = ["Market and Exchange Names", "As of Date in Form YYYY-MM-DD", "Open Interest (All)",
            "Noncommercial Positions-Long (All)", "Noncommercial Positions-Short (All)"]
    frames = []
    for y in range(FIRST_LEGACY_YEAR, this_year + 1):
        blob = fetch_zip(f"https://www.cftc.gov/files/dea/history/deacot{y}.zip",
                         f"legacy_{y}.zip", refresh=(y >= this_year - 1))
        if blob:
            frames += read_zip_frames(blob, cols)
    if not frames:
        raise SystemExit("No legacy COT data could be loaded.")
    df = pd.concat(frames, ignore_index=True)
    df.columns = ["name", "date", "oi", "long", "short"]
    df["key"] = df["name"].map(classify)
    return _tidy(df)


def _tidy(df: pd.DataFrame) -> pd.DataFrame:
    df = df[df["key"].notna()].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in ("oi", "long", "short"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date", "oi", "long", "short"])
    df = df[df["oi"] > 0]
    df["net"] = df["long"] - df["short"]
    df["gross"] = df["long"] + df["short"]
    df["cat"] = df["key"].map(lambda k: CONSTITUENTS[k][0])
    # a contract can appear under two names in a transition year -> keep the larger OI
    df = df.sort_values("oi").drop_duplicates(["date", "key"], keep="last")
    print(f"  {len(df):,} rows | {df['date'].nunique():,} weeks | "
          f"{df['date'].min().date()} -> {df['date'].max().date()}")
    return df


# --------------------------------------------------------------------------------------
# Prices (cached to data/prices.csv so a Yahoo outage can't break the build)
# --------------------------------------------------------------------------------------
def load_prices() -> pd.DataFrame:
    cached = pd.DataFrame()
    if PRICE_CACHE.exists():
        cached = pd.read_csv(PRICE_CACHE, parse_dates=["date"]).set_index("date")
        print(f"Price cache: {cached.shape[1]} tickers, through {cached.index.max().date()}")

    tickers = [v[1] for v in CONSTITUENTS.values()] + [GSCI]
    fetched = {}
    try:
        import yfinance as yf
        for t in tickers:
            try:
                h = yf.Ticker(t).history(period="max", auto_adjust=False)
                if len(h):
                    s = h["Close"].copy()
                    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
                    fetched[t] = s[~s.index.duplicated(keep="last")]
            except Exception as e:
                print(f"  {t}: fetch failed ({str(e)[:60]})")
        print(f"Fetched {len(fetched)}/{len(tickers)} tickers from Yahoo")
    except Exception as e:
        print(f"yfinance unavailable ({e}); relying on cache")

    live = pd.DataFrame(fetched).sort_index() if fetched else pd.DataFrame()
    if live.empty and cached.empty:
        raise SystemExit("No price data available (no fetch, no cache).")
    if cached.empty:
        merged = live
    elif live.empty:
        merged = cached
    else:
        # union of columns and dates; fresh values win, cache fills any gaps
        merged = live.combine_first(cached)
        for c in cached.columns:
            if c not in merged.columns:
                merged[c] = cached[c]
    merged = merged.sort_index()
    merged.index = pd.to_datetime(merged.index).as_unit("ns")
    DATA.mkdir(parents=True, exist_ok=True)
    merged.round(4).to_csv(PRICE_CACHE, index_label="date")
    print(f"Price panel: {merged.shape[1]} tickers, {merged.index.min().date()} -> {merged.index.max().date()}")
    return merged


# --------------------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------------------
def build_payload(cot: pd.DataFrame, prices: pd.DataFrame, denominator: str) -> dict:
    """denominator: 'gross' -> net / (longs+shorts);  'oi' -> net / total open interest."""
    dates = pd.to_datetime(pd.Index(sorted(cot["date"].unique()))).as_unit("ns")
    mean_oi = cot.groupby("key")["oi"].mean()

    aligned = {}
    for k, (_, tkr, _, _) in CONSTITUENTS.items():
        if tkr not in prices.columns:
            continue
        s = prices[tkr].dropna()
        if s.empty:
            continue
        m = pd.merge_asof(pd.DataFrame({"date": dates}),
                          pd.DataFrame({"date": pd.to_datetime(s.index).as_unit("ns"), "price": s.values}),
                          on="date", direction="backward")
        aligned[k] = m.set_index("date")["price"]

    def price_index(keys):
        keys = [k for k in keys if k in aligned]
        w = {}
        for k in keys:
            _, tkr, mult, unit = CONSTITUENTS[k]
            w[k] = mean_oi.get(k, 0) * (aligned[k].mean() * unit) * mult   # avg dollar open interest
        tot = sum(w.values())
        w = {k: w[k] / tot for k in keys}
        P = pd.DataFrame({k: aligned[k] for k in keys}).dropna()
        idx = 100 * (P.divide(P.iloc[0], axis=1) * pd.Series(w)).sum(axis=1)
        return idx, {k: round(100 * v, 1) for k, v in w.items()}

    gs = None
    if GSCI in prices.columns:
        s = prices[GSCI].dropna()
        gs = pd.merge_asof(pd.DataFrame({"date": dates}),
                           pd.DataFrame({"date": pd.to_datetime(s.index).as_unit("ns"), "price": s.values}),
                           on="date", direction="backward").set_index("date")["price"]

    payload = {"meta": {}, "categories": {}, "weights": {}, "constituents": CATS}
    for cat in ("All", "Energy", "Metals", "Agriculture"):
        sub = cot if cat == "All" else cot[cot["cat"] == cat]
        g = sub.groupby("date").apply(
            lambda x: 100 * x["net"].sum() / x[denominator].sum(), include_groups=False)
        idx, w = price_index(CATS[cat])
        s = pd.DataFrame({"mm": g})
        s["pidx"] = idx
        if cat == "All" and gs is not None:
            s["gsci"] = 100 * gs / gs.dropna().iloc[0]
        s = s.dropna(subset=["pidx"]).sort_index()          # keep both panels populated
        rec = {
            "date": [d.strftime("%Y-%m-%d") for d in s.index],
            "mm_net_pct": [None if pd.isna(v) else round(v, 2) for v in s["mm"]],
            "price_index": [None if pd.isna(v) else round(v, 2) for v in s["pidx"]],
        }
        if cat == "All" and "gsci" in s:
            rec["gsci_index"] = [None if pd.isna(v) else round(v, 2) for v in s["gsci"]]
        payload["categories"][cat] = rec
        payload["weights"][cat] = w

    all_dates = payload["categories"]["All"]["date"]
    payload["meta"] = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "first_date": all_dates[0],
        "last_date": all_dates[-1],
        "n_weeks": len(all_dates),
    }
    return payload


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------
VARIANTS = {
    "managed-money": dict(
        TITLE="Commodity Positioning Monitor — CFTC Managed Money",
        EYEBROW="CFTC Disaggregated · Managed Money",
        SUBTITLE=("Speculative <b>managed money net length</b> expressed as net (longs − shorts) over gross "
                  "managed money positioning (longs + shorts) — the directional skew of specs, bounded ±100% — "
                  "aggregated weekly across the liquid futures complex and overlaid on a notional-weighted "
                  "<b>price index</b> for each group."),
        UNIT="% of MM L+S",
        READOUT_LABEL="Managed money net length",
        PANEL_TITLE="'MANAGED MONEY NET LENGTH  ·  % of MM longs + shorts'",
        LEGEND_LABEL="Managed money net % of L+S",
        METHOD_POS=("for each weekly CFTC report, net length = managed money longs − shorts; the group reading "
                    "sums net length across constituents and divides by summed managed money longs + shorts — "
                    "the net directional skew of speculative positioning, bounded ±100%."),
        SOURCE="CFTC Commitments of Traders, Disaggregated Futures-Only (Managed Money).",
        OTHER_HREF="speculative.html",
        OTHER_LABEL="switch to total speculative (% of OI)",
    ),
    "speculative": dict(
        TITLE="Commodity Positioning Monitor — Total Speculative (Non-Commercial)",
        EYEBROW="CFTC Legacy · Non-Commercial (Large Speculators)",
        SUBTITLE=("<b>Total speculative net long</b> — non-commercial longs − shorts — as a share of "
                  "<b>total open interest</b>, aggregated weekly across the liquid futures complex and overlaid "
                  "on a notional-weighted <b>price index</b> for each group."),
        UNIT="% of OI",
        READOUT_LABEL="Total speculative net length",
        PANEL_TITLE="'TOTAL SPECULATIVE NET LENGTH  ·  % of open interest'",
        LEGEND_LABEL="Total speculative net % of OI",
        METHOD_POS=("uses the CFTC Legacy report’s non-commercial category (large speculators). For each weekly "
                    "report, net length = non-commercial longs − shorts; the group reading sums net length across "
                    "constituents and divides by summed total open interest, so it is open-interest-weighted by "
                    "construction."),
        SOURCE="CFTC Commitments of Traders, Legacy Futures-Only (Non-Commercial).",
        OTHER_HREF="managed-money.html",
        OTHER_LABEL="switch to managed money (% of L+S)",
    ),
}


def render(variant: str, payload: dict) -> Path:
    html = TEMPLATE.read_text()
    cfg = dict(VARIANTS[variant])
    cfg["REBASE"] = payload["categories"]["All"]["date"][0]
    for token, value in cfg.items():
        html = html.replace(f"__{token}__", value)
    html = html.replace("__DATA__", json.dumps(payload, separators=(",", ":")))
    leftover = [t for t in ("__DATA__", "__TITLE__", "__UNIT__") if t in html]
    assert not leftover, f"unreplaced tokens: {leftover}"
    DOCS.mkdir(parents=True, exist_ok=True)
    out = DOCS / f"{variant}.html"
    out.write_text(html)
    print(f"  wrote {out.relative_to(ROOT)} ({len(html)//1024} KB)")
    return out


def write_index(summaries: dict) -> None:
    stamp = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC")
    cards = ""
    for variant, s in summaries.items():
        cfg = VARIANTS[variant]
        cards += f"""
      <a class="card" href="{variant}.html">
        <div class="eyebrow">{cfg['EYEBROW']}</div>
        <h2>{cfg['READOUT_LABEL']}</h2>
        <p>{s['blurb']}</p>
        <div class="stat"><span class="v">{s['last']:+.1f}</span><span class="u">{cfg['UNIT']}</span>
          <span class="d">all commodities · {s['last_date']}</span></div>
        <div class="range">{s['n_weeks']:,} weekly observations · {s['first_date']} → {s['last_date']}</div>
      </a>"""
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Commodity Positioning Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{{--bg:#fff;--hair:#e2e6ec;--txt:#161b24;--muted:#586271;--faint:#8b95a3;--amber:#d98315;--price:#2563c9;
--mono:'IBM Plex Mono',ui-monospace,monospace;--disp:'Space Grotesk',-apple-system,Segoe UI,Roboto,sans-serif}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--txt);font-family:var(--disp);padding:clamp(18px,4vw,52px);max-width:940px;margin:0 auto}}
.eyebrow{{font-family:var(--mono);font-size:10.5px;letter-spacing:.2em;color:var(--amber);text-transform:uppercase;font-weight:600}}
h1{{font-size:clamp(28px,5vw,42px);letter-spacing:-.02em;margin:.3em 0 .2em}}
.lede{{color:var(--muted);font-size:15px;line-height:1.6;max-width:64ch}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:34px 0 26px}}
@media(max-width:700px){{.grid{{grid-template-columns:1fr}}}}
.card{{display:block;text-decoration:none;color:inherit;border:1px solid var(--hair);border-radius:14px;padding:20px 22px;
  box-shadow:0 1px 2px rgba(16,24,40,.04),0 2px 6px rgba(16,24,40,.05);transition:.15s}}
.card:hover{{border-color:#c9d3e2;box-shadow:0 3px 10px rgba(16,24,40,.09);transform:translateY(-1px)}}
.card h2{{font-size:19px;margin:.5em 0 .35em}}
.card p{{color:var(--muted);font-size:13.5px;line-height:1.55;margin:0 0 16px}}
.stat{{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}}
.stat .v{{font-family:var(--mono);font-size:30px;font-weight:600;color:var(--price)}}
.stat .u{{font-family:var(--mono);font-size:12px;color:var(--muted)}}
.stat .d{{font-family:var(--mono);font-size:11px;color:var(--faint)}}
.range{{font-family:var(--mono);font-size:11px;color:var(--faint);margin-top:10px;border-top:1px solid var(--hair);padding-top:10px}}
.foot{{font-family:var(--mono);font-size:11.5px;color:var(--faint);line-height:1.7;border-top:1px solid var(--hair);padding-top:16px}}
</style></head><body>
<div class="eyebrow">CFTC Commitments of Traders</div>
<h1>Commodity Positioning Monitor</h1>
<p class="lede">Weekly speculative positioning across the liquid commodity futures complex — aggregated for all
commodities, energy, metals and agriculture — with a notional-weighted price index for each group. Two views of the
same underlying data: choose the trader definition and denominator you prefer.</p>
<div class="grid">{cards}
</div>
<p class="foot">Rebuilt automatically every Saturday from CFTC source files. Last build: {stamp}.<br>
Sources: CFTC Commitments of Traders (Legacy &amp; Disaggregated, futures-only); prices via Yahoo Finance.
Not investment advice.</p>
</body></html>"""
    (DOCS / "index.html").write_text(html)
    print(f"  wrote docs/index.html")


# --------------------------------------------------------------------------------------
def main() -> int:
    t0 = time.time()
    year = datetime.now(timezone.utc).year
    prices = load_prices()

    summaries = {}
    for variant, loader, denom, blurb in (
        ("speculative", load_legacy, "oi",
         "Non-commercial (large speculator) net position as a share of total open interest — the conventional "
         "COT measure, and the longest history."),
        ("managed-money", load_disaggregated, "gross",
         "Managed money net position over its own gross book — a bounded ±100% read on how one-sided "
         "professional speculators are."),
    ):
        cot = loader(year)
        payload = build_payload(cot, prices, denom)
        render(variant, payload)
        a = payload["categories"]["All"]
        summaries[variant] = dict(blurb=blurb, last=a["mm_net_pct"][-1], last_date=a["date"][-1],
                                  first_date=a["date"][0], n_weeks=payload["meta"]["n_weeks"])
        print(f"  {variant}: latest all-commodities {a['mm_net_pct'][-1]:+.1f} as of {a['date'][-1]}")

    write_index(summaries)
    (DOCS / ".nojekyll").write_text("")
    print(f"Done in {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
