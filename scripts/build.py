#!/usr/bin/env python3
"""
build.py — compute the dashboard dataset from data/manual.json + data/auto/ and render docs/.
Offline and deterministic (apart from the build timestamp).

Outputs
  docs/index.html                       the dashboard (GitHub Pages serves this)
  docs/data/us_ipo_quarterly.csv        quarterly series — stable URL, usable from Excel Power Query
  docs/data/us_ipo_dashboard_data.json  everything, incl. calibration, mega-IPO register, sources

  python3 scripts/build.py
  IPO_AS_OF=2027-01-05 python3 scripts/build.py     # simulate a later run date (testing)
"""
import csv
import datetime as dt
import html
import io
import json
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ipo_core import (AUTO_DIR, CAL_DIR, DOCS_DIR, MANUAL_PATH, classify, js_round, largest_remainder,  # noqa: E402
                      load_json, parse_q, q_add, q_bounds, q_index, qlabel, quarter_of, utc_now)

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html")
STATUS_LONG = {
    "hist": "historical (Ritter annual)",
    "actual": "actual (Nasdaq calendar, calibrated to Ritter)",
    "in_progress": "in progress (quarter-to-date + forecast for remaining days)",
    "forecast": "forecast",
}
WARNINGS = []


def warn(msg):
    WARNINGS.append(msg)
    print(f"[warn] {msg}", file=sys.stderr)


# ── inputs ────────────────────────────────────────────────────────────────────
def load_calendar():
    months = {}
    for p in sorted(CAL_DIR.glob("*.json")):
        months[p.stem] = load_json(p).get("priced") or []
    return months


def aggregate_calendar(manual, months):
    """Per-quarter totals of the Ritter-proxy universe, with mega-IPO and excluded tickers split out."""
    mega_t = {m["ticker"].upper(): m for m in manual["mega_ipos"] if m.get("ticker")}
    excl_t = {e["ticker"].upper(): e for e in manual["exclude_tickers"]}
    agg = defaultdict(lambda: dict(nyse=0, nasdaq=0, proc_bn=0.0, deals=[], mega=[], excluded=[], dropped=0))
    for ym, rows in months.items():
        for r in rows:
            y, q = quarter_of(r.get("priced_date") or f"{ym}-15")
            a = agg[(y, q)]
            tkr = (r.get("ticker") or "").upper()
            if tkr in mega_t:
                a["mega"].append(dict(ticker=tkr, name=r.get("name"), offer_bn=r["offer_usd"] / 1e9))
                continue
            if tkr in excl_t:
                a["excluded"].append(dict(ticker=tkr, name=r.get("name"), offer_bn=r["offer_usd"] / 1e9,
                                          reason=excl_t[tkr]["reason"]))
                continue
            ex, _why = classify(r, manual)
            if not ex:
                a["dropped"] += 1
                continue
            a["nyse" if ex == "NYSE" else "nasdaq"] += 1
            a["proc_bn"] += r["offer_usd"] / 1e9
            a["deals"].append(dict(ticker=r.get("ticker"), name=r.get("name"), exchange=ex,
                                   date=r.get("priced_date"), offer_bn=round(r["offer_usd"] / 1e9, 3)))
    for a in agg.values():
        a["deals"].sort(key=lambda d: -d["offer_bn"])
    coverage = defaultdict(int)
    for ym in months:
        coverage[int(ym[:4])] += 1
    return agg, coverage


