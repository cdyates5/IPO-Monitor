# US IPO Market Dashboard — auto-refreshing

Quarterly US IPO activity since 1990, NYSE vs Nasdaq, with a rolling 12-month forecast and a separate mega-IPO layer (SpaceX, Anthropic, OpenAI …). It covers:

- IPO count
- Aggregate market cap at first-day close
- IPO market cap as a percentage of the S&P 500

GitHub Actions refreshes the data every weekday and publishes the page with GitHub Pages.

**→ Setup takes about 15 minutes: see [SETUP.md](SETUP.md).**

```
data/manual.json ──┐   (your judgement: mega-IPO timing, forecasts, Ritter annual data)
                   ├─► scripts/build.py ─► docs/index.html            (dashboard)
data/auto/*  ──────┘                       docs/data/us_ipo_quarterly.csv
   ▲                                       docs/data/us_ipo_dashboard_data.json
   └── scripts/fetch_data.py  (Nasdaq IPO calendar, SPY, mega-IPO quotes, Ritter PDF watch)

.github/workflows/refresh.yml   weekdays 22:17 UTC + on every change to manual.json / scripts:
                                fetch → build → commit data → deploy Pages → open/close alert issues
```

| Path | What it is |
|---|---|
| `data/manual.json` | Hand-maintained inputs; edit on github.com and the page rebuilds |
| `data/auto/` | Fetched data cache (calendar months back to 2015, SPY closes, quotes, logs) |
| `scripts/fetch_data.py` | Network fetches; every source fails soft and keeps its cache |
| `scripts/build.py` + `scripts/template.html` | Computes every number and renders the page, offline |
| `scripts/raise_issues.py` | Turns fetch failures, build warnings and new Ritter editions into GitHub issues |
| `docs/` | Published site: the dashboard plus stable CSV/JSON data URLs |

The scripts need only Python 3.10+ and its standard library; there are no API keys or secrets. The dashboard itself loads Chart.js, and SheetJS for its Excel export, from cdnjs.

**Downloads on the page:**

- Full dataset as CSV, Excel (seven sheets, including the calibration and calendar detail) or JSON
- Per-chart CSV and PNG

Sources: Jay R. Ritter, *IPO Statistics* (Univ. of Florida); Nasdaq IPO calendar and quote data; company filings and press for mega-IPO details. All figures after Ritter's latest year, every quarterly split and all forecasts are estimates. Not investment advice.
