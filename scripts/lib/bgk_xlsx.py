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

# BGK's WAF blocks default Python / generic "compatible; ..." User-Agents
# with 403 (confirmed from GH Actions Ubuntu runner). Use a real browser
# UA + standard Accept-* headers; carry cookies across the index->asset
# request in case BGK sets a challenge cookie on the first hit.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
}

# Captures both href and the DD.MM.YYYY snapshot date so callers can log it.
_XLSX_HREF_RE = re.compile(
    r'href="(?P<href>[^"]*Baza_obligacji_strona_internetowa_'
    r'(?P<date>\d{2}\.\d{2}\.\d{4})\.xlsx)"',
    re.IGNORECASE,
)


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_BROWSER_HEADERS)
    return s


def _diagnose_response(r: requests.Response) -> str:
    """Build a multi-line diagnostic string for non-200 responses.

    BGK's WAF could be blocking via Cloudflare, Akamai, Imperva, custom
    geo filter, or rate limit - the response headers + body snippet
    usually reveal which. Captures enough to choose the right workaround
    without re-running the failing job.
    """
    interesting_headers = [
        "server", "content-type", "content-length",
        "cf-ray", "cf-mitigated", "x-amz-cf-id", "x-akamai-edgescape",
        "x-iinfo", "set-cookie", "x-blocked-by", "x-served-by",
    ]
    lines = [f"  HTTP {r.status_code} {r.reason}"]
    for h in interesting_headers:
        if h in r.headers:
            lines.append(f"  {h}: {r.headers[h][:200]}")
    body = r.text[:800] if r.text else "(empty body)"
    lines.append(f"  body[:800]: {body!r}")
    return "\n".join(lines)


def find_xlsx_url(session: requests.Session | None = None) -> tuple[str, date]:
    """Locate the current Baza_obligacji XLSX. Returns (absolute_url, snapshot_date).

    Raises if no matching link is found - signals BGK page structure changed.
    """
    s = session or _make_session()
    r = s.get(BGK_STATS_PAGE, timeout=30)
    if r.status_code != 200:
        print(f"[bgk_xlsx] GET {BGK_STATS_PAGE} failed:", flush=True)
        print(_diagnose_response(r), flush=True)
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


def download_xlsx(url: str, session: requests.Session | None = None) -> BytesIO:
    """Download the BGK XLSX. Returns BytesIO ready for openpyxl."""
    s = session or _make_session()
    # Set Referer to the Statystyka page so the asset request looks like
    # a click-from-index, which some WAFs require.
    r = s.get(url, headers={"Referer": BGK_STATS_PAGE}, timeout=120)
    r.raise_for_status()
    return BytesIO(r.content)


def make_session() -> requests.Session:
    """Public factory: build a Session preconfigured with browser headers.

    Callers that want index + asset to share cookies (e.g. WAF challenge
    cookie set on the first request) should create one Session and pass
    it to both find_xlsx_url() and download_xlsx().
    """
    return _make_session()