def calibrate(manual, agg, coverage):
    cfg = manual["calibration"]
    per_year, rn, cn, mc, pr = [], 0, 0, 0.0, 0.0
    for row in manual["ritter"]["annual"]:
        y, ny, nd, _t1c, _t1p, t1a, _sp = row
        if y < cfg["first_year"] or coverage.get(y, 0) < 12:
            continue
        c = sum(agg[(y, q)]["nyse"] + agg[(y, q)]["nasdaq"] for q in range(1, 5))
        p = sum(agg[(y, q)]["proc_bn"] for q in range(1, 5))
        if c == 0 or p == 0:
            continue
        per_year.append(dict(year=y, ritter_t3b_count=ny + nd, calendar_count=c, count_ratio=round((ny + nd) / c, 4),
                             ritter_t1a_mktcap_bn=t1a, calendar_proceeds_bn=round(p, 2),
                             mktcap_per_proceeds=round(t1a / p, 3),
                             ritter_nyse_share=round(ny / (ny + nd), 3),
                             calendar_nyse_share=round(sum(agg[(y, q)]["nyse"] for q in range(1, 5)) / c, 3)))
        rn, cn, mc, pr = rn + ny + nd, cn + c, mc + t1a, pr + p
    if not per_year:
        raise SystemExit("calibration impossible: no year has both a Ritter row and 12 months of calendar data")
    ratio = cfg.get("count_ratio_override") or rn / cn
    mult = cfg.get("mktcap_per_proceeds_override") or mc / pr
    return dict(count_ratio=ratio, mktcap_per_proceeds=mult, years=[p["year"] for p in per_year],
                count_ratio_range=[min(p["count_ratio"] for p in per_year), max(p["count_ratio"] for p in per_year)],
                mult_range=[min(p["mktcap_per_proceeds"] for p in per_year), max(p["mktcap_per_proceeds"] for p in per_year)],
                overridden=bool(cfg.get("count_ratio_override") or cfg.get("mktcap_per_proceeds_override")),
                per_year=per_year)


class SP500:
    """S&P 500 total market cap ($T) from SPY closes scaled to a published anchor."""

    def __init__(self, manual):
        cfg = manual["sp500"]
        self.closes = (load_json(AUTO_DIR / "spy.json", {}) or {}).get("closes", {})
        self.dates = sorted(self.closes)
        self.spy_from = cfg["spy_from"]
        anchor = cfg["anchor"]
        a_close = self.close_on(anchor["date"])
        self.mult = anchor["mktcap_tn"] / a_close if a_close else None
        if not self.mult:
            warn("SPY history does not cover the S&P anchor date; using the last Ritter year-end value instead")
        self.fallback = manual["ritter"]["annual"][-1][6]

    def close_on(self, date):
        prior = [d for d in self.dates if d <= date]
        return self.closes[prior[-1]] if prior else None

    def at(self, date):
        c = self.close_on(date)
        return round(c * self.mult, 2) if (c and self.mult) else self.fallback

    def latest(self):
        return (self.dates[-1], self.at(self.dates[-1])) if self.dates else (None, self.fallback)


def resolve_megas(manual, quotes, cur):
    out = []
    for m in manual["mega_ipos"]:
        q = quotes.get((m.get("ticker") or "").upper()) if m.get("ticker") else None
        r = dict(m, listed=False, quarter=None, mktcap_bn=None, current_mktcap_bn=None, slipped=False, flags=[])
        if q and q.get("trading") and q.get("conflict"):
            r["flags"].append(f"ticker {m['ticker']} traded before the listing window — check the ticker in manual.json")
            warn(r["flags"][-1])
        if q and q.get("trading") and not q.get("conflict"):
            r["listed"] = True
            r["listing_date"] = q["first_date"]
            r["quarter"] = quarter_of(q["first_date"])
            shares = (q["market_cap"] / q["previous_close"]) if q.get("market_cap") and q.get("previous_close") else None
            computed = q["first_close"] * shares / 1e9 if shares else None
            r["mktcap_bn"] = m.get("first_day_mktcap_bn") or computed or m.get("valuation_mid_bn")
            r["first_close"] = q["first_close"]
            r["current_mktcap_bn"] = round(q["market_cap"] / 1e9, 1) if q.get("market_cap") else None
            r["current_date"] = q.get("last_date")
            if not m.get("first_day_mktcap_bn") and not computed:
                r["flags"].append("market cap unavailable — using valuation midpoint")
        elif m.get("expected_quarter"):
            eq = parse_q(m["expected_quarter"])
            if q_index(*eq) < q_index(*cur):
                r["slipped"] = True
                eq = cur
                r["flags"].append(f"{m['name']} was expected in {m['expected_quarter']} but has not listed — "
                                  f"shown in {qlabel(*cur)}; update expected_quarter in manual.json")
                warn(r["flags"][-1])
            r["quarter"] = eq
            r["mktcap_bn"] = m.get("valuation_mid_bn")
        out.append(r)
    return out


def mega_label(r):
    if r["listed"]:
        return f"{r['name']} ({r.get('ticker')}, {r['exchange']}) ${r['mktcap_bn']:,.0f}B first-day close"
    rng = ""
    if r.get("valuation_low_bn") and r.get("valuation_high_bn"):
        rng = f" [range ${r['valuation_low_bn']:,.0f}–{r['valuation_high_bn']:,.0f}B]"
    slip = " (slipped from " + r["expected_quarter"] + ")" if r["slipped"] else ""
    return f"{r['name']} ({r.get('ticker') or 'TBD'}, {r['exchange']}) ${r['mktcap_bn']:,.0f}B est.{rng}{slip}"


