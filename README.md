# Commodity Positioning Monitor

Self-updating dashboards of CFTC Commitments of Traders speculative positioning across the
liquid commodity futures complex — aggregated for **all commodities, energy, metals and
agriculture** — each overlaid on a notional-weighted price index for that group.

Two views of the same underlying data:

| Page | Metric | Trader definition | History |
|---|---|---|---|
| `speculative.html` | Net long ÷ **total open interest** | Legacy report — **non-commercial** (large speculators) | 2000 → |
| `managed-money.html` | Net long ÷ **gross MM positioning** (longs + shorts) | Disaggregated report — **managed money** | 2006 → |

Rebuilt automatically every Saturday by GitHub Actions and published with GitHub Pages.
Each dashboard is a single self-contained HTML file — no runtime API calls, no JS
dependencies beyond Google Fonts, so it loads instantly and works offline.

---

## Setup (about 5 minutes)

### 1. Create the repository

```bash
# in this folder
git init -b main
git add .
git commit -m "Commodity positioning monitor"
gh repo create commodity-positioning --public --source=. --push
```

No `gh` CLI? Create an empty repo on github.com, then:

```bash
git remote add origin https://github.com/<you>/commodity-positioning.git
git push -u origin main
```

> The repo can be **private** — Pages works on private repos for GitHub Pro/Team/Enterprise
> accounts. On a free account, the repo must be public for Pages to serve.

### 2. Allow Actions to push commits

**Settings → Actions → General → Workflow permissions** → select
**Read and write permissions** → Save.

This lets the weekly job commit the refreshed `docs/` and `data/prices.csv`. Without it the
build succeeds but the push fails with a 403.

### 3. Turn on GitHub Pages

**Settings → Pages** → Source: **Deploy from a branch** → Branch: **main**, folder: **/docs**
→ Save.

Your dashboard appears within a minute or two at:

```
https://<you>.github.io/commodity-positioning/
```

### 4. Run it once by hand

**Actions → Update dashboard → Run workflow**. First run takes ~2–4 minutes (it downloads
~66 MB of CFTC history); later runs are faster because the history is cached. When it's
green, open your Pages URL.

That's it — it now refreshes itself every Saturday.

---

## How the refresh works

`.github/workflows/update.yml` runs `build.py` on a cron schedule:

```yaml
- cron: "0 9 * * 6"      # Saturday 09:00 UTC
```

The CFTC publishes COT every **Friday at 15:30 ET**, so Saturday morning UTC is safely after
publication (Saturday evening in Melbourne). The job:

1. restores the cached CFTC history files (past years never change),
2. re-downloads the current and previous year plus fresh prices,
3. rebuilds both dashboards into `docs/`,
4. commits the result — Pages redeploys automatically.

Adjust the cadence by editing the cron line (e.g. `"0 9 * * 1"` for Mondays). GitHub may
delay scheduled runs by a few minutes to an hour under load; that's normal and harmless here.

**Note:** scheduled workflows are automatically disabled after **60 days of no repository
activity**. The weekly auto-commit counts as activity, so this stays alive on its own. If you
ever see it stop, open the Actions tab and re-enable it.

---

## Running locally

```bash
pip install -r requirements.txt
python build.py
open docs/index.html
```

Takes about 20 seconds with a warm cache. Downloads land in `data/cot_cache/`, which is
gitignored.

---

## Repository layout

```
build.py                     fetch → aggregate → render (the whole pipeline)
template.html                tokenised dashboard: styling, canvas charts, interactivity
requirements.txt
.github/workflows/update.yml weekly schedule + commit
data/prices.csv              committed price cache (outage fallback)
data/cot_cache/              raw CFTC zips — gitignored, cached in CI
docs/                        published output (GitHub Pages serves this)
  index.html                 landing page
  speculative.html
  managed-money.html
```

---

## Methodology

**Positioning.** For each weekly report, net length = longs − shorts per contract. A group
reading sums net length across its constituents and divides by the summed denominator, so it
is weighted by contract size rather than treating a corn contract like a crude contract.

- `speculative.html` divides by **total open interest** — the conventional COT measure.
- `managed-money.html` divides by **managed money longs + shorts** — the directional skew of
  the speculative book, bounded ±100%.

**Constituents (23 contracts).** Deliberately limited to the liquid, index-relevant futures.
Natural-gas locational basis swaps and power contracts are excluded: they carry enormous open
interest but are hedging instruments that would swamp an energy aggregate.

**Price index.** Constituents are weighted by full-sample average dollar open interest
(average contracts × average price × contract size) and each rebased to 100 at the start of
the sample. Crude carries energy (~54%), gold carries metals (~66%) — as in
production-weighted benchmarks. The composite view also plots the actual **S&P GSCI** as a
dashed cross-check.

**Contract renaming.** CFTC market names drift over 25 years (NYBOT → ICE, unleaded gasoline →
RBOB, `WHEAT` → `WHEAT-SRW`, quoted `'SWEET'`). `classify()` in `build.py` maps every historical
alias to one constituent key, so series don't break at a rename.

### Known limitations

- Minneapolis spring wheat (`MW=F`) has no Yahoo price history — it's included in positioning
  but not in the price index (22 of 23 contracts priced).
- Price indices use front-month continuous futures, so they carry roll effects and are not
  pure spot. Fine for trend context against positioning, which is the intent.
- Yahoo Finance is an unofficial data source. `data/prices.csv` is committed as a fallback so
  a Yahoo outage degrades to stale prices rather than breaking the build.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Push fails, 403 in the log | Step 2 above — set workflow permissions to read/write |
| Pages 404 | Settings → Pages must point at branch `main`, folder `/docs`; check `docs/index.html` exists |
| Charts blank, page otherwise fine | Hard refresh (`Cmd/Ctrl+Shift+R`) — a stale cached HTML |
| `No price data available` | Yahoo blocked the runner *and* `data/prices.csv` wasn't committed; commit it |
| Schedule stopped firing | 60-day inactivity disable — re-enable in the Actions tab |
| Numbers unchanged after a run | CFTC hadn't published yet; re-run after Friday 15:30 ET |

---

Sources: CFTC Commitments of Traders (Legacy and Disaggregated, futures-only); prices via
Yahoo Finance. Not investment advice.
