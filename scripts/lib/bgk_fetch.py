"""Shared HTTP fetchers for BGK content.

Two backends:

* `curl_get` - local subprocess wrapping `curl --ssl-no-revoke`. Used by
  the one-shot backfill that runs from the user's Polish-IP machine,
  where bgk.pl doesn't trigger Cloudflare challenges. The --ssl-no-revoke
  flag works around a Windows-specific CRL fetch failure that doesn't
  affect cert chain validity (see memory: local-ssl-revocation-workaround).

* `scrapingbee_get` - residential-proxy fetch through ScrapingBee.
  Used by the GH Actions incremental workflow because Azure datacentre
  IPs get Turnstile-challenged by bgk.pl (see memory:
  bgk-cloudflare-scrapingbee).
"""

from __future__ import annotations

import os
import subprocess
import requests


_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_SCRAPINGBEE_API = "https://app.scrapingbee.com/api/v1/"


def curl_get(url: str, timeout: int = 60) -> bytes:
    """Fetch URL via curl subprocess. Returns response body as bytes."""
    r = subprocess.run(
        [
            "curl",
            "--ssl-no-revoke",
            "-sSL",            # silent, show errors, follow redirects
            "--fail",          # error on HTTP 4xx/5xx
            "-A", _BROWSER_UA,
            url,
        ],
        capture_output=True,
        timeout=timeout,
        check=True,
    )
    return r.stdout


def scrapingbee_get(target_url: str, *, render_js: bool, timeout: int = 120) -> requests.Response:
    """Fetch a URL through ScrapingBee with premium proxy + Polish exit IP.

    render_js=True for the CF-protected HTML index page. render_js=False
    for binary PDF assets (cheaper credits, avoids render errors).
    """
    api_key = os.environ.get("SCRAPINGBEE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing required env var: SCRAPINGBEE_API_KEY")
    params = {
        "api_key": api_key,
        "url": target_url,
        "premium_proxy": "true",
        "country_code": "pl",
        "render_js": "true" if render_js else "false",
    }
    r = requests.get(_SCRAPINGBEE_API, params=params, timeout=timeout)
    if r.status_code != 200:
        body = r.text[:800] if r.text else "(empty)"
        sb_headers = {k: v for k, v in r.headers.items() if k.lower().startswith("spb-")}
        raise RuntimeError(
            f"ScrapingBee HTTP {r.status_code} fetching {target_url!r}\n"
            f"  spb headers: {sb_headers}\n"
            f"  body[:800]: {body}"
        )
    return r