def pct(mcap_bn, sp_tn):
    return round(mcap_bn / (sp_tn * 1000) * 100, 3) if sp_tn else 0.0


# ── rows ──────────────────────────────────────────────────────────────────────
def build(manual, as_of):
    months = load_calendar()
    agg, coverage = aggregate_calendar(manual, months)
    cal = calibrate(manual, agg, coverage)
    ratio, mult = cal["count_ratio"], cal["mktcap_per_proceeds"]
    sp = SP500(manual)
    quotes = load_json(AUTO_DIR / "quotes.json", {}) or {}
    cur = quarter_of(as_of)
    horizon = manual["forecast"]["horizon_quarters"]
    last_q = q_add(*cur, horizon - 1)
    megas = resolve_megas(manual, quotes, cur)
    ritter = {r[0]: r for r in manual["ritter"]["annual"]}
    seasonal = manual["seasonal"]
    shape_from = manual["allocation"]["calendar_shape_from_year"]
    fc_by_q = {f["quarter"]: f for f in manual["forecast"]["quarters"]}
    overrides = {k: v for k, v in manual.get("actual_overrides", {}).items() if not k.startswith("_")}

    rows = []
    y0 = manual["ritter"]["annual"][0][0]
    y, q = y0, 1
    while q_index(y, q) <= q_index(*last_q):
        label = qlabel(y, q)
        a = agg.get((y, q))
        row = dict(q=label, y=y, qn=q, alloc="", nyse=0, nasdaq=0, mega_n=0, base_mcap=0.0, mega_mcap=0.0,
                   mega_names="", note="", cal_count=None, cal_proceeds_bn=None)
        if a is not None and coverage.get(y, 0) > 0:
            row["cal_count"] = a["nyse"] + a["nasdaq"]
            row["cal_proceeds_bn"] = round(a["proc_bn"], 2)
        if y in ritter:
            row["status"] = "hist"
            _, ny, nd, _c, _p, t1a, sp_ye = ritter[y]
            use_cal = y >= shape_from and coverage.get(y, 0) == 12
            if q == 1:  # allocate the whole year once
                if use_cal:
                    wn = [agg[(y, k)]["nyse"] for k in range(1, 5)]
                    wd = [agg[(y, k)]["nasdaq"] for k in range(1, 5)]
                    wm = [agg[(y, k)]["proc_bn"] for k in range(1, 5)]
                    wsea = seasonal["overrides"].get(str(y), seasonal["default"])
                    wn, wd, wm = (w if sum(w) > 0 else wsea for w in (wn, wd, wm))
                else:
                    wn = wd = wm = seasonal["overrides"].get(str(y), seasonal["default"])
                year_alloc = dict(nyse=largest_remainder(ny, wn), nasdaq=largest_remainder(nd, wd),
                                  mcap=[round(t1a * w / sum(wm), 1) for w in wm],
                                  alloc="calendar shape" if use_cal else "seasonal weights")
            row.update(nyse=year_alloc["nyse"][q - 1], nasdaq=year_alloc["nasdaq"][q - 1],
                       base_mcap=year_alloc["mcap"][q - 1], alloc=year_alloc["alloc"], sp500=sp_ye)
        else:
            qs, qe = q_bounds(y, q)
            idx, cidx = q_index(y, q), q_index(*cur)
            raw_n = (a["nyse"] + a["nasdaq"]) if a else 0
            raw_nyse = a["nyse"] if a else 0
            raw_p = a["proc_bn"] if a else 0.0
            if idx < cidx:
                row["status"] = "actual"
                n = js_round(raw_n * ratio)
                nyse = js_round(n * raw_nyse / raw_n) if raw_n else 0
                mc = round(raw_p * mult, 1)
                ov = overrides.get(label)
                if ov:
                    n, nyse, mc = ov.get("count", n), ov.get("nyse", nyse), ov.get("mktcap_bn", mc)
                    row["note"] = "manual override; "
                row.update(nyse=nyse, nasdaq=n - nyse, base_mcap=mc, sp500=sp.at(qe.isoformat()))
                row["note"] += f"{raw_n} calendar deals, ${raw_p:.1f}B base proceeds"
                if a and a["deals"]:
                    row["note"] += "; largest: " + ", ".join(f"{d['ticker']} ${d['offer_bn']:.1f}B" for d in a["deals"][:3])
                if a and a["excluded"]:
                    row["note"] += "; excluded: " + ", ".join(f"{e['ticker']} ${e['offer_bn']:.1f}B" for e in a["excluded"])
            else:
                fc = fc_by_q.get(label)
                if not fc:
                    fc = dict(manual["forecast"]["default"], quarter=label)
                    warn(f"no forecast entry for {label}; using forecast.default")
                if idx == cidx:
                    row["status"] = "in_progress"
                    days = (qe - qs).days + 1
                    frac = max(0.0, (qe - as_of).days / days)
                    n_f = raw_n * ratio + fc["count"] * frac
                    nyse_f = raw_nyse * ratio + fc["count"] * frac * fc["nyse_share"]
                    n = js_round(n_f)
                    nyse = min(n, js_round(nyse_f))
                    mc = round(raw_p * mult + fc["mktcap_bn"] * frac, 1)
                    row["note"] = (f"QTD to {as_of.isoformat()}: {raw_n} calendar deals, ${raw_p:.1f}B "
                                   f"(≈{raw_n * ratio:.1f} Ritter-eq., ${raw_p * mult:.0f}B mkt cap) + "
                                   f"{frac:.0%} of forecast ({fc['count']} IPOs, ${fc['mktcap_bn']}B). {fc.get('note', '')}")
                else:
                    row["status"] = "forecast"
                    n = fc["count"]
                    nyse = js_round(n * fc["nyse_share"])
                    mc = float(fc["mktcap_bn"])
                    row["note"] = fc.get("note", "")
                row.update(nyse=nyse, nasdaq=n - nyse, base_mcap=mc, sp500=sp.latest()[1])
        ms = [m for m in megas if m["quarter"] == (y, q) and m["mktcap_bn"]]
        row["mega_n"] = len(ms)
        row["mega_mcap"] = float(sum(m["mktcap_bn"] for m in ms))
        row["mega_listed"] = all(m["listed"] for m in ms) if ms else None
        row["mega_names"] = "; ".join(mega_label(m) for m in ms)
        row["base_count"] = row["nyse"] + row["nasdaq"]
        row["total_count"] = row["base_count"] + row["mega_n"]
        row["total_mcap"] = round(row["base_mcap"] + row["mega_mcap"], 1)
        row["base_ratio"] = pct(row["base_mcap"], row["sp500"])
        row["mega_ratio"] = pct(row["mega_mcap"], row["sp500"])
        row["total_ratio"] = pct(row["total_mcap"], row["sp500"])
        rows.append(row)
        y, q = q_add(y, q, 1)

    for m in megas:
        if m["quarter"] and q_index(*m["quarter"]) > q_index(*last_q):
            m["flags"].append("beyond the 12-month window — not charted")

    detail = []
    for (yy, qq), a in sorted(agg.items()):
        if yy in ritter or q_index(yy, qq) > q_index(*cur):
            continue
        detail.append(dict(quarter=qlabel(yy, qq), calendar_nyse=a["nyse"], calendar_nasdaq=a["nasdaq"],
                           calendar_proceeds_bn=round(a["proc_bn"], 2), dropped_by_filter=a["dropped"],
                           mega=", ".join(f"{d['ticker']} ${d['offer_bn']:.1f}B" for d in a["mega"]),
                           excluded=", ".join(f"{d['ticker']} ${d['offer_bn']:.1f}B" for d in a["excluded"]),
                           largest_deals=", ".join(f"{d['ticker']} ({d['exchange']}) ${d['offer_bn']:.2f}B"
                                                   for d in a["deals"][:5])))
    return rows, cal, megas, detail, sp, agg, cur


