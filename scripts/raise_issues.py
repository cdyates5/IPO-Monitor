#!/usr/bin/env python3
"""
raise_issues.py — open, update or close GitHub issues so problems reach you by GitHub notification
instead of silently going stale. Called by the workflow after fetch + build; uses the `gh` CLI.

Issues it manages (one open issue per kind, never duplicated):
  * "IPO dashboard: data fetch failing"   — opened when a source fails, closed when all recover
  * "IPO dashboard: build warnings"       — slipped mega-IPOs, missing forecast quarters, ticker conflicts
  * "IPO dashboard: Ritter statistics updated" — Ritter's PDF changed; review data/manual.json

Environment: FAILED (comma list), RITTER ('true'), WARN (JSON list), GITHUB_REPOSITORY, GITHUB_SERVER_URL, GITHUB_RUN_ID
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ipo_core import AUTO_DIR, load_json  # noqa: E402

RUN_URL = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}"
           f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def find_open(title):
    out = gh("issue", "list", "--state", "open", "--search", f'"{title}" in:title', "--json", "number,title")
    return next((i["number"] for i in json.loads(out or "[]") if i["title"] == title), None)


def ensure_open(title, body, comment_if_open=False):
    num = find_open(title)
    if num is None:
        gh("issue", "create", "--title", title, "--body", body)
        print(f"opened: {title}")
    elif comment_if_open:
        gh("issue", "comment", str(num), "--body", body)
        print(f"commented on #{num}: {title}")


def ensure_closed(title, comment):
    num = find_open(title)
    if num is not None:
        gh("issue", "close", str(num), "--comment", comment)
        print(f"closed #{num}: {title}")


def main():
    failed = [s for s in os.environ.get("FAILED", "").split(",") if s]
    warnings = json.loads(os.environ.get("WARN") or "[]")
    ritter_changed = os.environ.get("RITTER") == "true"

    t_fetch = "IPO dashboard: data fetch failing"
    if failed:
        log = load_json(AUTO_DIR / "fetch_log.json", {}).get("sources", {})
        lines = [f"- **{s}**: {log.get(s, {}).get('message', '?')} (last success: {log.get(s, {}).get('last_success_utc')})"
                 for s in failed]
        ensure_open(t_fetch, "The scheduled refresh could not reach these sources. The dashboard was still rebuilt "
                             "from cached data, so it is showing the last good values.\n\n" + "\n".join(lines) +
                    f"\n\nRun: {RUN_URL}\n\nThis issue closes itself after the next fully successful run. "
                    "See SETUP.md → Troubleshooting.")
    else:
        ensure_closed(t_fetch, f"All sources fetched successfully again. Run: {RUN_URL}")

    t_warn = "IPO dashboard: build warnings"
    if warnings:
        ensure_open(t_warn, "The build needs a judgement call in `data/manual.json`:\n\n" +
                    "\n".join(f"- {w}" for w in warnings) +
                    f"\n\nRun: {RUN_URL}\n\nEdit `data/manual.json` (SETUP.md → Routine maintenance); "
                    "this issue closes itself once a build has no warnings.")
    else:
        ensure_closed(t_warn, f"No build warnings. Run: {RUN_URL}")

    if ritter_changed:
        ensure_open("IPO dashboard: Ritter statistics updated",
                    "Jay Ritter's IPO Statistics PDF has changed since the last check "
                    "(https://site.warrington.ufl.edu/ritter/files/IPO-Statistics.pdf).\n\n"
                    "1. Open the PDF and note its date.\n"
                    "2. If a new year appears in Tables 1, 1a and 3b, append it to `ritter.annual` in `data/manual.json` "
                    "(and check the last two years for revisions).\n"
                    "3. Update `ritter.edition`, commit — the workflow rebuilds and recalibrates automatically.\n\n"
                    f"Run: {RUN_URL}", comment_if_open=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as e:
        print(f"gh failed: {e.stderr}", file=sys.stderr)
        sys.exit(0)   # never fail the workflow over notification plumbing
