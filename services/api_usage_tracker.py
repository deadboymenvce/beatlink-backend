import os
import logging
import requests
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Persists the latest RapidAPI rate-limit headers (limit/remaining/reset) per API into
# Supabase, so the frontend admin can see exact usage without reading Render logs.
#
# Deliberately NOT a dedicated quota check — RapidAPI has no free "check my usage" call,
# so polling it would itself burn requests. Instead this is called as a side effect of
# every real API response the backend already makes during a scan (both success and 401
# responses carry these headers), which is also why the dashboard "updates on every scan"
# for free: it is never more than one real scan behind.
#
# Same raw-REST + service-role pattern as scan_logger.py. No-ops silently if the
# SUPABASE_* env vars are absent or the headers are missing, so a scan never fails because
# of this.

_SUPABASE_URL = os.getenv("SUPABASE_URL")
_SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")


def record_api_usage(api_name, headers):
    """
    api_name: stable identifier for the API, e.g. 'real-time-spotify-data-scraper'
    headers: the requests.Response.headers object (or any dict-like) from the call
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        return

    limit = headers.get('X-RateLimit-Requests-Limit')
    remaining = headers.get('X-RateLimit-Requests-Remaining')
    reset = headers.get('X-RateLimit-Requests-Reset')

    # Nothing to record — this response didn't carry RapidAPI's rate-limit headers
    # (e.g. a network-level failure never reached the gateway).
    if limit is None and remaining is None and reset is None:
        return

    def to_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    row = {
        "api_name": api_name,
        "limit_value": to_int(limit),
        "remaining": to_int(remaining),
        "reset_seconds": to_int(reset),
        # A real timestamp computed here, not the string "now()" — PostgREST inserts JSON
        # values literally, it does not evaluate SQL function calls embedded in a payload.
        # It also must be sent explicitly (not omitted) so the column updates on the
        # UPDATE side of the upsert too, not just on first INSERT (where the column
        # default would otherwise cover it).
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        requests.post(
            f"{_SUPABASE_URL}/rest/v1/api_usage_status?on_conflict=api_name",
            headers={
                "apikey": _SUPABASE_KEY,
                "Authorization": f"Bearer {_SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
            json=[row],
            timeout=5,
        )
    except Exception as e:  # never let usage tracking break a real request
        logger.warning(f"⚠️ api_usage_status upsert failed for {api_name}: {e}")