# ── render ────────────────────────────────────────────────────────────────────
def fmt_t(bn):
    return f"${bn / 1000:.2f}T" if bn >= 1000 else f"${bn:,.0f}B"


def short_date(iso):
    d = dt.date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b')}"


def cards_html(megas, rows, agg, cur, upcoming):
    out = []
    for m in megas:
        if not m.get("card"):
            continue
        title = html.escape(m.get("detail", ""), quote=True)
        if m["listed"]:
            label = f"{m['name']} · {qlabel(*m['quarter'])} ✓"
            value, color = fmt_t(m["mktcap_bn"]), "#1D9E75"
            sub = f"Listed {short_date(m['listing_date'])} · first-day close"
            if m.get("current_mktcap_bn"):
                sub += f" · now {fmt_t(m['current_mktcap_bn'])}"
        elif m["quarter"]:
            label = f"{m['name']} · {qlabel(*m['quarter'])}"
            value, color = "~" + fmt_t(m["mktcap_bn"]), "#D85A30"
            bits = [b for b in (m["exchange"] if m["exchange"] != "TBD" else "", m.get("ticker") or "") if b]
            sub = (" ".join(bits) + " · " if bits else "")
            if m.get("valuation_low_bn"):
                sub += f"range {fmt_t(m['valuation_low_bn'])}–{fmt_t(m['valuation_high_bn'])}"
            if m["slipped"]:
                sub += f" · slipped from {m['expected_quarter']}"
        else:
            label, value, color = m["name"], "Private", None
            sub = f"Valued ~{fmt_t(m['valuation_mid_bn'])} · no IPO assumed in window"
        style = f' style="color:{color}"' if color else ""
        cls = "value" if color else "value delayed"
        out.append(f'  <div class="meta-card" title="{title}"><div class="label">{html.escape(label)}</div>'
                   f'<div class="{cls}"{style}>{html.escape(value)}</div><div class="sub">{html.escape(sub)}</div></div>')
    a = agg.get(cur)
    n = (a["nyse"] + a["nasdaq"]) if a else 0
    p = a["proc_bn"] if a else 0.0
    nxt = sorted([u for u in upcoming if u.get("offer_usd")], key=lambda u: -u["offer_usd"])[:1]
    sub = f"${p:.1f}B raised (main boards, ex-mega)"
    if nxt:
        sub += f" · next big: {nxt[0]['ticker']} {short_date(nxt[0]['expected_date'])}" if nxt[0].get("expected_date") else ""
    out.append(f'  <div class="meta-card"><div class="label">{qlabel(*cur)} to date</div>'
               f'<div class="value">{n} IPOs</div><div class="sub">{html.escape(sub)}</div></div>')
    return "\n".join(out)


