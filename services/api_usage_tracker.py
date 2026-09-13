import os
import logging
import threading
import requests
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# How long a 403 ("not subscribed to this API") keeps a key out of rotation before it's
# worth probing again. Unlike quota exhaustion, there is no clock RapidAPI reports for
# this — a human can fix the subscription at any moment — so this is a flat cooldown
# rather than a real deadline. Cheap insurance: at most one wasted 403 every 6h per key
# instead of that key being blind-skipped forever until someone remembers to clear its
# row in Supabase by hand.
_RESUBSCRIBE_RECHECK_S = 6 * 3600


def _parse_updated_at(value):
    """Best-effort parse of a PostgREST timestamp. Returns None on anything unparseable
    so the caller can fall back to the old, safe "treat as still spent" behaviour rather
    than silently un-spending a key it can't actually reason about the age of."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None

# Lowest `remaining` already written per (api_name, key_label), and the lock guarding it.
#
# The scan enrichment runs 4 responses in parallel and every one of them upserts this table.
# Whichever upsert lands LAST wins, even when it carries an older, higher `remaining` than a
# concurrent one that landed before it — so the stored number could sit above the truth and
# under-report real usage by up to the worker count. Remembering the lowest value already
# written makes the counter monotonic within a process, at no network cost.
_lowest_seen = {}
_lowest_lock = threading.Lock()

# A monthly reset legitimately sends `remaining` back UP to the plan limit. Only a small
# upward step is treated as the race above; anything larger is a real reset and is written.
_RESET_JUMP = 10

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


def fetch_artist_cache(spotify_artist_id):
    """Previously-scraped data for a Spotify artist, or None.

    Deliberately has no expiry (see supabase/artist_scrape_cache.sql). Silent None on any
    failure, so a cache outage costs a RapidAPI request rather than a broken scan.
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY or not spotify_artist_id:
        return None
    try:
        r = requests.get(
            f"{_SUPABASE_URL}/rest/v1/artist_scrape_cache",
            headers={"apikey": _SUPABASE_KEY, "Authorization": f"Bearer {_SUPABASE_KEY}"},
            params={"select": "payload", "spotify_artist_id": f"eq.{spotify_artist_id}", "limit": 1},
            timeout=5,
        )
        if r.status_code != 200:
            return None
        rows = r.json() or []
        return rows[0].get("payload") if rows else None
    except Exception:
        return None


