#!/usr/bin/env python3
"""
fetch_data.py — pull raw inputs into data/auto/. Network only; computes nothing for the dashboard.

Sources (all public, no API keys):
  1. Nasdaq IPO calendar, priced deals by month   -> data/auto/ipo_calendar/YYYY-MM.json
     plus the upcoming-deals table                -> data/auto/upcoming.json
  2. SPY daily closes (S&P 500 denominator)        -> data/auto/spy.json
  3. Quotes for mega-IPO tickers in manual.json    -> data/auto/quotes.json
  4. SHA-256 of Ritter's IPO Statistics PDF        -> data/auto/ritter_watch.json (change = new edition)

Every source fails soft: on error the previous cache is kept and the failure is recorded in
data/auto/fetch_log.json. Exit code is 1 if any source failed (the workflow still builds).

  python3 scripts/fetch_data.py                # normal incremental run
  python3 scripts/fetch_data.py --full         # re-download every calendar month
  python3 scripts/fetch_data.py --only spy     # one source: calendar | spy | quotes | ritter
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ipo_core import (AUTO_DIR, CAL_DIR, MANUAL_PATH, load_json, month_range, num, save_json,  # noqa: E402
                      us_date, utc_stamp)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}
NASDAQ = "https://api.nasdaq.com/api"


def http_get(url, as_json=True, tries=3, timeout=30):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            return json.loads(body) if as_json else body
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed after {tries} tries: {last}")


def trim_priced(r):
    return dict(ticker=r.get("proposedTickerSymbol"), name=r.get("companyName"), exchange=r.get("proposedExchange"),
                price=r.get("proposedSharePrice"), shares=r.get("sharesOffered"),
                priced_date=us_date(r["pricedDate"]) if r.get("pricedDate") else None,
                offer_usd=num(r.get("dollarValueOfSharesOffered")))


def trim_upcoming(r):
    return dict(ticker=r.get("proposedTickerSymbol"), name=r.get("companyName"), exchange=r.get("proposedExchange"),
                price_range=r.get("proposedSharePrice"), shares=r.get("sharesOffered"),
                expected_date=us_date(r["expectedPriceDate"]) if r.get("expectedPriceDate") else None,
                offer_usd=num(r.get("dollarValueOfSharesOffered")))


# ── 1. IPO calendar ───────────────────────────────────────────────────────────
def fetch_calendar(manual, today, full=False):
    cfg = manual["calendar"]
    months = month_range(cfg["start_month"], today.strftime("%Y-%m"))
    recent = set(months[-cfg.get("refresh_recent_months", 3):])
    fetched, kept_previous, upcoming = 0, [], None
    for ym in months:
        path = CAL_DIR / f"{ym}.json"
        if path.exists() and ym not in recent and not full:
            continue
        payload = http_get(f"{NASDAQ}/ipo/calendar?date={ym}")
        data = payload.get("data")
        if not isinstance(data, dict) or "priced" not in data:
            raise RuntimeError(f"unexpected calendar payload for {ym}: {str(payload)[:200]}")
        priced = [trim_priced(r) for r in ((data.get("priced") or {}).get("rows") or [])]
        prev = load_json(path, {})
        if not priced and prev.get("priced"):
            kept_previous.append(ym)   # API glitch: never replace real data with an empty month
        elif priced != prev.get("priced"):   # rewrite only on change, so git history shows real revisions
            save_json(path, {"month": ym, "fetched_utc": utc_stamp(), "priced": priced})
        if ym == months[-1]:
            rows = ((data.get("upcoming") or {}).get("upcomingTable") or {}).get("rows") or []
            upcoming = [trim_upcoming(r) for r in rows]
        fetched += 1
        time.sleep(0.4)
    if upcoming is not None:
        save_json(AUTO_DIR / "upcoming.json", {"fetched_utc": utc_stamp(), "rows": upcoming})
    msg = f"{fetched} month(s) fetched"
    if kept_previous:
        msg += f"; kept cached data for empty responses: {', '.join(kept_previous)}"
    return msg


# ── 2. SPY closes ─────────────────────────────────────────────────────────────
def nasdaq_history(symbol, assetclass, start, today):
    # Note: the endpoint returns nothing when todate is in the past, so always ask up to today.
    url = (f"{NASDAQ}/quote/{symbol}/historical?assetclass={assetclass}"
           f"&fromdate={start}&todate={today.isoformat()}&limit=9999")
    data = http_get(url).get("data") or {}
    rows = ((data.get("tradesTable") or {}).get("rows")) or []
    return {us_date(r["date"]): num(r["close"]) for r in rows if r.get("close")}


def fetch_spy(manual, today):
    store = load_json(AUTO_DIR / "spy.json", {"symbol": "SPY", "closes": {}})
    closes = store["closes"]
    if closes:
        start = (dt.date.fromisoformat(max(closes)) - dt.timedelta(days=10)).isoformat()
    else:
        start = manual["sp500"]["history_start"]
    new = nasdaq_history("SPY", "etf", start, today)
    if not new:
        raise RuntimeError(f"no SPY rows returned from {start}")
    closes.update(new)
    store["closes"] = dict(sorted(closes.items()))
    store["fetched_utc"] = utc_stamp()
    save_json(AUTO_DIR / "spy.json", store, indent=0)
    return f"{len(new)} closes (latest {max(new)})"


# ── 3. Mega-IPO quotes ────────────────────────────────────────────────────────
def fetch_quotes(manual, today):
    store = load_json(AUTO_DIR / "quotes.json", {})
    notes = []
    for m in manual["mega_ipos"]:
        tkr, start = m.get("ticker"), m.get("listing_window_start")
        if not tkr or not start:
            continue
        summary = http_get(f"{NASDAQ}/quote/{tkr}/summary?assetclass=stocks")
        sdata = summary.get("data")
        if not sdata:
            store[tkr] = {"trading": False, "checked_utc": utc_stamp()}
            notes.append(f"{tkr}: not trading")
            continue
        sd = sdata.get("summaryData") or {}
        hist = nasdaq_history(tkr, "stocks", start, today)
        if not hist:
            store[tkr] = {"trading": False, "checked_utc": utc_stamp(), "summary_seen": True}
            notes.append(f"{tkr}: summary but no history yet")
            continue
        first = min(hist)
        conflict = first <= start   # traded on the window's first day -> probably a different, older security
        store[tkr] = {
            "trading": True, "conflict": conflict,
            "first_date": first, "first_close": hist[first],
            "last_date": max(hist), "last_close": hist[max(hist)],
            "market_cap": num((sd.get("MarketCap") or {}).get("value")),
            "previous_close": num((sd.get("PreviousClose") or {}).get("value")),
            "exchange": (sd.get("Exchange") or {}).get("value"),
            "checked_utc": utc_stamp(),
        }
        notes.append(f"{tkr}: first {first} @ {hist[first]}, last {max(hist)} @ {hist[max(hist)]}"
                     + (" [CONFLICT]" if conflict else ""))
    save_json(AUTO_DIR / "quotes.json", store)
    return "; ".join(notes) or "no tickers configured"


# ── 4. Ritter PDF watch ───────────────────────────────────────────────────────
def fetch_ritter(manual, today):
    body = http_get(manual["ritter"]["pdf_url"], as_json=False, timeout=60)
    if not body.startswith(b"%PDF"):
        raise RuntimeError("Ritter URL did not return a PDF")
    digest = hashlib.sha256(body).hexdigest()
    prev = load_json(AUTO_DIR / "ritter_watch.json", {})
    changed = bool(prev.get("sha256")) and prev["sha256"] != digest
    save_json(AUTO_DIR / "ritter_watch.json", {
        "url": manual["ritter"]["pdf_url"], "sha256": digest, "bytes": len(body), "checked_utc": utc_stamp(),
        "changed_utc": utc_stamp() if (changed or not prev) else prev.get("changed_utc"),
        "edition_in_manual": manual["ritter"].get("edition"),
    })
    if changed:
        set_output("ritter_changed", "true")
    if not prev:
        return "baseline hash recorded"
    return "CHANGED since last check" if changed else "unchanged"


def set_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")


SOURCES = {"calendar": fetch_calendar, "spy": fetch_spy, "quotes": fetch_quotes, "ritter": fetch_ritter}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--full", action="store_true", help="re-download every calendar month")
    ap.add_argument("--only", choices=list(SOURCES), action="append", help="run only these sources")
    args = ap.parse_args()

    manual = load_json(MANUAL_PATH)
    today = dt.datetime.now(dt.timezone.utc).date()
    log = load_json(AUTO_DIR / "fetch_log.json", {"sources": {}})
    failed = []
    for name, fn in SOURCES.items():
        if args.only and name not in args.only:
            continue
        try:
            msg = fn(manual, today, full=args.full) if name == "calendar" else fn(manual, today)
            log["sources"][name] = {"ok": True, "utc": utc_stamp(), "message": msg,
                                    "last_success_utc": utc_stamp()}
            print(f"[ok]   {name}: {msg}")
        except Exception as e:  # noqa: BLE001 — every source must fail soft
            prev = log["sources"].get(name, {})
            log["sources"][name] = {"ok": False, "utc": utc_stamp(), "message": str(e)[:500],
                                    "last_success_utc": prev.get("last_success_utc")}
            failed.append(name)
            print(f"[FAIL] {name}: {e}", file=sys.stderr)
    log["last_run_utc"] = utc_stamp()
    save_json(AUTO_DIR / "fetch_log.json", log)
    set_output("failed_sources", ",".join(failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
