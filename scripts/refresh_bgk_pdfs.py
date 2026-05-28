"""GH Actions incremental refresh of bgk_auction_results.

Routes both HTML and PDF requests through ScrapingBee (residential
proxy + JS render) because Azure IPs trigger Cloudflare Turnstile on
bgk.pl. Only processes PDFs newer than the latest auction_date already
in DB - typically 0-2 new PDFs per weekly run, keeping credit burn low.

For initial population, run scripts/backfill_bgk_pdfs.py locally instead.
"""

from __future__ import annotations

import sys
from datetime import date
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.bgk_fetch import scrapingbee_get  # noqa: E402
from lib.bgk_pdf import (  # noqa: E402
    KOMUNIKATY_PAGE,
    parse_komunikaty_listing,
    parse_pdf,
)
from lib.supabase import select_all, upsert  # noqa: E402


def _latest_auction_date_in_db() -> date | None:
    rows = select_all(
        "bgk_auction_results",
        "?select=auction_date&order=auction_date.desc&limit=1",
    )
    if not rows:
        return None
    return date.fromisoformat(str(rows[0]["auction_date"])[:10])


def main() -> None:
    print("[1/4] Fetching komunikaty page via ScrapingBee (CF bypass)...",
          flush=True)
    r = scrapingbee_get(KOMUNIKATY_PAGE, render_js=True)
    html = r.text
    print(f"  -> {len(html) / 1024:.0f} KB", flush=True)

    print("[2/4] Diffing against bgk_auction_results...", flush=True)
    entries = parse_komunikaty_listing(html)
    latest = _latest_auction_date_in_db()
    if latest is None:
        # First run / empty table - refuse to backfill everything via
        # ScrapingBee (~80 PDFs * ~25 credits = ~2000 credits, blows the
        # free tier). Operator should run backfill_bgk_pdfs.py locally.
        print(
            "  ! bgk_auction_results is empty. Refusing to backfill all "
            "~80 historical PDFs via ScrapingBee (would exhaust the free "
            "tier). Run scripts/backfill_bgk_pdfs.py locally first.",
            flush=True,
        )
        sys.exit(1)
    new_entries = [e for e in entries if e["auction_date"] > latest]
    new_entries.sort(key=lambda e: e["auction_date"])
    print(
        f"  -> latest in DB: {latest}; {len(new_entries)} new PDF(s) to fetch",
        flush=True,
    )
    if not new_entries:
        print("[3/4] nothing to do - DB is current", flush=True)
        print("[4/4] 0 rows posted", flush=True)
        return

    print("[3/4] Fetching + parsing new PDFs...", flush=True)
    all_rows: list[dict] = []
    for i, e in enumerate(new_entries, 1):
        url = e["pdf_url"]
        ad = e["auction_date"].isoformat()
        print(f"  {i}/{len(new_entries)}  {ad}  {url}", flush=True)
        resp = scrapingbee_get(url, render_js=False)
        rows = parse_pdf(BytesIO(resp.content), url,
                         auction_date=e["auction_date"])
        # No series filter - komunikaty page is empirically all-PLN, so we
        # take every series the PDF reports. As of 2026-05 that's FPC + FWA.
        print(f"     -> {len(rows)} series", flush=True)
        all_rows.extend(rows)

    print(f"[4/4] Upserting {len(all_rows)} rows to bgk_auction_results...",
          flush=True)
    posted = upsert(
        "bgk_auction_results",
        all_rows,
        on_conflict="auction_date,series",
        batch_size=500,
    )
    print(f"  -> {posted} rows posted", flush=True)


if __name__ == "__main__":
    main()