def store_artist_cache(spotify_artist_id, payload):
    """Persist one artist scrape. Fire-and-forget: never let caching break a scan."""
    if not _SUPABASE_URL or not _SUPABASE_KEY or not spotify_artist_id:
        return
    try:
        requests.post(
            f"{_SUPABASE_URL}/rest/v1/artist_scrape_cache?on_conflict=spotify_artist_id",
            headers={
                "apikey": _SUPABASE_KEY,
                "Authorization": f"Bearer {_SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
            json=[{
                "spotify_artist_id": spotify_artist_id,
                "payload": payload,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }],
            timeout=5,
        )
    except Exception as e:
        logger.warning(f"⚠️ artist_scrape_cache upsert failed for {spotify_artist_id}: {e}")


def record_key_unusable(api_name, key_label):
    """Ecrit qu'une cle ne sert a rien pour cette API, alors qu'aucun en-tete ne le dit.

    Un 403 RapidAPI ("You are not subscribed to this API") ne porte AUCUN en-tete
    x-ratelimit. record_api_usage n'ecrit donc jamais de ligne pour ce couple, et
    fetch_spent_labels, qui selectionne sur remaining <= 1, ne peut pas le retrouver. La
    consequence se voyait dans les journaux du 20/08 : le rotateur redecouvrait a CHAQUE
    demarrage de processus que prodconnect512 n'est pas abonne, au prix d'un aller-retour
    perdu et d'un WARNING trompeur, alors que la reponse etait connue depuis la veille.

    remaining = 0 est le vocabulaire que fetch_spent_labels comprend deja : on ne lui
    apprend pas un nouveau concept, on ecrit dans le sien. limit_value reste nul, ce qui
    distingue une cle non abonnee d'une cle a quota epuise si on lit la table a la main.
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        return
    row = {
        "api_name": api_name,
        "key_label": key_label or "default",
        "limit_value": None,
        "remaining": 0,
        "reset_seconds": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        requests.post(
            f"{_SUPABASE_URL}/rest/v1/api_usage_status?on_conflict=api_name,key_label",
            headers={
                "apikey": _SUPABASE_KEY,
                "Authorization": f"Bearer {_SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
            json=[row],
            timeout=5,
        )
        logger.info(f"📝 {api_name}: '{row['key_label']}' recorded as not subscribed, "
                    f"future restarts will skip it")
    except Exception as e:  # ne doit jamais casser une vraie requete
        logger.warning(f"⚠️ api_usage_status write-off failed for {api_name} "
                       f"({row['key_label']}): {e}")


def fetch_spent_labels(api_name, floor=1):
    """Key labels this API has already been told are out of quota, read back from the same
    table record_api_usage writes to — MINUS whichever of those have plausibly become
    usable again since they were last recorded, so a fixed subscription or a monthly quota
    reset is picked up on the next restart instead of the key staying blind-skipped forever.

    Exists so a fresh process doesn't have to rediscover, one wasted request at a time, what
    the previous one already learned. Without it every container restart sent the rotator
    back to the first key in the list, and if that key was spent, every scan opened by
    burning requests on it before moving on.

    Two different reasons a row can read remaining <= floor, aged differently:
      * Genuine quota exhaustion (limit_value set, reset_seconds set): RapidAPI itself told
        us how many seconds were left, as of updated_at. Past that real deadline the quota
        has almost certainly reset — worth trying again.
      * "Not subscribed" (limit_value NULL — see record_key_unusable): no clock applies,
        since a human can fix the subscription at any moment. Re-probed on a flat
        _RESUBSCRIBE_RECHECK_S cooldown instead of never again.

    Found live 2026-09-14: every pool account except one had been sitting on a Sept-11 403
    with nothing to ever re-try them, so subscribing a fixed account on RapidAPI's side
    would otherwise have changed nothing until someone manually cleared its row by hand.

    Returns a set of labels. On any read failure, or for a row whose age can't be
    determined, degrades to the old "still spent" behaviour rather than wrongly un-spending
    a key that might still be dead.
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        return set()
    try:
        r = requests.get(
            f"{_SUPABASE_URL}/rest/v1/api_usage_status",
            headers={"apikey": _SUPABASE_KEY, "Authorization": f"Bearer {_SUPABASE_KEY}"},
            params={"select": "key_label,remaining,limit_value,reset_seconds,updated_at",
                    "api_name": f"eq.{api_name}", "remaining": f"lte.{floor}"},
            timeout=5,
        )
        if r.status_code != 200:
            return set()

        now = datetime.now(timezone.utc)
        spent = set()
        for row in (r.json() or []):
            label = row.get("key_label")
            if not label:
                continue

            recorded_at = _parse_updated_at(row.get("updated_at"))
            if recorded_at is None:
                spent.add(label)
                continue

            age_s = (now - recorded_at).total_seconds()
            limit_value = row.get("limit_value")
            reset_seconds = row.get("reset_seconds")

            if limit_value is not None and reset_seconds is not None:
                if age_s >= reset_seconds:
                    logger.info(f"⏰ {api_name}: '{label}' recorded reset window has passed "
                                f"({int(age_s)}s ≥ {reset_seconds}s) — retrying it")
                    continue
            elif age_s >= _RESUBSCRIBE_RECHECK_S:
                logger.info(f"⏰ {api_name}: '{label}' was not subscribed {int(age_s)}s ago — "
                            f"re-checking in case that changed")
                continue

            spent.add(label)
        return spent
    except Exception as e:
        logger.warning(f"⚠️ could not read spent keys for {api_name}: {e}")
        return set()


def record_api_usage(api_name, headers, key_label=None):
    """
    api_name: stable identifier for the API, e.g. 'real-time-spotify-data-scraper'
    headers: the requests.Response.headers object (or any dict-like) from the call
    key_label: which RapidAPI account/key made this call, e.g. an account email —
        omit for APIs that only ever use one key. One row per (api_name, key_label),
        so a multi-key API (see key_rotation.py) gets a separate row — and a separate
        /settings progress bar — per key, instead of one row that gets overwritten
        every time the backend rotates to the next key.
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

    label = key_label or "default"
    remaining_int = to_int(remaining)
    if remaining_int is not None:
        memo_key = (api_name, label)
        with _lowest_lock:
            previous = _lowest_seen.get(memo_key)
            if previous is not None and previous < remaining_int <= previous + _RESET_JUMP:
                # A concurrent response already recorded a lower figure. Writing this one
                # would walk the counter backwards and hide requests that really happened.
                return
            _lowest_seen[memo_key] = remaining_int

    row = {
        "api_name": api_name,
        # Never null — the column default ('default') exists so single-key APIs keep
        # upserting onto the same row, but PostgREST sends this literally, so an
        # explicit None here would ship as JSON null and defeat that default.
        "key_label": label,
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
            f"{_SUPABASE_URL}/rest/v1/api_usage_status?on_conflict=api_name,key_label",
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
        logger.warning(f"⚠️ api_usage_status upsert failed for {api_name} ({row['key_label']}): {e}")
