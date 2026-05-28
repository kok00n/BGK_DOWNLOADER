"""Parse upcoming-auction announcements from BGK komunikaty page.

BGK posts auction announcements as PDFs with `Informacja_o_przetargu` in
the URL (we already see them on the same komunikaty page that hosts the
post-auction `Komunikat_o_wynikach` PDFs - the existing parser in
bgk_pdf.parse_komunikaty_listing skips them).

URL pattern (empirical, may shift):
    .../Informacja_o_przetargu_<SERIES_LIST>_<DD.MM.YYYY>.pdf
    .../Informacja_o_przetargu_FPC0631_FPC1031_04.06.2026.pdf

Strategy:
1. Pull every announcement <a href="*Informacja_o_przetargu*.pdf">.
2. Extract auction_date from filename via DD.MM.YYYY regex.
3. Extract series codes from filename (FPC/KFD/FP/FWSZ/FWA prefix
   + 4-digit YYMM suffix).
4. Filter to future-only (auction_date >= today) before returning.

Returns list of {auction_date: date, series: [str, ...], source_url: str}.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

# Date inside filename: 04.06.2026 anywhere in the URL string.
_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")

# BGK series codes - prefix + 4 digit YYMM tenor code.
# FPC, KFD, FP, FWSZ, FWA observed; allow trailing letter variants like
# FP0136 / FPC0631 / FWSZ0824. Add /A /B /C variants if BGK starts
# tapping with sub-tranches.
_SERIES_RE = re.compile(
    r"\b(FPC|KFD|FWSZ|FWA|FP)(\d{4})\b",
    re.IGNORECASE,
)


def _parse_url(url: str) -> tuple[date, list[str]] | None:
    """Return (auction_date, series_list) parsed from a komunikaty PDF URL.

    Returns None when no date or no series found - caller treats as
    unparseable and skips. Series list is dedup'd and uppercased.
    """
    m = _DATE_RE.search(url)
    if not m:
        return None
    try:
        ad = datetime.strptime(
            f"{m.group(1)}.{m.group(2)}.{m.group(3)}", "%d.%m.%Y"
        ).date()
    except ValueError:
        return None

    series_set: set[str] = set()
    for sm in _SERIES_RE.finditer(url):
        series_set.add(f"{sm.group(1).upper()}{sm.group(2)}")
    if not series_set:
        return None
    return ad, sorted(series_set)


def parse_announcements(html: str, *, today: date | None = None) -> list[dict]:
    """Find upcoming-auction announcement PDFs on BGK komunikaty page.

    Mirrors bgk_pdf.parse_komunikaty_listing but inverts the filter -
    we KEEP `Informacja_o_przetargu` URLs and SKIP `Komunikat_o_wynikach`.
    Past-dated announcements are filtered out (today's auction is still
    "upcoming" until end-of-day; we use >= today).
    """
    from bs4 import BeautifulSoup

    today = today or datetime.now(timezone.utc).date()
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen_dates: set[date] = set()
    for a in soup.find_all("a", href=re.compile(r"\.pdf$", re.IGNORECASE)):
        href = a.get("href", "")
        if not href:
            continue
        # Only announcements - skip post-auction result PDFs.
        if "Informacja_o_przetargu" not in href:
            continue
        url = "https://www.bgk.pl" + href if href.startswith("/") else href

        parsed = _parse_url(url)
        if parsed is None:
            continue
        auction_date, series = parsed
        # Filter past-dated announcements - we only want forward calendar.
        if auction_date < today:
            continue
        # Dedup on (date, series_set) - BGK occasionally lists the same
        # announcement twice (once in cards, once in archive table).
        key = auction_date
        if key in seen_dates:
            # Merge series lists - if a second URL adds new series, union.
            for existing in out:
                if existing["auction_date"] == auction_date:
                    existing["series"] = sorted(set(existing["series"]) | set(series))
                    break
            continue
        seen_dates.add(key)
        out.append({
            "auction_date": auction_date,
            "series": series,
            "source_url": url,
        })
    return sorted(out, key=lambda r: r["auction_date"])
