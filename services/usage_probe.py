import os
import logging
from concurrent.futures import ThreadPoolExecutor
import requests
from services.key_rotation import POOL_ACCOUNTS
from services.api_usage_tracker import record_api_usage, record_key_unusable

logger = logging.getLogger(__name__)

# Same bounded-concurrency reasoning as spotify_service's own enrichment pool: enough
# workers to not wait out 8 accounts one at a time, not so many that the host sees a burst
# and starts throttling by IP on top of the per-key limits it already enforces.
PROBE_MAX_WORKERS = 4

# RapidAPI has no free "check my quota" call — the only way to read the X-RateLimit-*
# headers for a key is to make a real request with it. Both targets below are picked to be
# the cheapest real call each host actually exposes, not a dedicated status endpoint
# (neither host has one).
_SPOTIFY_HOST = "real-time-spotify-data-scraper.p.rapidapi.com"
# Drake's Spotify artist id — an always-populated, never-deleted profile, so this never
# 404s regardless of which account/month it's called from.
_SPOTIFY_PROBE_ARTIST_ID = "3TVXtAsR1Inumwj472S9r4"

_YOUTUBE_HOST = "youtube-video-fast-downloader-24-7.p.rapidapi.com"
# "Me at the zoo" — the first YouTube video ever uploaded, 19s, about as canonical and
# unlikely-to-disappear as a video id gets. trim_duration=1 asks the provider for the
# smallest fragment its API accepts, since the endpoint still has to fetch and re-encode
# whatever window it's given regardless of what a real scan would trim.
_YOUTUBE_PROBE_VIDEO_ID = "jNQXAC9IVRw"
_YOUTUBE_PROBE_PARAMS = "quality=251&trim_start_time=0&trim_duration=1"


def _configured_accounts(primary_label, primary_env_var):
    accounts = [(primary_label, os.getenv(primary_env_var))] + [
        (label, os.getenv(env_var)) for label, env_var in POOL_ACCOUNTS
    ]
    return [(label, key) for label, key in accounts if key]


def _probe_one(api_name, host, url, label, key, timeout):
    try:
        resp = requests.get(url, headers={
            'x-rapidapi-host': host,
            'x-rapidapi-key': key,
        }, timeout=timeout)
    except requests.exceptions.RequestException as e:
        return {'label': label, 'ok': False, 'error': str(e)}

    if resp.status_code == 403:
        # No rate-limit headers ride along with a 403 (not subscribed) — record_api_usage
        # would silently no-op on it, so this uses the same "not subscribed" vocabulary
        # record_key_unusable already writes for the rotator to read back.
        record_key_unusable(api_name, label)
        return {'label': label, 'ok': False, 'status': 403, 'error': 'not subscribed'}

    record_api_usage(api_name, resp.headers, key_label=label)
    return {
        'label': label,
        'ok': resp.status_code < 400,
        'status': resp.status_code,
        'limit': resp.headers.get('X-RateLimit-Requests-Limit'),
        'remaining': resp.headers.get('X-RateLimit-Requests-Remaining'),
        'reset_seconds': resp.headers.get('X-RateLimit-Requests-Reset'),
    }


def probe_all_accounts():
    """Sends exactly one real request per configured RapidAPI key, for both the Spotify
    scraper and the YouTube downloader, and writes each response's rate-limit headers
    straight to api_usage_status via the same record_api_usage() a real scan uses — so the
    /settings admin panel reflects every key's true current state immediately, instead of
    only whichever key the most recent real scan happened to touch.

    Costs exactly one real request per configured key, against each key's real monthly
    quota. This is an on-demand admin action (POST /admin/probe-usage), never something to
    run on a timer — see api_usage_tracker.py's own comment on why this project doesn't
    poll RapidAPI for usage by default.
    """
    results = {}

    spotify_accounts = _configured_accounts('Primary', 'RAPIDAPI_KEY_SPOTIFY')
    spotify_url = f"https://{_SPOTIFY_HOST}/artist_overview/?id={_SPOTIFY_PROBE_ARTIST_ID}"
    with ThreadPoolExecutor(max_workers=PROBE_MAX_WORKERS) as ex:
        results['real-time-spotify-data-scraper'] = list(ex.map(
            lambda acc: _probe_one('real-time-spotify-data-scraper', _SPOTIFY_HOST, spotify_url, acc[0], acc[1], (10, 15)),
            spotify_accounts,
        ))

    youtube_accounts = _configured_accounts('prodconnect512@gmail.com', 'RAPIDAPI_KEY')
    youtube_url = f"https://{_YOUTUBE_HOST}/download_audio/{_YOUTUBE_PROBE_VIDEO_ID}?{_YOUTUBE_PROBE_PARAMS}"
    with ThreadPoolExecutor(max_workers=PROBE_MAX_WORKERS) as ex:
        results['youtube-video-fast-downloader-24-7'] = list(ex.map(
            lambda acc: _probe_one('youtube-video-fast-downloader-24-7', _YOUTUBE_HOST, youtube_url, acc[0], acc[1], (10, 60)),
            youtube_accounts,
        ))

    if not spotify_accounts and not youtube_accounts:
        logger.warning("⚠️ probe_all_accounts: no keys configured for either API")

    return results
