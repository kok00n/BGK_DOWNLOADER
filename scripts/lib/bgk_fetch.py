"""Shared HTTP fetchers for BGK content.

bgk.pl fronts its pages and assets with Cloudflare Turnstile that
challenges datacentre IPs (GH Actions / Azure). Residential Polish
exit IPs pass. We route requests through a *chain* of scraper-API
providers with free tiers, falling through on quota exhaustion or an
unresolved challenge, so a single dead account no longer kills the
pipeline (as happened with ScrapingBee in July 2026 - its "free tier"
turned out to be a one-time trial, not monthly).

Providers (tried in order, each only if its API key env var is set):

* ``scrapedo``    - scrape.do, env SCRAPEDO_TOKEN. Free 1000
  credits/month, renewable; residential (super=true) + geoCode=pl +
  render all included. Cost: ~10 credits no-JS, ~25 with render.
* ``scrapingant`` - ScrapingAnt, env SCRAPINGANT_API_KEY. Free 10000
  credits/month, renewable. Cost: residential 50 no-JS, 125 with
  browser - big headroom fallback.
* ``scrapingbee`` - ScrapingBee, env SCRAPINGBEE_API_KEY. Kept as a
  legacy backend (trial credits exhausted; only useful if topped up).
* ``curl``        - local subprocess `curl --ssl-no-revoke`, always
  appended last. Works from the user's Polish residential IP (no
  challenge there); on GH Actions it returns the challenge page,
  which content validation rejects. --ssl-no-revoke works around a
  Windows-specific CRL fetch failure that doesn't affect cert chain
  validity.

Per provider we try the cheap no-JS request first and escalate to JS
rendering only when the response looks like a CF challenge. Every
response is validated against the expected content type (PDF/XLSX
magic bytes, challenge markers in HTML) before being accepted -
a "successful" HTTP 200 that still carries the Turnstile page must
fall through to the next attempt, not poison the parser.

Override the chain with BGK_FETCH_PROVIDERS (comma-separated, e.g.
``BGK_FETCH_PROVIDERS=curl`` for local runs that should never spend
API credits).
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

_SCRAPEDO_API = "https://api.scrape.do/"
_SCRAPINGANT_API = "https://api.scrapingant.com/v2/general"
_SCRAPINGBEE_API = "https://app.scrapingbee.com/api/v1/"

# Provider-level statuses that mean "this account is dead for now"
# (bad key / payment required / quota) - no point escalating to a JS
# render on the same provider, skip straight to the next one.
_ACCOUNT_DEAD_STATUSES = {401, 402, 403, 429}


class FetchError(RuntimeError):
    """One failed fetch attempt. `status` is the provider's HTTP status."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


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


def _api_get(api_url: str, params: dict, provider: str, timeout: int) -> bytes:
    """Shared GET + error wrapping for the scraper-API providers."""
    try:
        r = requests.get(api_url, params=params, timeout=timeout)
    except requests.RequestException as e:
        raise FetchError(f"{provider}: {type(e).__name__}: {e}") from e
    if r.status_code != 200:
        body = r.text[:500] if r.text else "(empty)"
        raise FetchError(
            f"{provider}: HTTP {r.status_code}, body[:500]: {body}",
            status=r.status_code,
        )
    return r.content


def _scrapedo_get(url: str, *, render_js: bool, timeout: int = 120) -> bytes:
    return _api_get(
        _SCRAPEDO_API,
        {
            "token": os.environ["SCRAPEDO_TOKEN"],
            "url": url,
            "super": "true",       # residential/mobile pool
            "geoCode": "pl",       # Polish exit IP - CF treats as normal visitor
            "render": "true" if render_js else "false",
        },
        provider="scrape.do",
        timeout=timeout,
    )


def _scrapingant_get(url: str, *, render_js: bool, timeout: int = 120) -> bytes:
    return _api_get(
        _SCRAPINGANT_API,
        {
            "x-api-key": os.environ["SCRAPINGANT_API_KEY"],
            "url": url,
            "proxy_type": "residential",
            "proxy_country": "PL",
            # ScrapingAnt renders by default - disable explicitly for the
            # cheap tier (50 vs 125 credits per residential request).
            "browser": "true" if render_js else "false",
        },
        provider="scrapingant",
        timeout=timeout,
    )


