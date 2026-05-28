"""Refresh bgk_auction_calendar from BGK komunikaty page announcements.

Weekly cron. Scrapes the same komunikaty page that refresh_bgk_pdfs.py
uses, but filters for `Informacja_o_przetargu` PDFs (forward-looking
announcements) instead of `Komunikat_o_wynikach` (past results).
Upserts {auction_date, series, source_url} - downstream the
auction_day_pipeline.yml uses these dates to decide whether to run
the full refresh + render pipeline on any given day.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.bgk_calendar import parse_announcements  # noqa: E402
from lib.bgk_fetch import scrapingbee_get  # noqa: E402
from lib.bgk_pdf import KOMUNIKATY_PAGE  # noqa: E402
from lib.supabase import upsert  # noqa: E402


def _diagnostic_listing(html: str) -> None:
    """Print every PDF href on the page grouped by leading filename token.

    Helps debug parsing - BGK occasionally renames filename patterns,
    and we need to see what URLs are live without scraping the page
    locally (Cloudflare blocks non-PL IPs).
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    hrefs: list[str] = []
    for a in soup.find_all("a", href=re.compile(r"\.pdf$", re.IGNORECASE)):
        href = a.get("href") or ""
        if href:
            hrefs.append(href)

    print(f"  (diag) total PDF hrefs on page: {len(hrefs)}", flush=True)
    # Bucket by filename leading token (split on first underscore after path).
    buckets: Counter = Counter()
    for h in hrefs:
        fn = h.rsplit("/", 1)[-1]
        # Token = first few underscore-separated words (drop date/series).
        token = "_".join(fn.split("_")[:3])
        buckets[token] += 1
    print("  (diag) URL prefix buckets (top 20):", flush=True)
    for token, cnt in buckets.most_common(20):
        print(f"     {cnt:3d}  {token}...", flush=True)
    # Sample 5 full URLs per bucket so we see exact patterns.
    print("  (diag) sample URLs per bucket:", flush=True)
    shown = set()
    for h in hrefs:
        fn = h.rsplit("/", 1)[-1]
        token = "_".join(fn.split("_")[:3])
        if token in shown:
            continue
        shown.add(token)
        print(f"     [{token}] {h}", flush=True)


def main() -> None:
    print("[1/3] Fetching komunikaty page via ScrapingBee (CF bypass)...",
          flush=True)
    r = scrapingbee_get(KOMUNIKATY_PAGE, render_js=True)
    html = r.text
    print(f"  -> {len(html) / 1024:.0f} KB", flush=True)

    # Diagnostic listing - print every PDF URL so we can fix parser
    # if BGK has changed the filename convention.
    _diagnostic_listing(html)

    print("[2/3] Parsing future-dated announcements...", flush=True)
    entries = parse_announcements(html)
    if not entries:
        print("  -> 0 upcoming auctions found", flush=True)
        print("[3/3] nothing to upsert", flush=True)
        return
    print(f"  -> {len(entries)} upcoming auction date(s):", flush=True)
    for e in entries:
        print(f"     {e['auction_date'].isoformat()}  series={e['series']}",
              flush=True)

    print(f"[3/3] Upserting {len(entries)} rows to bgk_auction_calendar...",
          flush=True)
    rows = [{
        "auction_date": e["auction_date"].isoformat(),
        "series":       e["series"],
        "source_url":   e["source_url"],
    } for e in entries]
    posted = upsert(
        "bgk_auction_calendar",
        rows,
        on_conflict="auction_date",
        batch_size=100,
    )
    print(f"  -> {posted} rows posted", flush=True)


if __name__ == "__main__":
    main()
