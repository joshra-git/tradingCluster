"""
Pulls REAL spend from Anthropic's Usage & Cost Admin API, rather than estimating
it from token counts and a hardcoded price list.

Two things to know:
  - This needs an ADMIN key (sk-ant-admin...), which is a different credential
    from the sk-ant-api... key the agents use. Create it in the Console under
    Settings -> Admin Keys.
  - The Admin API is unavailable to individual accounts. You need an
    organisation (Console -> Settings -> Organization) for these endpoints to
    exist at all.

If the key is missing or the call fails, everything degrades to the token-based
estimate the dashboard already had. Nothing breaks; the figure is just labelled
as estimated rather than actual.
"""
import os
import logging
import requests
from datetime import datetime, timedelta, timezone

log = logging.getLogger("anthropic_admin")

ADMIN_KEY = os.environ.get("ANTHROPIC_ADMIN_KEY", "").strip()
BASE = "https://api.anthropic.com/v1/organizations"
FX_URL = "https://api.frankfurter.app/latest"


def is_enabled():
    return bool(ADMIN_KEY)


def _headers():
    return {"x-api-key": ADMIN_KEY, "anthropic-version": "2023-06-01"}


def fetch_cost_report(days=7):
    """Daily USD cost buckets for the last N days. Returns [{date, usd}, ...].

    The cost endpoint is authoritative - it is the same data the Console's Cost
    page draws, so it already accounts for cache-write and cache-read pricing
    without us having to model any of that ourselves.
    """
    if not is_enabled():
        return []

    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start = end - timedelta(days=days)
    try:
        resp = requests.get(
            f"{BASE}/cost_report",
            headers=_headers(),
            params={
                "starting_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ending_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            timeout=20,
        )
        if resp.status_code == 401:
            log.error("cost report: admin key rejected (401). Is it an sk-ant-admin key?")
            return []
        if resp.status_code == 404:
            log.error("cost report: endpoint not found (404). Individual accounts do not "
                      "have the Admin API - an organisation is required.")
            return []
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        log.error(f"cost report failed: {e}")
        return []

    # The response shape groups results into time buckets, each holding line
    # items. Sum every amount in a bucket to get that day's total. Parsing is
    # deliberately defensive - the exact key names have moved before.
    out = []
    for bucket in payload.get("data", []):
        day = (bucket.get("starting_at") or "")[:10]
        total = 0.0
        for item in bucket.get("results", []) or []:
            amount = item.get("amount") or item.get("cost") or item.get("amount_usd") or 0
            try:
                total += float(amount)
            except (TypeError, ValueError):
                continue
        if day:
            out.append({"date": day, "usd": round(total, 6)})
    return out


def fetch_usd_to_aud(fallback=1.50):
    """Anthropic bills in USD only, so the AUD figure needs a conversion rate.
    Frankfurter is free, keyless and backed by ECB reference rates. Falls back
    to the configured rate if unreachable - a slightly stale rate is much better
    than no AUD figure at all."""
    try:
        resp = requests.get(FX_URL, params={"from": "USD", "to": "AUD"}, timeout=10)
        resp.raise_for_status()
        rate = float(resp.json()["rates"]["AUD"])
        if 0.5 < rate < 5.0:   # sanity guard against a malformed response
            return rate, True
        log.warning(f"FX rate {rate} outside plausible range, using fallback")
    except Exception as e:
        log.warning(f"FX lookup failed ({e}), using fallback rate {fallback}")
    return fallback, False
