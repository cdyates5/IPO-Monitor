# Setting up the auto-refreshing IPO dashboard on GitHub

About 15 minutes, once. After that GitHub refreshes the data every weekday morning, republishes the page, and opens an issue (which emails you) whenever something needs your judgement.

**What you end up with**

- A dashboard at `https://<your-username>.github.io/<repo-name>/`
- A stable CSV at `https://<your-username>.github.io/<repo-name>/data/us_ipo_quarterly.csv` that Excel can refresh from
- A repo whose commit history is the data history

**What you need**

- A GitHub account.
- The repo must be **public** for free GitHub Pages hosting. Private-repo Pages needs GitHub Pro, Team or Enterprise.
- Optional: `git` or the GitHub CLI (`gh`) on your computer. The browser-only route also works.

---

## 1. Create the repository

**Option A — GitHub CLI (fastest)**

```bash
unzip ipo-dashboard-github.zip
cd ipo-dashboard
gh auth login                      # first time only
gh repo create ipo-dashboard --public --source . --push
```

**Option B — git**

1. On github.com click **New repository**. Name it `ipo-dashboard`, make it **Public**, and don't add a README.
2. Then run:

```bash
unzip ipo-dashboard-github.zip
cd ipo-dashboard
git init -b main
git add .
git commit -m "Initial IPO dashboard"
git remote add origin https://github.com/<your-username>/ipo-dashboard.git
git push -u origin main
```

**Option C — browser only**

1. Create the empty public repo as in Option B.
2. On the repo page click **uploading an existing file**.
3. Drag in the *contents* of the unzipped `ipo-dashboard` folder (`data`, `docs`, `scripts`, `README.md`, `SETUP.md` and the rest), then click **Commit changes**.
4. Browsers often skip the hidden `.github` folder, so add the workflow by hand:
   1. Click **Add file → Create new file**.
   2. Type the name `.github/workflows/refresh.yml`.
   3. Paste in the contents of that file from the zip and commit.

## 2. Turn on GitHub Pages

1. Go to **Settings → Pages**.
2. Under **Build and deployment → Source**, choose **GitHub Actions**.

That's all. There's no branch or folder to pick, because the workflow publishes the `docs/` folder itself.

## 3. Check workflow permissions

1. Go to **Settings → Actions → General → Workflow permissions**.
2. Select **Read and write permissions** and click Save.

The workflow file already requests what it needs. This setting just avoids a 403 error on the commit step in accounts where the default is read-only.

## 4. Run it once

1. Open the **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**.
2. Choose **Refresh IPO dashboard → Run workflow → Run workflow**.
3. Wait about a minute. You should see two green jobs, **refresh** and **deploy**.
   - The deploy job shows the page URL.
   - The first deploy can take another minute or two to go live.
4. Open the URL. Under the title there's a row of status chips (IPO calendar ✓, S&P 500 ✓, mega-IPO quotes ✓, Ritter watch ✓) and a "last data refresh" time.

From now on it runs at **22:17 UTC Monday to Friday**, which is 09:17 the next morning in Melbourne or Sydney during daylight saving. It also runs within a minute whenever you change `data/manual.json` or anything in `scripts/`.

## 5. Get notified

GitHub emails you when the workflow opens an issue, as long as you're watching your own repo, which is the default.

To also hear about failed runs, go to **Settings → Notifications → Actions** on your GitHub account (not the repo) and tick "Send notifications for failed workflows only".

## 6. Optional: a live Excel link

1. In Excel, choose **Data → Get Data → From Other Sources → From Web**.
2. Paste `https://<your-username>.github.io/ipo-dashboard/data/us_ipo_quarterly.csv` and click **Load**.

From then on, **Data → Refresh All** pulls the latest numbers. The full dataset (calibration, mega-IPO register, sources) is also published as JSON at `…/data/us_ipo_dashboard_data.json`.

---

## What refreshes by itself and what doesn't

| Refreshes automatically every run | Source |
|---|---|
| IPO counts by exchange and proceeds for the current and recent quarters | Nasdaq IPO calendar (priced deals) |
| The current quarter: quarter-to-date actuals plus the forecast for the days left | same |
| S&P 500 total market cap (the denominator) | SPY closes, scaled to a published anchor |
| Mega-IPO listings: first-day close and current market cap, picked up once the ticker trades | Nasdaq quote data |
| The upcoming-deals table | Nasdaq IPO calendar |
| Calendar-to-Ritter calibration, recomputed whenever Ritter data changes | computed |
| The 12-month window, which rolls forward each quarter | computed |
| An alert when Ritter publishes a new edition | hash of Ritter's PDF |

