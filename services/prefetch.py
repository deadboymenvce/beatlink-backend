"""Audio prefetch: the download of a beat's samples starts while the producer is still deciding.

The scan is dominated by the provider's file preparation (84 of 88 s, 117 of 125 s measured on
2026-10-19). The site asks for a prefetch the moment a producer analyses a beat that is worth a
scan; when they then press "Get Artists", the samples are already here (or their download is
already under way and the scan simply joins it), and only ACRCloud and the enrichment are left.

It prepares the SAME samples a scan would download: one clip cut into `segments` windows, the first
of which is the single 30 s sample of a one-window scan, so a prefetch of three windows serves a scan
of one, two or three.

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
# video_id -> {'event': Event, 'files': [{'start', 'path'}], 'segments': int, 'at': float}
_entries = {}
_day = {'date': None, 'n': 0}
_slots = threading.Semaphore(2)


def _remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _drop_files(files):
    for f in files or []:
        _remove(f.get('path'))


def _sweep_locked():
    now = time.time()
    for vid in [v for v, e in _entries.items() if e['event'].is_set() and now - e['at'] > TTL_S]:
        _drop_files(_entries[vid]['files'])
        del _entries[vid]


def start(video_id, youtube_url, youtube_service, starts):
    """Begin downloading the samples in the background. `starts` lists where each window begins
    (seconds). Returns 'started', 'already' or 'capped'."""
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
        entry = {'event': threading.Event(), 'files': [], 'segments': len(starts), 'at': time.time()}
        _entries[video_id] = entry
    threading.Thread(target=_work, args=(video_id, youtube_url, youtube_service, list(starts), entry), daemon=True).start()
    return 'started'


def _work(video_id, youtube_url, youtube_service, starts, entry):
    files = []
    try:
        with _slots:
            t0 = time.time()
            if len(starts) > 1:
                got = youtube_service.download_segments(youtube_url, starts) or []
            else:
                src = youtube_service.download_audio(youtube_url)
                got = [{'start': starts[0], 'path': src}] if src else []
            for i, f in enumerate(got):
                ext = os.path.splitext(f['path'])[1] or '.mp3'
                # Its own name: a scan of the same beat cleans beatlink_{id}* first.
                pf = os.path.join(os.path.dirname(f['path']), f'beatlink_pf_{video_id}_{i}{ext}')
                os.replace(f['path'], pf)
                files.append({'start': f['start'], 'path': pf})
            logger.info(f"⏩ [prefetch] {video_id}: {len(files)}/{len(starts)} window(s) ready in {round(time.time() - t0, 1)}s")
    except Exception as e:
        logger.error(f"❌ [prefetch] {video_id} failed: {e}", exc_info=True)
        _drop_files(files)
        files = []
    finally:
        entry['files'] = files
        entry['at'] = time.time()
        if not files:
            with _lock:
                if _entries.get(video_id) is entry:
                    del _entries[video_id]
        entry['event'].set()


def take(video_id, want, wait_s=150):
    """The prefetched windows of this beat (the first `want` of them), waiting for a download still
    in progress. Returns (files, waited_seconds), files being [{'start', 'path'}, ...], or
    (None, 0) when nothing usable was prefetched (none, failed, too few windows, expired): the caller
    then downloads itself. The files are the caller's to delete."""
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
    files = entry['files']
    # A short video yields fewer windows than asked: that is all a scan of it would get too.
    usable = (files and entry['segments'] >= want
              and time.time() - entry['at'] <= TTL_S and all(os.path.exists(f['path']) for f in files))
    if not usable:
        _drop_files(files)
        return None, 0
    _drop_files(files[want:])
    return files[:want], round(time.time() - t0, 1)