def upcoming_html(upcoming):
    rows = sorted([u for u in upcoming if u.get("offer_usd")], key=lambda u: (u.get("expected_date") or "", -u["offer_usd"]))
    if not rows:
        return '<p class="muted">No deals currently on the Nasdaq upcoming calendar.</p>'
    trs = "".join(
        f"<tr><td>{html.escape(short_date(u['expected_date']) if u.get('expected_date') else '—')}</td>"
        f"<td>{html.escape(u.get('ticker') or '')}</td><td>{html.escape((u.get('name') or '')[:42])}</td>"
        f"<td>{html.escape(u.get('exchange') or '')}</td><td>{html.escape(u.get('price_range') or '')}</td>"
        f"<td class='num'>${u['offer_usd'] / 1e6:,.0f}M</td></tr>" for u in rows[:12])
    return ("<table class='upc'><thead><tr><th>Expected</th><th>Ticker</th><th>Company</th><th>Exchange</th>"
            f"<th>Range</th><th class='num'>Offer</th></tr></thead><tbody>{trs}</tbody></table>")


def status_html(log, as_of):
    parts = []
    stale_days = None
    last = log.get("last_run_utc")
    if last:
        stale_days = (utc_now() - dt.datetime.fromisoformat(last.replace("Z", "+00:00"))).days
    for name, label in (("calendar", "IPO calendar"), ("spy", "S&P 500"), ("quotes", "mega-IPO quotes"),
                        ("ritter", "Ritter watch")):
        s = log.get("sources", {}).get(name)
        if not s:
            parts.append(f'<span class="st na">{label} —</span>')
            continue
        ok = s.get("ok")
        tip = html.escape(s.get("message", ""), quote=True)
        parts.append(f'<span class="st {"ok" if ok else "bad"}" title="{tip}">{label} {"✓" if ok else "✗"}</span>')
    when = f"last data refresh {last.replace('T', ' ').replace('Z', ' UTC')}" if last else "never refreshed"
    stale = f' <span class="st bad">stale: {stale_days} days</span>' if stale_days and stale_days > 4 else ""
    if WARNINGS:
        tip = html.escape(" | ".join(WARNINGS), quote=True)
        stale += f' <span class="st bad" title="{tip}">{len(WARNINGS)} build warning{"s" if len(WARNINGS) > 1 else ""}</span>'
    return f'<span class="muted">{when}</span> ' + " ".join(parts) + stale


