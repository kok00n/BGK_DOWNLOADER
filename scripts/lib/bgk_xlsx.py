"""Shared helpers for downloading BGK's "Baza obligacji" XLSX.

BGK fronts both the Statystyka index page and the XLSX asset with
Cloudflare Turnstile in interactive mode (confirmed: `cf-mitigated:
challenge`, `challenges.cloudflare.com/.../turnstile/.../normal`).
TLS-fingerprint impersonation (curl_cffi) and stealth headless
Chromium both failed from Azure (GH Actions) IPs - CF requires either
a residential IP or a verified human click.

We route both requests through ScrapingBee's API with `premium_proxy`
(residential IP pool) and `country_code=pl` (Polish exit IP, which BGK
treats as a normal visitor). ScrapingBee handles JS render + CF
challenge server-side; we get the post-challenge HTML / binary back.

Cost note: ~25 credits per request, ~50 credits per refresh. Free tier
is 1000 credits/month -> we use ~400/mo at the planned 2x/week cadence,
well under the limit. If we need to economise, the asset download can
be tried without premium_proxy first.

Index page:
    https://www.bgk.pl/dla-klienta/relacje-inwestorskie/emisje-obligacji-bgk/statystyka/

Asset URL pattern:
    /files/public/Pliki/informacje/Emisje_obligacji_BGK/Statystyka/
        Baza_obligacji_strona_internetowa_DD.MM.YYYY.xlsx
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from io import BytesIO

import requests


BGK_BASE = "https://www.bgk.pl"
BGK_STATS_PAGE = f"{BGK_BASE}/dla-klienta/relacje-inwestorskie/emisje-obligacji-bgk/statystyka/"

_SCRAPINGBEE_API = "https://app.scrapingbee.com/api/v1/"

# Captures both href and the DD.MM.YYYY snapshot date so callers can log it.
_XLSX_HREF_RE = re.compile(
    r'href="(?P<href>[^"]*Baza_obligacji_strona_internetowa_'
    r'(?P<date>\d{2}\.\d{2}\.\d{4})\.xlsx)"',
    re.IGNORECASE,
)


def _scrapingbee_get(target_url: str, *, render_js: bool, timeout: int = 120) -> requests.Response:
    """Fetch a URL through ScrapingBee with premium proxy + Polish exit IP.

    render_js=True asks ScrapingBee to execute page JS (needed for CF
    challenge resolution on the HTML index page). For binary assets
    pass render_js=False to save credits and avoid rendering errors.
    """
    api_key = os.environ.get("SCRAPINGBEE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing required env var: SCRAPINGBEE_API_KEY")
    params = {
        "api_key": api_key,
        "url": target_url,
        # Residential proxy pool - CF treats these as normal users.
        "premium_proxy": "true",
        # Polish exit IP - matches Accept-Language: pl-PL we want anyway.
        "country_code": "pl",
        "render_js": "true" if render_js else "false",
    }
    r = requests.get(_SCRAPINGBEE_API, params=params, timeout=timeout)
    if r.status_code != 200:
        # ScrapingBee surfaces target errors and its own errors via status code +
        # JSON body (e.g. quota exceeded, invalid key, target 5xx, CF still blocked).
        body = r.text[:800] if r.text else "(empty)"
        # Surface useful headers (Spb-* are ScrapingBee's diagnostics)
        sb_headers = {k: v for k, v in r.headers.items() if k.lower().startswith("spb-")}
        raise RuntimeError(
            f"ScrapingBee HTTP {r.status_code} fetching {target_url!r}\n"
            f"  spb headers: {sb_headers}\n"
            f"  body[:800]: {body}"
        )
    return r


def _parse_xlsx_link(html: str) -> tuple[str, date]:
    """Find the newest Baza_obligacji XLSX link in the rendered HTML.

    Returns (absolute_url, snapshot_date_from_filename). Raises if no
    match - signals BGK page structure changed or CF still serving challenge.
    """
    matches = list(_XLSX_HREF_RE.finditer(html))
    if not matches:
        raise RuntimeError(
            "Could not find Baza_obligacji_*.xlsx link in rendered HTML. "
            "Either the BGK page structure changed or ScrapingBee did "
            "not resolve the Cloudflare challenge. URL: " + BGK_STATS_PAGE
        )
    best = max(matches, key=lambda m: datetime.strptime(m.group("date"), "%d.%m.%Y"))
    href = best.group("href")
    if href.startswith("/"):
        href = BGK_BASE + href
    snapshot = datetime.strptime(best.group("date"), "%d.%m.%Y").date()
    return href, snapshot


def fetch_bgk_xlsx() -> tuple[BytesIO, str, date]:
    """Fetch the latest BGK Baza_obligacji XLSX via ScrapingBee.

    Returns (xlsx_bytes, source_url, snapshot_date). Two API calls:
    one for the index page (with render_js, so CF challenge resolves
    server-side) and one for the binary XLSX (no render_js).
    """
    print(f"  -> fetching index page via ScrapingBee: {BGK_STATS_PAGE}",
          flush=True)
    r_index = _scrapingbee_get(BGK_STATS_PAGE, render_js=True)
    url, snapshot = _parse_xlsx_link(r_index.text)
    print(f"  -> found XLSX: {url}", flush=True)
    print(f"  -> snapshot date: {snapshot.isoformat()}", flush=True)

    print("  -> downloading XLSX via ScrapingBee (no JS render)...",
          flush=True)
    r_xlsx = _scrapingbee_get(url, render_js=False)
    xlsx_bytes = r_xlsx.content
    print(f"  -> {len(xlsx_bytes) / 1024:.0f} KB downloaded", flush=True)

    return BytesIO(xlsx_bytes), url, snapshot
