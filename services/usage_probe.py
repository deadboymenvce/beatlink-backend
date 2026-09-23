import os
import logging
import requests
from services.key_rotation import POOL_ACCOUNTS
from services.api_usage_tracker import record_api_usage, record_key_unusable

logger = logging.getLogger(__name__)

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

# One entry per multi-key API this panel tracks. `timeout` is a (connect, read) pair —
# the YouTube downloader genuinely does real work server-side (fetch + re-encode) before
# answering, unlike Spotify's lightweight lookup, so it gets a much longer read window.
_API_CONFIG = {
    'real-time-spotify-data-scraper': {
        'primary_label': 'Primary',
        'primary_env_var': 'RAPIDAPI_KEY_SPOTIFY',
        'host': _SPOTIFY_HOST,
        'url': f"https://{_SPOTIFY_HOST}/artist_overview/?id={_SPOTIFY_PROBE_ARTIST_ID}",
        'timeout': (10, 15),
    },
    'youtube-video-fast-downloader-24-7': {
        'primary_label': 'prodconnect512@gmail.com',
        'primary_env_var': 'RAPIDAPI_KEY',
        'host': _YOUTUBE_HOST,
        'url': f"https://{_YOUTUBE_HOST}/download_audio/{_YOUTUBE_PROBE_VIDEO_ID}?{_YOUTUBE_PROBE_PARAMS}",
        'timeout': (10, 90),
    },
}


def _configured_accounts(primary_label, primary_env_var):
    accounts = [(primary_label, os.getenv(primary_env_var))] + [
        (label, os.getenv(env_var)) for label, env_var in POOL_ACCOUNTS
    ]
    return [(label, key) for label, key in accounts if key]


def probe_one_account(api_name, label):
    """Sends exactly one real request for exactly one (api_name, label) key, reads
    RapidAPI's own rate-limit headers off the response, and writes them to
    api_usage_status via the same record_api_usage() a real scan uses.

    One key at a time, on purpose (see /settings' per-key "Check" buttons): probing all 8
    accounts together meant a slow key at the back of the batch (the YouTube downloader
    genuinely takes real time per call) could still be running when the admin only wanted
    to re-check the one key they knew was stale, burning time and quota on keys that were
    already known-good.
    """
    cfg = _API_CONFIG.get(api_name)
    if not cfg:
        return {'label': label, 'ok': False, 'error': f'unknown api_name: {api_name}'}

    accounts = dict(_configured_accounts(cfg['primary_label'], cfg['primary_env_var']))
    key = accounts.get(label)
    if not key:
        return {'label': label, 'ok': False, 'error': 'this key has no env var configured on this service'}

    try:
        resp = requests.get(cfg['url'], headers={
            'x-rapidapi-host': cfg['host'],
            'x-rapidapi-key': key,
        }, timeout=cfg['timeout'])
    except requests.exceptions.RequestException as e:
        logger.warning(f"⚠️ probe_one_account: {api_name} '{label}' request failed: {e}")
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
