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

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright


BGK_BASE = "https://www.bgk.pl"
BGK_STATS_PAGE = f"{BGK_BASE}/dla-klienta/relacje-inwestorskie/emisje-obligacji-bgk/statystyka/"

# How long to wait for Cloudflare's Turnstile challenge to resolve. The
# challenge JS typically clears in 5-15s on a real browser; 90s is a
# safety margin for slow runs / harder challenge variants.
_CHALLENGE_TIMEOUT_MS = 90_000

# Inline stealth init script: patches the most reliable headless-detection
# tells (navigator.webdriver, missing chrome.runtime, empty plugins,
# default languages). Equivalent to what playwright-stealth applies for
# these properties; we inline to avoid adding a dependency that has had
# maintenance churn lately.
_STEALTH_INIT_JS = """
() => {
    // navigator.webdriver === true is the canonical "I'm automated" tell.
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Real Chrome exposes window.chrome with runtime; headless lacks it.
    window.chrome = window.chrome || { runtime: {} };

    // Plugins array empty in headless; real Chrome has at least PDF viewer.
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            { name: 'Chrome PDF Plugin' },
            { name: 'Chrome PDF Viewer' },
            { name: 'Native Client' },
        ],
    });

    // Languages list - we already set Accept-Language: pl-PL via headers,
    // but JS-readable navigator.languages must match for consistency.
    Object.defineProperty(navigator, 'languages', {
        get: () => ['pl-PL', 'pl', 'en-US', 'en'],
    });

    // Permissions API: real Chrome resolves 'notifications' as 'default';
    // headless sometimes returns 'denied' which Cloudflare keys on.
    const origQuery = navigator.permissions && navigator.permissions.query;
    if (origQuery) {
        navigator.permissions.query = (params) =>
            params && params.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission || 'default' })
                : origQuery.call(navigator.permissions, params);
    }
}
"""

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
        # Args that reduce headless-detection surface area. --disable-blink-
        # features=AutomationControlled is the canonical one (removes the
        # CDP "automation" banner and the runtime.enable side channel).
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        try:
            context = browser.new_context(
                locale="pl-PL",
                timezone_id="Europe/Warsaw",
                viewport={"width": 1280, "height": 800},
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
            # Apply stealth patches BEFORE any page navigates. add_init_script
            # injects into every page in the context, before site JS runs,
            # so Cloudflare's bot detection sees patched values.
            context.add_init_script(_STEALTH_INIT_JS)

            page = context.new_page()
            print(f"  -> navigating to {BGK_STATS_PAGE}", flush=True)
            page.goto(BGK_STATS_PAGE, wait_until="domcontentloaded",
                      timeout=_CHALLENGE_TIMEOUT_MS)

            print("  -> waiting for Cloudflare challenge to clear and "
                  "Baza_obligacji link to appear (up to 90s)...", flush=True)
            try:
                page.wait_for_function(
                    "() => document.querySelector("
                    "'a[href*=\"Baza_obligacji_strona_internetowa\"]') !== null",
                    timeout=_CHALLENGE_TIMEOUT_MS,
                )
            except PlaywrightTimeout:
                # Dump enough state to diagnose what CF is showing us.
                _dump_page_state(page, "wait_for link timed out")
                raise

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


def _dump_page_state(page, reason: str) -> None:
    """Print enough state at failure time to choose the next workaround.

    Captures URL (did we redirect at all?), title (still "Just a moment..."?),
    visible challenge frame presence, and a body snippet.
    """
    print(f"[bgk_xlsx] page state dump - {reason}", flush=True)
    try:
        print(f"  url: {page.url}", flush=True)
    except Exception as e:
        print(f"  url: <unreadable: {e}>", flush=True)
    try:
        print(f"  title: {page.title()!r}", flush=True)
    except Exception as e:
        print(f"  title: <unreadable: {e}>", flush=True)
    try:
        frames = page.frames
        challenge_frames = [
            f.url for f in frames
            if "challenges.cloudflare.com" in (f.url or "")
        ]
        print(f"  total frames: {len(frames)}", flush=True)
        print(f"  cloudflare challenge frames: {challenge_frames}", flush=True)
    except Exception as e:
        print(f"  frames: <unreadable: {e}>", flush=True)
    try:
        body = page.content()
        print(f"  body[:1500]: {body[:1500]!r}", flush=True)
    except Exception as e:
        print(f"  body: <unreadable: {e}>", flush=True)
