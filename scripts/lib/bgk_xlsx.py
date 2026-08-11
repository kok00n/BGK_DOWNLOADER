"""Shared helpers for downloading BGK's "Baza obligacji" XLSX.

BGK fronts both the Statystyka index page and the XLSX asset with
Cloudflare Turnstile in interactive mode (confirmed: `cf-mitigated:
challenge`, `challenges.cloudflare.com/.../turnstile/.../normal`).
TLS-fingerprint impersonation (curl_cffi) and stealth headless
Chromium both failed from Azure (GH Actions) IPs - CF requires either
a residential IP or a verified human click.

Fetching goes through lib.bgk_fetch.smart_fetch: a failover chain of
scraper-API providers (scrape.do -> ScrapingAnt -> ScrapingBee) with
Polish residential exit IPs, plus a final local-curl fallback that
works when run from the user's Polish machine. See bgk_fetch.py for
provider setup and credit-cost notes.

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

from .bgk_fetch import smart_fetch


BGK_BASE = "https://www.bgk.pl"
BGK_STATS_PAGE = f"{BGK_BASE}/dla-klienta/relacje-inwestorskie/emisje-obligacji-bgk/statystyka/"

# Captures both href and the DD.MM.YYYY snapshot date so callers can log it.
_XLSX_HREF_RE = re.compile(
    r'href="(?P<href>[^"]*Baza_obligacji_strona_internetowa_'
    r'(?P<date>\d{2}\.\d{2}\.\d{4})\.xlsx)"',
    re.IGNORECASE,
)


def _parse_xlsx_link(html: str) -> tuple[str, date]:
    """Find the newest Baza_obligacji XLSX link in the rendered HTML.

    Returns (absolute_url, snapshot_date_from_filename). Raises if no
    match - signals BGK page structure changed (smart_fetch has already
    rejected challenge pages before we get here).
    """
    matches = list(_XLSX_HREF_RE.finditer(html))
    if not matches:
        raise RuntimeError(
            "Could not find Baza_obligacji_*.xlsx link in fetched HTML. "
            "BGK page structure likely changed. URL: " + BGK_STATS_PAGE
        )
    best = max(matches, key=lambda m: datetime.strptime(m.group("date"), "%d.%m.%Y"))
    href = best.group("href")
    if href.startswith("/"):
        href = BGK_BASE + href
    snapshot = datetime.strptime(best.group("date"), "%d.%m.%Y").date()
    return href, snapshot


def fetch_bgk_xlsx() -> tuple[BytesIO, str, date]:
    """Fetch the latest BGK Baza_obligacji XLSX via the provider chain.

    Returns (xlsx_bytes, source_url, snapshot_date). Two fetches: the
    index page (HTML) to discover the current asset URL, then the
    binary XLSX itself.
    """
    print(f"  -> fetching index page: {BGK_STATS_PAGE}", flush=True)
    html = smart_fetch(BGK_STATS_PAGE, expect="html").decode("utf-8", errors="replace")
    url, snapshot = _parse_xlsx_link(html)
    print(f"  -> found XLSX: {url}", flush=True)
    print(f"  -> snapshot date: {snapshot.isoformat()}", flush=True)

    print("  -> downloading XLSX...", flush=True)
    xlsx_bytes = smart_fetch(url, expect="xlsx")
    print(f"  -> {len(xlsx_bytes) / 1024:.0f} KB downloaded", flush=True)

    return BytesIO(xlsx_bytes), url, snapshot
