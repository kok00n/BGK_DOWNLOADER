"""GH Actions incremental refresh of bgk_auction_results.

Routes both HTML and PDF requests through the scraper-API provider
chain in lib.bgk_fetch (Polish residential exit IPs; Azure IPs trigger
Cloudflare Turnstile on bgk.pl). Only processes PDFs newer than the
latest auction_date already in DB - typically 0-2 new PDFs per weekly
run, keeping credit burn low.

For initial population, run scripts/backfill_bgk_pdfs.py locally instead.

When GITHUB_OUTPUT is set (i.e. running inside a GH Actions step) we
also write `new_auctions=N` so a downstream step can branch on it
without parsing stdout. N is the count of NEW auction *days* found,
not the count of series rows.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.bgk_fetch import smart_fetch  # noqa: E402
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


def _emit_gh_output(key: str, value) -> None:
    """Append `key=value` to $GITHUB_OUTPUT if running inside Actions.

    Used by the workflow that wraps this script to branch on whether
    any new auction PDFs landed (-> trigger render) or not (-> skip
    so we don't burn LLM tokens on no-op days).
    """
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{key}={value}\n")


def main() -> None:
    print("[1/4] Fetching komunikaty page (CF bypass via provider chain)...",
          flush=True)
    html = smart_fetch(KOMUNIKATY_PAGE, expect="html").decode("utf-8", errors="replace")
    print(f"  -> {len(html) / 1024:.0f} KB", flush=True)

    print("[2/4] Diffing against bgk_auction_results...", flush=True)
    entries = parse_komunikaty_listing(html)
    latest = _latest_auction_date_in_db()
    if latest is None:
        # First run / empty table - refuse to backfill everything via
        # scraper-API credits (~80 PDFs would burn most of a free tier).
        # Operator should run backfill_bgk_pdfs.py locally.
        print(
            "  ! bgk_auction_results is empty. Refusing to backfill all "
            "~80 historical PDFs via scraper-API credits. Run "
            "scripts/backfill_bgk_pdfs.py locally first.",
            flush=True,
        )
        sys.exit(1)
    new_entries = [e for e in entries if e["auction_date"] > latest]
    new_entries.sort(key=lambda e: e["auction_date"])
    print(
        f"  -> latest in DB: {latest}; {len(new_entries)} new PDF(s) to fetch",
        flush=True,
    )
    _emit_gh_output("new_auctions", len(new_entries))
    if not new_entries:
        print("[3/4] nothing to do - DB is current", flush=True)
        print("[4/4] 0 rows posted", flush=True)
        return

    print("[3/4] Fetching + parsing new PDFs...", flush=True)
    all_rows: list[dict] = []
    failed: str | None = None
    for i, e in enumerate(new_entries, 1):
        url = e["pdf_url"]
        ad = e["auction_date"].isoformat()
        print(f"  {i}/{len(new_entries)}  {ad}  {url}", flush=True)
        try:
            pdf_bytes = smart_fetch(url, expect="pdf")
            rows = parse_pdf(BytesIO(pdf_bytes), url,
                             auction_date=e["auction_date"])
        except Exception as ex:
            # Stop at the first failure but still upsert everything parsed
            # so far. new_entries is sorted ascending by auction_date, so
            # the failed PDF (and everything after it) stays newer than
            # the latest date in DB and gets retried on the next run.
            failed = f"{ad} {url}: {type(ex).__name__}: {ex}"
            print(f"     ! FAILED, aborting after partial upsert: {ex}",
                  flush=True)
            break
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
    if failed:
        print(f"  ! run failed on: {failed}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
