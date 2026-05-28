"""Shared helpers for downloading BGK's "Baza obligacji" XLSX.

The XLSX URL embeds a DD.MM.YYYY date in the filename (updated weekly to
biweekly), so we scrape the Statystyka index page on each run to find
the current link.

Index page:
    https://www.bgk.pl/dla-klienta/relacje-inwestorskie/emisje-obligacji-bgk/statystyka/

Asset URL pattern:
    /files/public/Pliki/informacje/Emisje_obligacji_BGK/Statystyka/
        Baza_obligacji_strona_internetowa_DD.MM.YYYY.xlsx

Cloudflare bypass:
    BGK fronts the site with Cloudflare and serves a Turnstile challenge
    (HTTP 403, `cf-mitigated: challenge`, "Just a moment..." body) to
    clients with a Python JA3 TLS fingerprint - even with a full Chrome
    User-Agent. We use `curl_cffi`, which mimics a real Chrome TLS
    fingerprint at the libcurl level; Cloudflare then lets the request
    through without challenge.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from io import BytesIO

from curl_cffi import requests as cffi_requests


BGK_BASE = "https://www.bgk.pl"
BGK_STATS_PAGE = f"{BGK_BASE}/dla-klienta/relacje-inwestorskie/emisje-obligacji-bgk/statystyka/"

# curl_cffi impersonation target. "chrome124" = newest Chrome JA3 shipped
# in curl_cffi 0.7.x (the 0.7.4 we pin). If we bump curl_cffi to 0.8+, we
# can move to chrome131/133 - which Cloudflare currently treats as "more
# trustworthy" than older Chromes since it's still the dominant version.
# Pinned so the TLS fingerprint stays stable across runs (Cloudflare keys
# on the exact JA3, silent bumps could re-trigger the challenge).
_IMPERSONATE = "chrome124"

# Captures both href and the DD.MM.YYYY snapshot date so callers can log it.
_XLSX_HREF_RE = re.compile(
    r'href="(?P<href>[^"]*Baza_obligacji_strona_internetowa_'
    r'(?P<date>\d{2}\.\d{2}\.\d{4})\.xlsx)"',
    re.IGNORECASE,
)


def _make_session() -> cffi_requests.Session:
    # impersonate= sets a realistic UA + Accept + Accept-Encoding matching
    # the chosen Chrome build. Override Accept-Language so the request
    # looks like a Polish user (BGK's WAF may also score on geo cues).
    s = cffi_requests.Session(impersonate=_IMPERSONATE)
    s.headers.update({"Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7"})
    return s


def _diagnose_response(r) -> str:
    """Build a multi-line diagnostic string for non-200 responses.

    Captures enough to identify which WAF / block layer is responding
    (Cloudflare, Akamai, Imperva, ...) without a second debug run.
    """
    interesting_headers = [
        "server", "content-type", "content-length",
        "cf-ray", "cf-mitigated", "x-amz-cf-id", "x-akamai-edgescape",
        "x-iinfo", "set-cookie", "x-blocked-by", "x-served-by",
    ]
    reason = getattr(r, "reason", "")
    lines = [f"  HTTP {r.status_code} {reason}".rstrip()]
    for h in interesting_headers:
        v = r.headers.get(h)
        if v:
            lines.append(f"  {h}: {str(v)[:200]}")
    body = r.text[:800] if r.text else "(empty body)"
    lines.append(f"  body[:800]: {body!r}")
    return "\n".join(lines)


def find_xlsx_url(session: cffi_requests.Session | None = None) -> tuple[str, date]:
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


def download_xlsx(url: str, session: cffi_requests.Session | None = None) -> BytesIO:
    """Download the BGK XLSX. Returns BytesIO ready for openpyxl."""
    s = session or _make_session()
    # Set Referer to the Statystyka page so the asset request looks like
    # a click-from-index, which some WAFs require.
    r = s.get(url, headers={"Referer": BGK_STATS_PAGE}, timeout=120)
    r.raise_for_status()
    return BytesIO(r.content)


def make_session() -> cffi_requests.Session:
    """Public factory: build a Session preconfigured with the impersonated
    Chrome TLS fingerprint + browser headers.

    Callers that want index + asset to share cookies (e.g. WAF challenge
    cookie set on the first request) should create one Session and pass
    it to both find_xlsx_url() and download_xlsx().
    """
    return _make_session()
