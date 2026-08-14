import os
import logging
import threading
from services.api_usage_tracker import record_api_usage, fetch_spent_labels

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
        # Every mutation of idx happens under this lock. Without it, the 4 parallel enrichment
        # workers each observed the SAME exhausted key and each advanced idx by one, so the
        # cursor jumped four slots on a single key's failure and silently skipped three
        # perfectly good accounts without ever calling them. Observed live on 2026-08-14:
        # four consecutive "'Primary' down to -1 — switching to X" lines inside 94ms, after
        # which 777asthma/pluggzhawkins/ryuklol724 had zero recorded usage because they were
        # never actually used.
        self._lock = threading.Lock()
        # Labels known to be unusable: quota at zero, or a 403 meaning the account is not
        # subscribed to this particular API. Seeded from api_usage_status so a restart
        # inherits what the previous process already paid to discover — otherwise the cursor
        # returned to slot 1 on every redeploy and every scan re-opened by burning requests
        # on a key that was already dead. Starting position is the first key NOT in this set.
        self._spent = fetch_spent_labels(api_name)
        if self._spent:
            while self.idx < len(self.accounts) and self.accounts[self.idx][0] in self._spent:
                self.idx += 1
            skipped = [l for l, _ in self.accounts if l in self._spent]
            logger.info(f"⏭️ KeyRotator for {api_name}: skipping {len(skipped)} already-spent account(s) at startup [{', '.join(skipped)}]")
        if not self.accounts:
            logger.error(f"❌ KeyRotator for {api_name}: no keys configured at all (checked {primary_env_var} + the shared pool)")
        elif len(self.accounts) == 1:
            logger.info(f"ℹ️ KeyRotator for {api_name}: only {self.accounts[0][0]} configured, no fallback available yet")
        elif self.idx >= len(self.accounts):
            logger.error(f"❌ KeyRotator for {api_name}: all {len(self.accounts)} configured account(s) are already out of quota — enrichment will be skipped, not retried")
        else:
            labels = ', '.join(label for label, _ in self.accounts)
            # Reports the key it will ACTUALLY start on, which is no longer necessarily the
            # first one now that spent accounts are skipped up front.
            logger.info(f"✅ KeyRotator for {api_name}: {len(self.accounts)} account(s) configured [{labels}], starting on {self.accounts[self.idx][0]}")

    def current(self):
        """(label, key) for the account this call should use right now. (None, None) when
        nothing is configured, or when every configured account is already spent."""
        with self._lock:
            return self._current_locked()

    def _current_locked(self):
        if not self.accounts or self.idx >= len(self.accounts):
            return (None, None)
        return self.accounts[self.idx]

    def _advance_locked(self, reason, from_label):
        """Move to the next account that hasn't already been written off. Caller holds the
        lock. Leaves idx past the end when nothing usable is left, which makes current()
        return (None, None) and lets callers stop instead of retrying into a wall."""
        self._spent.add(from_label)
        while self.idx < len(self.accounts) and self.accounts[self.idx][0] in self._spent:
            self.idx += 1
        if self.idx >= len(self.accounts):
            logger.error(f"❌ {self.api_name}: '{from_label}' {reason} and no account is left — every key is spent")
            return False
        logger.warning(f"⚠️ {self.api_name}: '{from_label}' {reason} — switching to '{self.accounts[self.idx][0]}'")
        return True

    def note_response(self, headers, used_label=None):
        """Call once per response from a request made with current()'s key.

        `used_label` is the label that actually made this call. It is what makes the advance
        race-safe: a response from a key that some other thread has already moved past must
        not advance the cursor a second time. Callers that don't pass it keep the old
        (unsafe) behaviour, so it is threaded through explicitly at every call site.

        Returns True when a switch happened, i.e. an immediate retry on the new key is
        worth making. Returns False when nothing changed or nothing is left.
        """
        with self._lock:
            label, _key = self._current_locked()
            record_label = used_label or label
        if record_label is None:
            return False
        record_api_usage(self.api_name, headers, key_label=record_label)

        remaining = headers.get('X-RateLimit-Requests-Remaining')
        try:
            remaining = int(remaining)
        except (TypeError, ValueError):
            return False

        if remaining > 1:
            return False

        with self._lock:
            active_label, _ = self._current_locked()
            # Stale response: this key was already retired by another worker. Recording its
            # usage above is still correct, advancing again is not.
            if active_label is None or (used_label is not None and used_label != active_label):
                return False
            return self._advance_locked(f"down to {remaining} request(s) left", active_label)

    def mark_unusable(self, used_label, reason):
        """Retire a key on a signal that carries no quota headers at all — in practice a 403,
        which on RapidAPI means this account is not subscribed to this API. Left in rotation
        it would be retried forever and block every account behind it.

        Returns True when another account took its place."""
        with self._lock:
            active_label, _ = self._current_locked()
            if active_label is None or (used_label is not None and used_label != active_label):
                return False
            return self._advance_locked(reason, active_label)

    def all_spent(self):
        """True once no usable account remains — callers should stop retrying entirely
        rather than burn attempts against keys already known to be dead."""
        with self._lock:
            return self._current_locked()[0] is None
