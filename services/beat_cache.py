"""Cache of a whole beat's scan result, keyed by YouTube video id (supabase/beat_scan_cache.sql).

32% of the scans on the platform repeat a beat somebody already scanned, nearly always another
producer (2026-10-19, 714 scans). The ACRCloud matches of a beat do not depend on who asks, so the
second producer gets the first one's result at once: no download, no ACRCloud request, no
enrichment.

Reused only while fresh: a beat keeps gaining artists as time goes by, so a result older than
CACHE_TTL_S is a miss and the beat is scanned again (and the cache refreshed). A beat that matched
nothing is trusted for less time (EMPTY_TTL_S), since an artist may still use it.

Everything here is best effort, like the artist cache: a missing table, an outage or a bad row
reads as a miss, so the cache can cost a scan its speed but never its result.
"""
import os
import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

_URL = os.getenv("SUPABASE_URL")
_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# 76% of the repeat scans observed came within 14 days of the previous one.
CACHE_TTL_S = 14 * 86400
EMPTY_TTL_S = 3 * 86400

_warned_missing = False


def _headers(extra=None):
    h = {"apikey": _KEY, "Authorization": f"Bearer {_KEY}"}
    if extra:
        h.update(extra)
    return h


def fetch_beat_cache(video_id, min_segments=1):
    """The stored result for this beat, or None (absent, stale, scanned with fewer windows than
    asked for, or the cache is unavailable). Returns {'payload': {...}, 'cached_at': iso}."""
    global _warned_missing
    if not _URL or not _KEY or not video_id:
        return None
    try:
        r = requests.get(
            f"{_URL}/rest/v1/beat_scan_cache",
            headers=_headers(),
            params={"select": "payload,segments,empty,updated_at", "video_id": f"eq.{video_id}", "limit": 1},
            timeout=5,
        )
        if r.status_code != 200:
            if not _warned_missing:
                _warned_missing = True
                logger.warning(f"⚠️ beat_scan_cache unavailable ({r.status_code}): is supabase/beat_scan_cache.sql run? {r.text[:120]}")
            return None
        rows = r.json() or []
        if not rows:
            return None
        row = rows[0]
        updated = datetime.fromisoformat(str(row["updated_at"]).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        if age > (EMPTY_TTL_S if row.get("empty") else CACHE_TTL_S):
            return None
        if int(row.get("segments") or 1) < int(min_segments or 1):
            return None
        return {"payload": row["payload"], "cached_at": row["updated_at"]}
    except Exception as e:
        logger.warning(f"⚠️ beat_scan_cache read failed for {video_id}: {e}")
        return None


def store_beat_cache(video_id, payload, segments, empty):
    """Keep this beat's result for the next producer. Never raises."""
    if not _URL or not _KEY or not video_id:
        return
    try:
        requests.post(
            f"{_URL}/rest/v1/beat_scan_cache?on_conflict=video_id",
            headers=_headers({"Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"}),
            json=[{
                "video_id": video_id,
                "payload": payload,
                "segments": int(segments or 1),
                "empty": bool(empty),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }],
            timeout=8,
        )
    except Exception as e:
        logger.warning(f"⚠️ beat_scan_cache write failed for {video_id}: {e}")