| Needs you (the workflow opens an issue when it does) | Where to edit |
|---|---|
| Mega-IPO timing and valuation ranges, and adding new candidates | `data/manual.json` → `mega_ipos` |
| Extending the base-case forecast as the window rolls on | `data/manual.json` → `forecast.quarters` |
| Adding a new Ritter year (early each year) | `data/manual.json` → `ritter.annual` |
| Excluding cross-listings or ADRs that slip past the filter | `data/manual.json` → `exclude_tickers` |
| Re-anchoring the S&P 500 market cap (once a year) | `data/manual.json` → `sp500.anchor` |

You can edit `data/manual.json` straight on github.com (pencil icon → edit → **Commit changes**). The workflow rebuilds and redeploys automatically. Keep it valid JSON: double quotes, no trailing commas.

## Routine maintenance

**When a mega-IPO's details change**
- Edit its entry in `mega_ipos`: `expected_quarter` (for example `"Q1 2027"`), `valuation_low_bn`, `valuation_high_bn`, `valuation_mid_bn`.
- As soon as a ticker is known, set `ticker` and `listing_window_start` (the public S-1 date works well).
- The listing then appears automatically on the first trading day. Market cap = first-day close × shares implied by Nasdaq's market cap. To pin a published figure instead, set `first_day_mktcap_bn`.

**When a mega-IPO slips**
- An expected deal that hasn't listed is rolled into the current quarter and flagged in a "build warnings" issue.
- Update `expected_quarter`, or set it to `null` to take the deal off the chart.

**To add a new candidate**
- Copy an existing entry, give it a new `id`, and set `"card": true` if you want a summary card for it.

**Each quarter**
- When a quarter has no entry in `forecast.quarters`, the default is used and a warning issue opens. Add the next quarter's base case (`count`, `nyse_share`, `mktcap_bn`, `note`).

**When the Ritter issue opens** (usually January–March, plus revisions during the year)
1. Open the PDF.
2. Append the new year to `ritter.annual` using the columns listed in `_columns`.
3. Check the previous two years for revisions and update `ritter.edition`.
4. Commit. That year then switches from calendar-derived to official figures (split across quarters by the calendar's actual shape), and the calibration recalculates.

**Once a year**
- Update `sp500.anchor` with a published S&P 500 total market cap and its date.

## Running it on your own computer

You need Python 3.10+ and nothing else; the scripts use only the standard library.

```bash
python3 scripts/fetch_data.py      # pull fresh data into data/auto/
python3 scripts/build.py           # rebuild docs/index.html and docs/data/*
open docs/index.html               # macOS; on Windows: start docs\index.html
```

`IPO_AS_OF=2027-01-05 python3 scripts/build.py` simulates a later run date, which is useful for checking the window roll-over before it happens.

## Troubleshooting

**"data fetch failing" issue**
- The page keeps building from cached data, so nothing breaks; the status chip shows ✗ and the issue lists the error.
- Nasdaq's API occasionally refuses requests from cloud IP addresses or changes its format.
- Re-run from the Actions tab later. If it keeps failing, run `python3 scripts/fetch_data.py` on your own computer and push `data/auto/`.
- The issue closes itself after the next fully successful run.

**Commit step fails with 403**
- Repeat step 3 (Read and write permissions).

**Page shows 404**
- Check **Settings → Pages → Source = GitHub Actions** and that the deploy job is green. Allow a couple of minutes after the first deploy.

**Scheduled runs stopped**
- GitHub pauses schedules in repos with no activity for 60 days. The dashboard shows a red "stale" chip after 4 days without a refresh.
- Go to Actions → Refresh IPO dashboard → **Enable workflow**.

**Excel export button falls back to CSV**
- The Excel export loads a small library from a CDN in your browser. On an offline or locked-down network, use the CSV or JSON buttons instead.

**Cost**
- GitHub Actions is free for public repos. Private repos get 2,000 free minutes a month; this uses about 1–2 minutes per run.

## How the numbers are made (short version)

- **1990–2025.** Jay Ritter's annual figures (Table 3b counts by exchange; Table 1a market cap at first-day close).
  - From 2015 they are split into quarters using the actual quarterly pattern in the Nasdaq calendar; earlier years use seasonal weights.
- **After Ritter's latest year.** Nasdaq calendar priced deals are filtered to a Table 3b-like universe: NYSE or Nasdaq Global/Global Select, offer ≥ $5, no SPACs, units, funds, trusts or REITs.
  - Counts are multiplied by the pooled Ritter ÷ calendar ratio for 2015–2025. Currently 0.665, and nine of the eleven years sit between 0.63 and 0.70.
  - Base market cap = base proceeds × the pooled Ritter market-cap ÷ proceeds multiple. Currently about 6.5, with a yearly range of 4–11, so quarterly market caps are approximate.
- **Mega-IPOs.** A separate layer at first-day-close market cap. Expected deals use the midpoint of their reported valuation range, with no first-day pop.
- Full notes are in the dashboard footer, the JSON, and the Excel "Notes & sources" sheet.
