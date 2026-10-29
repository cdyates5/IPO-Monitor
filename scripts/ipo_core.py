"""Shared helpers for the IPO dashboard pipeline. Standard library only."""
import datetime as dt
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
AUTO_DIR = DATA_DIR / "auto"
CAL_DIR = AUTO_DIR / "ipo_calendar"
DOCS_DIR = ROOT / "docs"
MANUAL_PATH = DATA_DIR / "manual.json"


# ── io ────────────────────────────────────────────────────────────────────────
def load_json(path, default=None):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path, obj, indent=1):
    """Atomic write; stable key order so git diffs stay readable."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=indent)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.replace(path)


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def utc_stamp():
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


# ── parsing ───────────────────────────────────────────────────────────────────
_NUM = re.compile(r"\d[\d,]*\.?\d*")


def num(text):
    """'$1,234.5' -> 1234.5; '15.00-17.00' -> 17.0 (upper end of a range); '' -> 0.0"""
    if text is None:
        return 0.0
    if isinstance(text, (int, float)):
        return float(text)
    found = _NUM.findall(str(text))
    return max(float(x.replace(",", "")) for x in found) if found else 0.0


def us_date(text):
    """'6/12/2026' or '06/12/2026' -> '2026-06-12'"""
    m, d, y = (int(x) for x in str(text).strip().split("/"))
    return dt.date(y, m, d).isoformat()


# ── quarters ──────────────────────────────────────────────────────────────────
def quarter_of(date):
    if isinstance(date, str):
        date = dt.date.fromisoformat(date)
    return date.year, (date.month - 1) // 3 + 1


def qlabel(y, q):
    return f"Q{q} {y}"


def parse_q(label):
    m = re.fullmatch(r"\s*Q([1-4])\s+(\d{4})\s*", label)
    if not m:
        raise ValueError(f"bad quarter label {label!r} (expected e.g. 'Q4 2026')")
    return int(m.group(2)), int(m.group(1))


def q_add(y, q, n):
    idx = y * 4 + (q - 1) + n
    return idx // 4, idx % 4 + 1


def q_index(y, q):
    return y * 4 + (q - 1)


def q_bounds(y, q):
    start = dt.date(y, 3 * (q - 1) + 1, 1)
    ny, nq = q_add(y, q, 1)
    end = dt.date(ny, 3 * (nq - 1) + 1, 1) - dt.timedelta(days=1)
    return start, end


def month_range(start_ym, end_ym):
    y, m = (int(x) for x in start_ym.split("-"))
    ey, em = (int(x) for x in end_ym.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


# ── calendar filtering (Ritter Table 3b proxy) ────────────────────────────────
def classify(row, cfg):
    """Return (exchange or None, reason). exchange is 'NYSE' or 'Nasdaq' when the deal
    belongs in the Ritter-style universe; otherwise None with the exclusion reason."""
    f = cfg["calendar_filter"]
    ex = f["exchange_map"].get(row.get("exchange") or "")
    if not ex:
        return None, f"exchange '{row.get('exchange')}' not a main board"
    if num(row.get("price")) < f["min_price"]:
        return None, "offer price below minimum"
    name = row.get("name") or ""
    if re.search(f["exclude_name_regex"], name, flags=re.I):
        return None, "SPAC / fund / trust by name"
    ticker = (row.get("ticker") or "").upper()
    if ticker.endswith("U") and abs(num(row.get("price")) - 10.0) < 1e-9:
        return None, "unit offering"
    return ex, ""


def largest_remainder(total, weights):
    """Split integer total across weights so the parts sum exactly to total."""
    s = sum(weights)
    if total <= 0 or s <= 0:
        return [0] * len(weights)
    raw = [total * w / s for w in weights]
    base = [int(x) for x in raw]
    rest = total - sum(base)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - base[i], reverse=True)
    for i in order[:rest]:
        base[i] += 1
    return base


def js_round(x):
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)