def sources_html(manual):
    return " &middot; ".join(f'<a href="{html.escape(s["url"], quote=True)}" target="_blank" rel="noopener">'
                             f'{html.escape(s["title"])}</a>' for s in manual["sources"])


def notes(manual, cal, sp):
    lo, hi = cal["count_ratio_range"]
    mlo, mhi = cal["mult_range"]
    return [
        ("Universe", "Operating-company IPOs as defined by Jay Ritter. Count charts follow Table 3b (US issuers, offer ≥ $5, "
                     "NYSE + Nasdaq main boards; excludes ADRs, units, SPACs, closed-end funds, REITs, LPs, banks/S&Ls). "
                     "Market-cap charts follow Table 1a (aggregate value at first-day close, broader Table 1 sample)."),
        ("Quarterly split", f"Ritter reports annual figures. From {manual['allocation']['calendar_shape_from_year']} the annual "
                            "totals are split using the quarterly shape of the filtered Nasdaq calendar; earlier years use "
                            "seasonal weights with event overrides."),
        ("Years after Ritter's latest", f"Nasdaq IPO calendar priced deals filtered to a Table 3b proxy (NYSE, Nasdaq Global/"
                                        f"Global Select; offer ≥ $5; no SPACs, units, funds, trusts, REITs). Counts × "
                                        f"{cal['count_ratio']:.3f} (pooled Ritter ÷ calendar, {cal['years'][0]}–{cal['years'][-1]}; "
                                        f"yearly range {lo:.2f}–{hi:.2f}). Base market cap = base proceeds × "
                                        f"{cal['mktcap_per_proceeds']:.2f} (pooled; yearly range {mlo:.1f}–{mhi:.1f}, so "
                                        "treat quarterly market caps as approximate)."),
        ("Mega-IPOs", "Separate layer at first-day-close market cap. Listed deals are detected automatically from Nasdaq quote "
                      "data once their ticker trades; expected deals use the midpoint of reported valuation ranges, with no "
                      "first-day pop, and roll forward a quarter at a time if they slip."),
        ("Current quarter", "Quarter-to-date calendar actuals plus the forecast pro-rated for the days remaining."),
        ("S&P 500 denominator", f"To {int(manual['sp500']['spy_from'][:4]) - 1}: approximate year-end totals held within each "
                                f"year. From {manual['sp500']['spy_from'][:4]}: SPY close × anchor (S&P 500 cap "
                                f"${manual['sp500']['anchor']['mktcap_tn']}T on {manual['sp500']['anchor']['date']}); forecast "
                                "quarters use the latest value."),
        ("Exclusions", "; ".join(f"{e['ticker']}: {e['reason']}" for e in manual["exclude_tickers"]) or "none"),
    ]


