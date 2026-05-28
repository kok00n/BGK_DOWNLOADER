"""One-shot backfill of bgk_auction_results from all komunikaty PDFs.

Designed for local execution on the user's Polish-IP machine (bgk.pl
doesn't Cloudflare-challenge Polish IPs). Uses curl subprocess for HTTP
to side-step the Windows local CRL-fetch failure that breaks Python's
TLS chain validation for bgk.pl certs.

Filter: keeps only series starting with 'FPC' (the FPC PLN scope).

Usage:
    python scripts/backfill_bgk_pdfs.py
    python scripts/backfill_bgk_pdfs.py --since 2024-01-01
    python scripts/backfill_bgk_pdfs.py --dry-run --limit 3
    python scripts/backfill_bgk_pdfs.py --series-prefix FPC,FWA  # custom filter

Requires SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY in env (or a .env file
loaded by the user's shell). No SCRAPINGBEE_API_KEY needed here.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.bgk_fetch import curl_get  # noqa: E402
from lib.bgk_pdf import (  # noqa: E402
    KOMUNIKATY_PAGE,
    parse_komunikaty_listing,
    parse_pdf,
)
from lib.supabase import upsert  # noqa: E402


def _series_matches(series: str, prefixes: tuple[str, ...]) -> bool:
    return any(series.startswith(p) for p in prefixes)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", help="ISO date - skip PDFs with auction_date earlier than this")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and print but don't upsert")
    ap.add_argument("--limit", type=int,
                    help="cap number of PDFs (handy for smoke-testing)")
    ap.add_argument("--series-prefix", default="FPC",
                    help="comma-separated series prefixes to keep (default: FPC)")
    args = ap.parse_args()

    since_date = datetime.fromisoformat(args.since).date() if args.since else None
    prefixes = tuple(p.strip() for p in args.series_prefix.split(",") if p.strip())

    print(f"[1/4] Fetching komunikaty page via curl...", flush=True)
    html = curl_get(KOMUNIKATY_PAGE).decode("utf-8")
    print(f"  -> {len(html) / 1024:.0f} KB", flush=True)

    print(f"[2/4] Parsing listing...", flush=True)
    entries = parse_komunikaty_listing(html)
    if since_date:
        before = len(entries)
        entries = [e for e in entries if e["auction_date"] >= since_date]
        print(f"  -> --since {since_date}: {before} -> {len(entries)}", flush=True)
    entries.sort(key=lambda e: e["auction_date"])
    if args.limit:
        entries = entries[: args.limit]
    if not entries:
        print("  -> no PDFs to process", flush=True)
        return
    print(
        f"  -> {len(entries)} PDFs queued "
        f"(oldest {entries[0]['auction_date']}, newest {entries[-1]['auction_date']})",
        flush=True,
    )

    print(f"[3/4] Downloading + parsing PDFs (keeping series prefix {prefixes})...", flush=True)
    all_rows: list[dict] = []
    failures: list[str] = []
    for i, e in enumerate(entries, 1):
        url = e["pdf_url"]
        date_str = e["auction_date"].isoformat()
        try:
            pdf_bytes = BytesIO(curl_get(url))
            rows = parse_pdf(pdf_bytes, url, auction_date=e["auction_date"])
            rows = [r for r in rows if _series_matches(r["series"], prefixes)]
            print(f"  {i:>3}/{len(entries)}  {date_str}  -> {len(rows)} matching series", flush=True)
            all_rows.extend(rows)
        except Exception as ex:
            failures.append(f"{date_str} {url}: {type(ex).__name__}: {ex}")
            print(f"  {i:>3}/{len(entries)}  {date_str}  ! FAILED: {ex}", flush=True)

    print(f"[4/4] {len(all_rows)} total rows; {len(failures)} PDFs failed", flush=True)
    if failures:
        print("  Failures:", flush=True)
        for f in failures:
            print(f"    - {f}", flush=True)

    if args.dry_run:
        print("  -> --dry-run, skipping upsert", flush=True)
        return

    posted = upsert(
        "bgk_auction_results",
        all_rows,
        on_conflict="auction_date,series",
        batch_size=500,
    )
    print(f"  -> {posted} rows posted to bgk_auction_results", flush=True)


if __name__ == "__main__":
    main()
