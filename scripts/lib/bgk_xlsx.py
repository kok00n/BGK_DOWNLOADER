"""Shared helpers for downloading BGK's "Baza obligacji" XLSX.

The XLSX URL embeds a DD.MM.YYYY date in the filename (updated weekly to
biweekly), so we scrape the Statystyka index page on each run to find
the current link.

Index page:
    https://www.bgk.pl/dla-klienta/relacje-inwestorskie/emisje-obligacji-bgk/statystyka/

Asset URL pattern:
    /files/public/Pliki/informacje/Emisje_obligacji_BGK/Statystyka/
        Baza_obligacji_strona_internetowa_DD.MM.YYYY.xlsx
"""

from __future__ import annotations

import re
from datetime import date, datetime
from io import BytesIO

import requests


BGK_BASE = "https://www.bgk.pl"
BGK_STATS_PAGE = f"{BGK_BASE}/dla-klienta/relacje-inwestorskie/emisje-obligacji-bgk/statystyka/"
USER_AGENT = "Mozilla/5.0 (compatible; bgk-dashboard/1.0)"

# Captures both href and the DD.MM.YYYY snapshot date so callers can log it.
_XLSX_HREF_RE = re.compile(
    r'href="(?P<href>[^"]*Baza_obligacji_strona_internetowa_'
    r'(?P<date>\d{2}\.\d{2}\.\d{4})\.xlsx)"',
    re.IGNORECASE,
)


def find_xlsx_url() -> tuple[str, date]:
    """Locate the current Baza_obligacji XLSX. Returns (absolute_url, snapshot_date).

    Raises if no matching link is found - signals BGK page structure changed.
    """
    r = requests.get(BGK_STATS_PAGE, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    html = r.text

    # If multiple snapshots are linked (historical archive), pick the newest by
    # the date embedded in the filename - not by source order.
    matches = list(_XLSX_HREF_RE.finditer(html))
    if not matches:
        raise RuntimeError(
            "Could not find Baza_obligacji_*.xlsx link on BGK Statystyka page. "
            f"Page structure may have changed. URL: {BGK_STATS_PAGE}"
        )
    best = max(matches, key=lambda m: datetime.strptime(m.group("date"), "%d.%m.%Y"))
    href = best.group("href")
    if href.startswith("/"):
        href = BGK_BASE + href
    snapshot = datetime.strptime(best.group("date"), "%d.%m.%Y").date()
    return href, snapshot


def download_xlsx(url: str) -> BytesIO:
    """Download the BGK XLSX. Returns BytesIO ready for openpyxl."""
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=120)
    r.raise_for_status()
    return BytesIO(r.content)