def write_outputs(manual, rows, cal, megas, detail, sp, agg, cur, as_of):
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "data").mkdir(exist_ok=True)
    (DOCS_DIR / ".nojekyll").write_text("")
    log = load_json(AUTO_DIR / "fetch_log.json", {}) or {}
    upcoming = (load_json(AUTO_DIR / "upcoming.json", {}) or {}).get("rows", [])
    ritter_watch = load_json(AUTO_DIR / "ritter_watch.json", {}) or {}
    sp_date, sp_val = sp.latest()
    generated = utc_now().strftime("%Y-%m-%d %H:%M UTC")
    data = dict(
        as_of=as_of.isoformat(), as_of_label=f"{as_of.day} {as_of.strftime('%b %Y')}", generated_utc=generated,
        current_quarter=qlabel(*cur), window=f"{qlabel(*cur)} – {rows[-1]['q']}",
        sp500_latest=dict(date=sp_date, mktcap_tn=sp_val),
        calibration={k: v for k, v in cal.items() if k != "per_year"}, calibration_by_year=cal["per_year"],
        rows=rows, calendar_quarters=detail,
        annual=[dict(year=r[0], t3b_total=r[1] + r[2], t3b_nyse=r[1], t3b_nasdaq=r[2], t1_count=r[3], t1_proceeds_bn=r[4],
                     t1a_mktcap_bn=r[5], sp500_ye_tn=r[6], mktcap_pct_sp500=pct(r[5], r[6])) for r in manual["ritter"]["annual"]],
        mega=[dict(id=m["id"], name=m["name"], ticker=m.get("ticker"), exchange=m["exchange"], listed=m["listed"],
                   quarter=qlabel(*m["quarter"]) if m["quarter"] else None, expected_quarter=m.get("expected_quarter"),
                   slipped=m["slipped"], mktcap_used_bn=m["mktcap_bn"], current_mktcap_bn=m.get("current_mktcap_bn"),
                   valuation_low_bn=m.get("valuation_low_bn"), valuation_high_bn=m.get("valuation_high_bn"),
                   detail=m.get("detail"), flags="; ".join(m["flags"])) for m in megas],
        upcoming=upcoming, fetch_log=log, ritter_watch=ritter_watch, warnings=WARNINGS,
        notes=[dict(section=a, text=b) for a, b in notes(manual, cal, sp)],
        sources=manual["sources"])

    cols = ["q", "y", "qn", "status", "alloc", "nyse", "nasdaq", "base_count", "mega_n", "total_count", "base_mcap",
            "mega_mcap", "total_mcap", "sp500", "base_ratio", "mega_ratio", "total_ratio", "mega_names", "cal_count",
            "cal_proceeds_bn", "note"]
    heads = ["quarter", "year", "qtr", "status", "allocation", "nyse_count", "nasdaq_count", "base_count", "mega_count",
             "total_count", "base_mktcap_bn", "mega_mktcap_bn", "total_mktcap_bn", "sp500_mktcap_tn", "base_pct_sp500",
             "mega_pct_sp500", "total_pct_sp500", "mega_ipos", "calendar_count_raw", "calendar_proceeds_bn_raw", "note"]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(heads)
    for r in rows:
        w.writerow([STATUS_LONG[r[c]] if c == "status" else ("" if r.get(c) is None else r.get(c)) for c in cols])
    (DOCS_DIR / "data" / "us_ipo_quarterly.csv").write_text("﻿" + buf.getvalue(), encoding="utf-8")
    (DOCS_DIR / "data" / "us_ipo_dashboard_data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                                                   encoding="utf-8")
    tpl = open(TEMPLATE_PATH, encoding="utf-8").read()
    repl = {
        "__DATA__": json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"),
        "__META_CARDS__": cards_html(megas, rows, agg, cur, upcoming),
        "__UPCOMING__": upcoming_html(upcoming),
        "__STATUS__": status_html(log, as_of),
        "__SOURCES__": sources_html(manual),
        "__AS_OF_LABEL__": data["as_of_label"],
        "__WINDOW__": data["window"],
        "__LAST_Q__": rows[-1]["q"],
        "__GENERATED__": generated,
        "__NOTES__": "".join(f"<p><strong>{html.escape(a)}.</strong> {html.escape(b)}</p>" for a, b in notes(manual, cal, sp)),
    }
    for key, val in repl.items():
        tpl, n = re.subn(re.escape(key), lambda _m, v=val: v, tpl)
        if n == 0:
            raise SystemExit(f"template placeholder {key} missing")
    (DOCS_DIR / "index.html").write_text(tpl, encoding="utf-8")
    return data


def main():
    manual = load_json(MANUAL_PATH)
    as_of = dt.date.fromisoformat(os.environ["IPO_AS_OF"]) if os.environ.get("IPO_AS_OF") else utc_now().date()
    rows, cal, megas, detail, sp, agg, cur = build(manual, as_of)
    data = write_outputs(manual, rows, cal, megas, detail, sp, agg, cur, as_of)
    print(f"built docs/index.html · {len(rows)} quarters to {rows[-1]['q']} · as of {as_of} · "
          f"count ratio {cal['count_ratio']:.3f} · mktcap/proceeds {cal['mktcap_per_proceeds']:.2f} · "
          f"S&P {data['sp500_latest']['mktcap_tn']}T ({data['sp500_latest']['date']})")
    for r in rows:
        if r["status"] != "hist" or r["y"] >= rows[-1]["y"] - 2:
            print(f"  {r['q']:8s} {r['status']:11s} n={r['nyse']:>3}+{r['nasdaq']:>3}+{r['mega_n']} "
                  f"mcap={r['base_mcap']:>7.1f}+{r['mega_mcap']:>6.0f} sp={r['sp500']:>6} pct={r['total_ratio']:.3f}")
    if WARNINGS:
        print(f"{len(WARNINGS)} warning(s)")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"build_warnings={json.dumps(WARNINGS)}\n")


if __name__ == "__main__":
    main()
