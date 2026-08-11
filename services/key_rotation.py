import os
import logging
from services.api_usage_tracker import record_api_usage

logger = logging.getLogger(__name__)

# Shared pool of backup RapidAPI accounts, reused across every API that rotates through
# them (currently: the Spotify scraper and the async-CDN YouTube downloader — see
# spotify_service.py / youtube_service.py). One physical account/key can be subscribed to
# several RapidAPI products at once, so the same 7 accounts cover both. Labelled by the
# account's own email (per the user's own request, 2026-08-11: "j'associe l'adresse mail
# du compte à la clé... fais de même") purely so a human looking at logs or the
# /settings usage panel can tell which account is which — never used for auth itself.
#
# Slot 1 (the primary key) is intentionally NOT in this pool: it's each service's own
# existing single env var (RAPIDAPI_KEY for YouTube, RAPIDAPI_KEY_SPOTIFY for Spotify),
# since those predate this rotation system and already work.
POOL_ACCOUNTS = [
    ("777asthma@gmail.com", "RAPIDAPI_KEY_POOL_2"),
    ("pluggzhawkins@gmail.com", "RAPIDAPI_KEY_POOL_3"),
    ("ryuklol724@gmail.com", "RAPIDAPI_KEY_POOL_4"),
    ("lifemaxxing51@gmail.com", "RAPIDAPI_KEY_POOL_5"),
    ("deadboymenvce@gmail.com", "RAPIDAPI_KEY_POOL_6"),
    ("lemondedebrian27@gmail.com", "RAPIDAPI_KEY_POOL_7"),
    ("bryanquibo.d@gmail.com", "RAPIDAPI_KEY_POOL_8"),
]


class KeyRotator:
    """
    Waterfalls a RapidAPI-backed call through an ordered list of (label, key) accounts:
    the primary first, then the shared pool above, advancing to the next one the moment
    the current key's own X-RateLimit-Requests-Remaining header reports 1 or fewer left.
    Never advances on a guess — only on the real quota signal RapidAPI itself sends back.

    One instance per API (constructed with that API's own primary key + the shared pool),
    so two APIs rotating through the same physical accounts still track and switch
    independently — one can be three keys deep while the other is still on its first.

    Advances forward only: once past a key it never returns to it mid-cycle. That's a
    deliberate simplification (no reset-detection), acceptable since the point is
    surviving one billing cycle, not indefinitely cycling.
    """

    def __init__(self, api_name, primary_label, primary_env_var):
        self.api_name = api_name
        accounts = [(primary_label, os.getenv(primary_env_var))] + [
            (label, os.getenv(env_var)) for label, env_var in POOL_ACCOUNTS
        ]
        # Drop any slot whose env var isn't set yet — lets the pool be filled in
        # gradually (e.g. only slots 2-3 configured on Render so far) without the
        # rotator ever handing back an empty key.
        self.accounts = [(label, key) for label, key in accounts if key]
        self.idx = 0
        if not self.accounts:
            logger.error(f"❌ KeyRotator for {api_name}: no keys configured at all (checked {primary_env_var} + the shared pool)")
        elif len(self.accounts) == 1:
            logger.info(f"ℹ️ KeyRotator for {api_name}: only {self.accounts[0][0]} configured, no fallback available yet")
        else:
            logger.info(f"✅ KeyRotator for {api_name}: {len(self.accounts)} account(s) configured, starting on {self.accounts[0][0]}")

    def current(self):
        """(label, key) for the account this call should use right now. (None, None) if
        nothing is configured at all — callers already handle a missing key gracefully."""
        if not self.accounts:
            return (None, None)
        return self.accounts[min(self.idx, len(self.accounts) - 1)]

    def note_response(self, headers):
        """Call once per response from a request made with current()'s key. Records usage
        under that account's label and advances to the next account if this one just hit
        its last request."""
        label, _key = self.current()
        if label is None:
            return
        record_api_usage(self.api_name, headers, key_label=label)

        remaining = headers.get('X-RateLimit-Requests-Remaining')
        try:
            remaining = int(remaining)
        except (TypeError, ValueError):
            return

        if remaining <= 1 and self.idx < len(self.accounts) - 1:
            self.idx += 1
            next_label = self.accounts[self.idx][0]
            logger.warning(f"⚠️ {self.api_name}: '{label}' down to {remaining} request(s) left — switching to '{next_label}'")