def _scrapingbee_get(url: str, *, render_js: bool, timeout: int = 120) -> bytes:
    return _api_get(
        _SCRAPINGBEE_API,
        {
            "api_key": os.environ["SCRAPINGBEE_API_KEY"],
            "url": url,
            "premium_proxy": "true",
            "country_code": "pl",
            "render_js": "true" if render_js else "false",
        },
        provider="scrapingbee",
        timeout=timeout,
    )


_PROVIDERS: dict[str, tuple] = {
    # name -> (fetch_fn, required_env_var)
    "scrapedo":    (_scrapedo_get, "SCRAPEDO_TOKEN"),
    "scrapingant": (_scrapingant_get, "SCRAPINGANT_API_KEY"),
    "scrapingbee": (_scrapingbee_get, "SCRAPINGBEE_API_KEY"),
}


_CHALLENGE_MARKERS = (
    "challenges.cloudflare.com",
    "cf-turnstile",
    "_cf_chl_opt",
    "cf-chl",
    "Just a moment",
)


def _validate(content: bytes, expect: str) -> str | None:
    """Return None if `content` looks like the real thing, else a reason.

    expect: 'html' | 'pdf' | 'xlsx'
    """
    if not content:
        return "empty response"
    if expect == "pdf":
        if b"%PDF" in content[:1024]:
            return None
        return f"no %PDF magic (first bytes: {content[:16]!r})"
    if expect == "xlsx":
        if content[:4] == b"PK\x03\x04":
            return None
        return f"no XLSX/zip magic (first bytes: {content[:16]!r})"
    # html
    text = content.decode("utf-8", errors="replace")
    for marker in _CHALLENGE_MARKERS:
        if marker in text:
            return f"Cloudflare challenge marker {marker!r} in HTML"
    if len(text) < 2048:
        return f"suspiciously short HTML ({len(text)} chars)"
    return None


def _default_chain() -> list[str]:
    chain = [
        name for name, (_, env_var) in _PROVIDERS.items()
        if os.environ.get(env_var)
    ]
    # curl last: free and instant locally (Polish residential IP passes CF);
    # on GH Actions it yields the challenge page, which validation rejects.
    chain.append("curl")
    return chain


def smart_fetch(url: str, *, expect: str, timeout: int = 120) -> bytes:
    """Fetch `url` through the provider chain, validating content.

    expect: 'html' | 'pdf' | 'xlsx' - drives content validation.

    Per API provider: no-JS attempt first (cheap credits), JS render
    second (only if the cheap response failed validation). Providers
    whose account is dead (401/402/403/429) are skipped without a JS
    retry. Raises RuntimeError with the full attempt trail if the
    whole chain fails.
    """
    chain_env = os.environ.get("BGK_FETCH_PROVIDERS", "")
    chain = [p.strip() for p in chain_env.split(",") if p.strip()] or _default_chain()

    trail: list[str] = []
    for name in chain:
        if name == "curl":
            try:
                content = curl_get(url, timeout=min(timeout, 90))
            except (subprocess.SubprocessError, OSError) as e:
                trail.append(f"curl: {type(e).__name__}: {e}")
                continue
            reason = _validate(content, expect)
            if reason is None:
                return content
            trail.append(f"curl: invalid content: {reason}")
            continue

        if name not in _PROVIDERS:
            trail.append(f"{name}: unknown provider (check BGK_FETCH_PROVIDERS)")
            continue
        fetch_fn, env_var = _PROVIDERS[name]
        if not os.environ.get(env_var):
            trail.append(f"{name}: skipped ({env_var} not set)")
            continue

        for render_js in (False, True):
            label = f"{name}({'js' if render_js else 'no-js'})"
            try:
                content = fetch_fn(url, render_js=render_js, timeout=timeout)
            except FetchError as e:
                trail.append(f"{label}: {e}")
                if e.status in _ACCOUNT_DEAD_STATUSES:
                    break  # account dead - next provider, skip JS retry
                continue
            reason = _validate(content, expect)
            if reason is None:
                if trail:
                    print(f"  -> smart_fetch: {label} succeeded after: "
                          f"{'; '.join(trail)}", flush=True)
                return content
            trail.append(f"{label}: invalid content: {reason}")

    raise RuntimeError(
        f"All fetch providers failed for {url!r} (expect={expect}).\n  "
        + "\n  ".join(trail)
    )
