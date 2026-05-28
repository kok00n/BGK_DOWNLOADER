"""Shared helpers for downloading BGK's "Baza obligacji" XLSX.

BGK fronts the Statystyka page with Cloudflare and serves a Turnstile
challenge to non-browser clients - confirmed even with curl_cffi's
Chrome JA3 impersonation. We use headless Chromium via Playwright: the
challenge resolves automatically because a real browser executes the
challenge JS, sets a cf_clearance cookie, and that cookie is reused
for the subsequent XLSX download via context.request.

Single browser launch covers both: index-page render + XLSX fetch.

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

from playwright.sync_api import sync_playwright


BGK_BASE = "https://www.bgk.pl"
BGK_STATS_PAGE = f"{BGK_BASE}/dla-klienta/relacje-inwestorskie/emisje-obligacji-bgk/statystyka/"

# How long to wait for Cloudflare's Turnstile challenge to resolve. The
# challenge JS typically clears in 5-15s on a real browser; 60s is a
# safety margin for slow runs.
_CHALLENGE_TIMEOUT_MS = 60_000

# Captures both href and the DD.MM.YYYY snapshot date so callers can log it.
_XLSX_HREF_RE = re.compile(
    r'href="(?P<href>[^"]*Baza_obligacji_strona_internetowa_'
    r'(?P<date>\d{2}\.\d{2}\.\d{4})\.xlsx)"',
    re.IGNORECASE,
)


def _parse_xlsx_link(html: str) -> tuple[str, date]:
    """Find the newest Baza_obligacji XLSX link in the rendered HTML.

    Returns (absolute_url, snapshot_date_from_filename). Raises if no
    match - signals BGK page structure changed or challenge didn't clear.
    """
    matches = list(_XLSX_HREF_RE.finditer(html))
    if not matches:
        raise RuntimeError(
            "Could not find Baza_obligacji_*.xlsx link in rendered HTML. "
            "Either the BGK page structure changed or the Cloudflare "
            "challenge did not resolve. URL: " + BGK_STATS_PAGE
        )
    best = max(matches, key=lambda m: datetime.strptime(m.group("date"), "%d.%m.%Y"))
    href = best.group("href")
    if href.startswith("/"):
        href = BGK_BASE + href
    snapshot = datetime.strptime(best.group("date"), "%d.%m.%Y").date()
    return href, snapshot


def fetch_bgk_xlsx() -> tuple[BytesIO, str, date]:
    """Fetch the latest BGK Baza_obligacji XLSX through headless Chromium.

    Returns (xlsx_bytes, source_url, snapshot_date). One browser launch
    covers index render + XLSX download so the cf_clearance cookie set
    by the challenge is reused for the asset request.
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                locale="pl-PL",
                timezone_id="Europe/Warsaw",
                # Playwright's default UA contains "HeadlessChrome" which
                # some WAFs flag. Override with the equivalent Chrome UA.
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                extra_http_headers={
                    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
                },
            )
            page = context.new_page()
            print(f"  -> navigating to {BGK_STATS_PAGE}", flush=True)
            page.goto(BGK_STATS_PAGE, wait_until="domcontentloaded",
                      timeout=_CHALLENGE_TIMEOUT_MS)

            # The Cloudflare challenge serves a "Just a moment..." page,
            # then JS-redirects to the real content. Wait until our
            # target link appears in the DOM - that means challenge
            # cleared AND page rendered (single condition for both).
            print("  -> waiting for Cloudflare challenge to clear and "
                  "Baza_obligacji link to appear...", flush=True)
            page.wait_for_function(
                "() => document.querySelector("
                "'a[href*=\"Baza_obligacji_strona_internetowa\"]') !== null",
                timeout=_CHALLENGE_TIMEOUT_MS,
            )

            html = page.content()
            url, snapshot = _parse_xlsx_link(html)
            print(f"  -> found XLSX: {url}", flush=True)
            print(f"  -> snapshot date: {snapshot.isoformat()}", flush=True)

            # Reuse the cleared browser context (with cf_clearance cookie)
            # for the asset request. context.request runs through the same
            # session, so Cloudflare sees a continued browsing session
            # rather than a fresh request.
            print("  -> downloading XLSX through cleared browser session...",
                  flush=True)
            response = context.request.get(
                url,
                headers={"Referer": BGK_STATS_PAGE},
                timeout=_CHALLENGE_TIMEOUT_MS,
            )
            if response.status != 200:
                raise RuntimeError(
                    f"XLSX download failed: HTTP {response.status} "
                    f"{response.status_text}"
                )
            xlsx_bytes = response.body()
            print(f"  -> {len(xlsx_bytes) / 1024:.0f} KB downloaded",
                  flush=True)
            return BytesIO(xlsx_bytes), url, snapshot
        finally:
            browser.close()
