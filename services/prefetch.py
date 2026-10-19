"""Audio prefetch: the download of a beat's sample starts while the producer is still deciding.

The scan is dominated by the provider's file preparation (84 of 88 s, 117 of 125 s measured on
2026-10-19). The site asks for a prefetch the moment a producer analyses a beat that is worth a
scan; when they then press "Get Artists", the sample is already here (or its download is already
under way and the scan simply joins it), and only ACRCloud and the enrichment are left.

Everything is in this process: the service runs as ONE worker (see render.yaml), the same one that
answers /prefetch and runs the scan, so a plain dict and the local disk are enough.

A prefetch that nobody follows with a scan is a wasted RapidAPI download, so there are three
guards: one entry per video, a daily ceiling (PREFETCH_MAX_PER_DAY, default 30) and at most two
downloads at once. The site adds the conditions on the beat and the producer.
"""
import os
import time
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

TTL_S = 600
MAX_PER_DAY = int(os.getenv("PREFETCH_MAX_PER_DAY", "30"))

_lock = threading.Lock()
_entries = {}            # video_id -> {'event': Event, 'path': str | None, 'at': float}
_day = {'date': None, 'n': 0}
_slots = threading.Semaphore(2)


def _remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _sweep_locked():
    now = time.time()
    for vid in [v for v, e in _entries.items() if e['event'].is_set() and now - e['at'] > TTL_S]:
        _remove(_entries[vid]['path'])
        del _entries[vid]


def start(video_id, youtube_url, youtube_service):
    """Begin downloading the sample in the background. Returns 'started', 'already' or 'capped'."""
    with _lock:
        _sweep_locked()
        if video_id in _entries:
            return 'already'
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        if _day['date'] != today:
            _day['date'], _day['n'] = today, 0
        if _day['n'] >= MAX_PER_DAY:
            return 'capped'
        _day['n'] += 1
        entry = {'event': threading.Event(), 'path': None, 'at': time.time()}
        _entries[video_id] = entry
    threading.Thread(target=_work, args=(video_id, youtube_url, youtube_service, entry), daemon=True).start()
    return 'started'


def _work(video_id, youtube_url, youtube_service, entry):
    path = None
    try:
        with _slots:
            t0 = time.time()
            src = youtube_service.download_audio(youtube_url)
            if src:
                ext = os.path.splitext(src)[1] or '.mp3'
                path = os.path.join(os.path.dirname(src), f'beatlink_pf_{video_id}{ext}')
                os.replace(src, path)   # own name: a scan of the same beat cleans beatlink_{id}.* first
            logger.info(f"⏩ [prefetch] {video_id}: {'ready' if path else 'failed'} in {round(time.time() - t0, 1)}s")
    except Exception as e:
        logger.error(f"❌ [prefetch] {video_id} failed: {e}", exc_info=True)
        path = None
    finally:
        entry['path'] = path
        entry['at'] = time.time()
        if not path:
            with _lock:
                if _entries.get(video_id) is entry:
                    del _entries[video_id]
        entry['event'].set()


def take(video_id, wait_s=120):
    """The prefetched sample of this beat, waiting for a download still in progress. Returns
    (path, waited_seconds) or (None, 0) when nothing was prefetched or it failed; the caller
    then downloads the sample itself. The file is the caller's to delete."""
    with _lock:
        entry = _entries.get(video_id)
    if not entry:
        return None, 0
    t0 = time.time()
    if not entry['event'].wait(wait_s):
        return None, 0
    with _lock:
        if _entries.get(video_id) is entry:
            del _entries[video_id]
    path = entry['path']
    if path and os.path.exists(path) and time.time() - entry['at'] <= TTL_S:
        return path, round(time.time() - t0, 1)
    _remove(path)
    return None, 0
