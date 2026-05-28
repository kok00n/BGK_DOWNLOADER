"""Minimal Supabase REST helpers - upsert / rpc / select.

Copied verbatim from CETO_DOWNLOADER/scripts/lib/supabase.py - generic, no
project-specific assumptions.
"""

import os
import time

import requests


def _env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def _normalize_supabase_url(raw: str) -> str:
    """Strip trailing slash and a trailing /rest/v1 if the operator pasted
    the full PostgREST endpoint instead of the project base URL. Without
    this, our `f"{url}/rest/v1/..."` build doubles up to `.../rest/v1/rest/v1/...`
    and PostgREST replies 404 PGRST125 - which is a confusing error for
    a config typo. (Confirmed bite from a 2026-05-28 deploy.)
    """
    u = raw.strip().rstrip("/")
    for suffix in ("/rest/v1", "/rest"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
            break
    return u


SUPABASE_URL = _normalize_supabase_url(_env("SUPABASE_URL"))
SUPABASE_KEY = _env("SUPABASE_SERVICE_ROLE_KEY")

_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


def upsert(
    table: str,
    rows: list[dict],
    on_conflict: str,
    batch_size: int = 500,
    ignore_duplicates: bool = False,
) -> int:
    """Batch upsert rows via PostgREST. Returns number of rows posted.

    ignore_duplicates=False (default): ON CONFLICT DO UPDATE (merge).
    ignore_duplicates=True: ON CONFLICT DO NOTHING - useful when the new row
    is a fallback that should not overwrite already-present authoritative data.
    """
    if not rows:
        return 0
    url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}"
    resolution = "ignore-duplicates" if ignore_duplicates else "merge-duplicates"
    headers = {**_HEADERS, "Prefer": f"resolution={resolution},return=minimal"}
    posted = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        r = _retry_post(url, headers=headers, json=batch)
        if r.status_code >= 400:
            body = r.text[:500] if r.text else "(empty body)"
            print(f"  ! upsert {table} HTTP {r.status_code}: {body}", flush=True)
            print(f"  ! first row sample: {batch[0] if batch else 'n/a'}", flush=True)
        r.raise_for_status()
        posted += len(batch)
    return posted


def rpc(name: str, payload: dict | None = None):
    """Call a Postgres function exposed via PostgREST /rpc/."""
    url = f"{SUPABASE_URL}/rest/v1/rpc/{name}"
    r = _retry_post(url, headers=_HEADERS, json=payload or {})
    r.raise_for_status()
    return r.json()


def select_all(table: str, query: str = "?select=*", page_size: int = 1000) -> list[dict]:
    """Fetch all rows from a table/view, paginating via Range headers."""
    url = f"{SUPABASE_URL}/rest/v1/{table}{query}"
    out = []
    offset = 0
    while True:
        headers = {
            **_HEADERS,
            "Range-Unit": "items",
            "Range": f"{offset}-{offset + page_size - 1}",
        }
        r = _retry_get(url, headers=headers)
        if r.status_code not in (200, 206):
            r.raise_for_status()
        chunk = r.json()
        out.extend(chunk)
        if len(chunk) < page_size:
            break
        offset += page_size
    return out


def _retry_post(url: str, *, headers: dict, json, attempts: int = 3, base_wait: float = 2.0):
    last_err = None
    for i in range(attempts):
        try:
            r = requests.post(url, headers=headers, json=json, timeout=60)
            if 500 <= r.status_code < 600 and i < attempts - 1:
                time.sleep(base_wait * (2**i))
                continue
            return r
        except requests.RequestException as e:
            last_err = e
            time.sleep(base_wait * (2**i))
    raise last_err  # type: ignore[misc]


def _retry_get(url: str, *, headers: dict, attempts: int = 3, base_wait: float = 2.0):
    last_err = None
    for i in range(attempts):
        try:
            r = requests.get(url, headers=headers, timeout=60)
            if 500 <= r.status_code < 600 and i < attempts - 1:
                time.sleep(base_wait * (2**i))
                continue
            return r
        except requests.RequestException as e:
            last_err = e
            time.sleep(base_wait * (2**i))
    raise last_err  # type: ignore[misc]
