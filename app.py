import os
import logging
import threading
from datetime import datetime
import time
import uuid
from flask import Flask, request, jsonify
from flask_cors import CORS
from services.youtube_service import YouTubeService
from services.acrcloud_service import ACRCloudService
from services.spotify_service import SpotifyService
from services.brave_search_service import BraveSearchService
from services.scan_logger import ScanLogger

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
CORS(app)

# Initialize services
youtube_service = YouTubeService()
acrcloud_service = ACRCloudService()
spotify_service = SpotifyService()
brave_search_service = BraveSearchService()


# ─────────────────────────────────────────────────────────────────────────────
# Background scans
#
# /scan validates, starts the pipeline on its own thread, and returns a job_id
# immediately; the frontend polls /scan/status/<job_id>. That part works and stays.
#
# WHAT IS GONE, AND WHY
# This used to be a shared queue.PriorityQueue drained by a pool of worker threads
# started at import, so a paying tier could jump ahead of a free one. It broke the
# scanner outright: jobs were accepted, logged, and never picked up, with the
# service returning 200 to every poll while nothing ran. The last log before the
# revert says it exactly — `queued=1, busy=0, workers=4`: four live threads, none
# busy, one job waiting, nobody taking it. A module-level queue and threads started
# at import don't survive gunicorn's fork the way a single process would; the
# producer and the consumers ended up looking at different objects, and no amount
# of respawning workers inside the request fixes a queue nobody is reading.
#
# So there is no queue any more. Each scan gets its own thread, created inside the
# request that asked for it, which is the one place we know is in the right process.
# Nothing is shared between jobs except a semaphore that caps how many run at once,
# and a semaphore has no ordering to get wrong.
#
# Priority went with it. At current volume it was ordering a queue that was almost
# always empty, and it is the feature that took the scanner down. If it comes back
# it belongs in a real broker (Redis/Celery) with the queue outside the process,
# not in application memory shared across forked workers.
#
# Jobs still live in memory, so a Render restart mid-scan loses them — same trade as
# before, and a lost scan just gets re-submitted.

# Concurrent scans. Matches gunicorn's --threads 4, so total in-flight work is what
# it has always been; the semaphore only stops a burst from opening twenty of them.
MAX_CONCURRENT_SCANS = 4
# How long a job will wait for a free slot before giving up. Past this the caller is
# told the service is busy instead of being left on a spinner.
MAX_QUEUE_WAIT_SECONDS = 90
# A job running longer than this is reported as failed. The thread can't be killed
# from outside in Python, so this doesn't free the slot — it stops the API telling a
# caller that something is still working when it has clearly stopped.
MAX_JOB_SECONDS = 4 * 60
JOB_RETENTION_SECONDS = 60 * 60  # prune finished jobs after 1h so memory can't grow unbounded

_jobs = {}
_jobs_lock = threading.Lock()
_scan_slots = threading.BoundedSemaphore(MAX_CONCURRENT_SCANS)


def _prune_old_jobs():
    cutoff = time.time() - JOB_RETENTION_SECONDS
    with _jobs_lock:
        stale = [jid for jid, j in _jobs.items() if j['status'] in ('done', 'error') and j['created_at'] < cutoff]
        for jid in stale:
            del _jobs[jid]


# Plans allowed to receive matches that came from a non-Spotify database (Deezer, YouTube
# Music). Deliberately admin-only for now: those rows carry no Spotify artist page, so they
# have no listeners, no Instagram and no email, and shipping contactless rows to a paying
# producer would inflate the result count while lowering what the product actually delivers.
# Widen this set only once alt-source rows have a contact path of their own.
ALT_SOURCE_PLANS = {'admin'}


def execute_scan(youtube_url, scan_id, plan=None):
    """The actual pipeline — unchanged from the old inline /scan handler, just
    extracted so it can be called from a worker thread instead of the request
    thread. Returns the same response body /scan always returned on success,
    or raises on a hard failure (caught by the worker loop).

    `plan` is the caller's current plan, used only to decide whether non-Spotify
    matches are included (see ALT_SOURCE_PLANS)."""
    scan_log = ScanLogger(scan_id, youtube_url)
    try:
        scan_log.log('received', f'Scan requested for {youtube_url}', data={'scan_id': scan_id})

        logger.info("⬇️ Step 1: Getting video metadata...")
        video_info = youtube_service.get_video_info(youtube_url)

        if not video_info['success']:
            scan_log.error('metadata', f"Metadata lookup failed: {video_info.get('message')}", data={'error': video_info.get('error')})
            return {
                'success': False,
                'error': video_info['error'],
                'message': video_info['message'],
            }

        scan_log.log('metadata', f"Resolved \"{video_info['title']}\" by {video_info['author']}", data={
            'title': video_info['title'], 'author': video_info['author'], 'views': video_info['views'],
        })

        logger.info("🎵 Step 2: Downloading audio via Apify...")
        audio_path = youtube_service.download_audio(youtube_url)

        if not audio_path:
            scan_log.error('download', 'Audio download failed (no file returned)')
            return {
                'success': False,
                'error': 'download_failed',
                'message': 'Failed to download audio from YouTube',
            }

        scan_log.log('download', 'Audio downloaded')

        logger.info("🔍 Step 3: Identifying audio with ACR Cloud...")
        matches = acrcloud_service.identify_audio(audio_path)

        youtube_service.cleanup_audio(audio_path)

        if not matches:
            logger.info("ℹ️ No matches found in ACR Cloud")
            scan_log.warn('acrcloud', 'ACRCloud returned 0 matches — no buyers for this beat')
            return {
                'success': True,
                'uploaded_beat': {
                    'title': video_info['title'],
                    'author': video_info['author'],
                    'youtube_url': youtube_url,
                    'views_number': video_info['views'],
                    'thumbnail': video_info['thumbnail'],
                },
                'matched_songs': [],
                'results_count': 0,
            }

        logger.info(f"✅ ACR Cloud found {len(matches)} matches")
        scan_log.log('acrcloud', f'ACRCloud found {len(matches)} raw match(es)', data={
            'count': len(matches),
            'titles': [m.get('title') for m in matches][:20],
        })

        logger.info("🎵 Step 4: Enriching with Spotify metadata...")
        enriched_songs = spotify_service.enrich_tracks(matches)

        logger.info(f"✅ Enriched {len(enriched_songs)} songs with Spotify data")
        scan_log.log('spotify', f'Enriched {len(enriched_songs)} song(s) with Spotify data', data={'count': len(enriched_songs)})

        # A row qualifies either as a full Spotify result (unchanged rules: artist page
        # resolved, cover art, and a real discography behind it), or — for the plans allowed
        # alt sources — as a match that only a non-Spotify database could name. The
        # ghost-artist rule is Spotify-only on purpose: has_discography is derived from the
        # Spotify artist page, so a Deezer/YouTube row has nothing to be judged on and would
        # be dropped for failing a test that was never run against it.
        allow_alt = (plan or '') in ALT_SOURCE_PLANS

        def qualifies(song):
            if song.get('spotify_url'):
                return bool(song.get('cover_url')) and song.get('has_discography', True)
            # Alt-source rows carry has_discography too now: from the Spotify artist page when
            # the ISRC resolved, or from Deezer's own album count when it did not. A 0-release
            # ghost is therefore filtered on either platform, not just on Spotify.
            return allow_alt and bool(song.get('source')) and song.get('has_discography', True)

        filtered_songs = [song for song in enriched_songs if qualifies(song)]

        dropped = []
        for song in enriched_songs:
            if qualifies(song):
                continue
            reasons = []
            if not song.get('spotify_url'):
                if not song.get('source'):
                    reasons.append('no platform link at all')
                elif not allow_alt:
                    reasons.append(f"{song.get('source')}-only (alt sources not enabled for this plan)")
            else:
                if not song.get('cover_url'):
                    reasons.append('no cover')
                if not song.get('has_discography', True):
                    reasons.append('ghost artist (0 published tracks)')
            dropped.append({'name': song.get('artists') or song.get('title'), 'reasons': reasons or ['unqualified']})

        # Provenance breakdown, so the effect of enabling Deezer/YouTube Music is measurable
        # from the scan logs instead of inferred.
        by_source = {}
        for song in filtered_songs:
            key = song.get('source') or ('spotify' if song.get('spotify_url') else 'unknown')
            by_source[key] = by_source.get(key, 0) + 1

        # ISRC coverage, measured rather than assumed. `alt_total` is every non-Spotify match
        # ACRCloud produced, `alt_with_isrc` how many carried an ISRC at all (from ACRCloud or
        # from Deezer), and `alt_resolved` how many of those actually found a counterpart on
        # Spotify — the only ones that end up with listeners and a contact path.
        alt_songs = [s for s in enriched_songs if s.get('source') in ('deezer', 'youtube')]
        alt_with_isrc = sum(1 for s in alt_songs if s.get('isrc'))
        alt_resolved = sum(1 for s in alt_songs if s.get('spotify_author_ID'))
        scan_log.log('sources', f'Kept by source: {by_source} · ISRC {alt_resolved}/{alt_with_isrc} resolved of {len(alt_songs)} alt matches', data={
            'by_source': by_source, 'alt_enabled': allow_alt, 'plan': plan,
            'alt_total': len(alt_songs), 'alt_with_isrc': alt_with_isrc, 'alt_resolved': alt_resolved,
        })

        logger.info(f"🔍 Filtered from {len(enriched_songs)} to {len(filtered_songs)} complete results")
        scan_log.log('filter', f'Kept {len(filtered_songs)} of {len(enriched_songs)} after the completeness/ghost-artist filter', data={
            'kept': len(filtered_songs), 'dropped': len(dropped), 'dropped_detail': dropped[:20],
        })
        scan_log.log('result', f'Scan complete — {len(filtered_songs)} buyer(s) returned', data={'results_count': len(filtered_songs)})

        return {
            'success': True,
            'uploaded_beat': {
                'title': video_info['title'],
                'author': video_info['author'],
                'youtube_url': youtube_url,
                'views_number': video_info['views'],
                'thumbnail': video_info['thumbnail'],
            },
            'matched_songs': filtered_songs,
            'results_count': len(filtered_songs),
        }
    finally:
        scan_log.flush()


def _run_job(job_id):
    """One scan, on its own thread. Everything it touches is either local or the
    job's own dict, so there is no shared structure that can be left in a state
    where work is accepted but never performed."""
    if not _scan_slots.acquire(timeout=MAX_QUEUE_WAIT_SECONDS):
        logger.error(f"❌ Job {job_id} found no free slot in {MAX_QUEUE_WAIT_SECONDS}s")
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is not None:
                job['status'] = 'error'
                job['finished_at'] = time.time()
                job['result'] = {
                    'success': False, 'error': 'queue_stalled',
                    'message': 'The scan service is busy. Please try again in a moment.',
                }
        return

    try:
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is None:
                return  # pruned/unknown — drop
            job['status'] = 'running'
            job['started_at'] = time.time()
            youtube_url, scan_id = job['youtube_url'], job['scan_id']
            plan = job.get('plan')

        try:
            result = execute_scan(youtube_url, scan_id, plan)
            with _jobs_lock:
                job = _jobs.get(job_id)
                if job is not None:
                    job['status'] = 'error' if result.get('success') is False else 'done'
                    job['result'] = result
                    job['finished_at'] = time.time()
        except Exception as e:
            logger.error(f"❌ Unexpected error in scan job {job_id}: {str(e)}", exc_info=True)
            with _jobs_lock:
                job = _jobs.get(job_id)
                if job is not None:
                    job['status'] = 'error'
                    job['result'] = {
                        'success': False,
                        'error': 'internal_error',
                        'message': f'Internal server error: {str(e)}',
                    }
                    job['finished_at'] = time.time()
    finally:
        _scan_slots.release()


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'message': 'BeatLink API is running',
        'version': '2.0.0'
    }), 200


@app.route('/scan', methods=['POST'])
def scan_beat():
    """
    Enqueue a scan of a YouTube Type Beat — returns immediately with a job_id;
    poll GET /scan/status/<job_id> for the result.

    Expected JSON body:
    {
        "youtube_url": "https://www.youtube.com/watch?v=...",
        "scan_id": "optional, correlates scan_logs rows",
        "plan": "optional, e.g. 'ultimate'/'pro'/'starter'/'free' — determines queue priority"
    }

    Returns (202):
    { "success": true, "job_id": "..." }
    """
    data = request.get_json(silent=True) or {}
    youtube_url = data.get('youtube_url')
    scan_id = data.get('scan_id')
    plan = data.get('plan')

    if not youtube_url:
        return jsonify({
            'success': False,
            'error': 'missing_url',
            'message': 'youtube_url is required in request body',
        }), 400

    _prune_old_jobs()

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            'status': 'queued',
            'youtube_url': youtube_url,
            'scan_id': scan_id,
            # Carried through to execute_scan, which uses it to decide whether non-Spotify
            # matches are returned (see ALT_SOURCE_PLANS). Previously read off the request
            # and then dropped, since queue priority was the only thing it fed.
            'plan': plan,
            'created_at': time.time(),
            'started_at': None,
            'finished_at': None,
            'result': None,
        }
    # Started here, in the process that accepted the request. That is the whole point:
    # a thread created inside the request handler cannot end up on the wrong side of a
    # fork from the work it is supposed to do.
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    logger.info(f"📥 Started scan job {job_id} for {youtube_url}")

    return jsonify({'success': True, 'job_id': job_id}), 202


@app.route('/scan/status/<job_id>', methods=['GET'])
def scan_status(job_id):
    """Poll the result of a job enqueued via POST /scan.

    A job that has stopped making progress is reported as failed rather than left at
    'queued' or 'running'. The caller can't tell the difference between slow and dead, so
    saying nothing leaves it on a spinner until its own timeout — which is what an infinite
    loading state actually is.
    """
    now = time.time()
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({'success': False, 'error': 'not_found', 'message': 'Unknown or expired job_id'}), 404

        if job['status'] == 'queued' and now - job['created_at'] > MAX_QUEUE_WAIT_SECONDS:
            logger.error(f"❌ Job {job_id} waited {int(now - job['created_at'])}s without starting")
            job['status'] = 'error'
            job['finished_at'] = now
            job['result'] = {
                'success': False, 'error': 'queue_stalled',
                'message': 'The scan service is busy. Please try again in a moment.',
            }
        elif job['status'] == 'running' and job['started_at'] and now - job['started_at'] > MAX_JOB_SECONDS:
            logger.error(f"❌ Job {job_id} exceeded {MAX_JOB_SECONDS}s — reporting as timed out")
            job['status'] = 'error'
            job['finished_at'] = now
            job['result'] = {
                'success': False, 'error': 'scan_timeout',
                'message': 'This scan took too long and was stopped. Please try again.',
            }

        body = {'success': True, 'status': job['status']}
        if job['status'] in ('done', 'error'):
            body['result'] = job['result']
        return jsonify(body), 200


@app.route('/scan/queue', methods=['GET'])
def scan_queue():
    """Operational view. The failure this replaces was invisible from outside: the
    service answered 200 to everything while doing nothing at all."""
    with _jobs_lock:
        by_status = {}
        for j in _jobs.values():
            by_status[j['status']] = by_status.get(j['status'], 0) + 1
    return jsonify({
        'max_concurrent': MAX_CONCURRENT_SCANS,
        'jobs': by_status,
    }), 200


@app.route('/youtube/search', methods=['GET'])
def youtube_search():
    """Most-watched "{niche} type beat" videos, for the scanner's auto-suggestions.

    Deliberately stateless: this service owns the YouTube key, the Next.js layer owns the
    database and the caching. Keeping the cache on that side is not a detail — search.list
    costs 100 quota units against a daily 10,000, so this must be called once per niche per
    day, never once per user. See YouTubeService.search_type_beats.

    Query: ?niche=trap&published_after=2026-01-01T00:00:00Z&min_views=30000&limit=10
    """
    niche = (request.args.get('niche') or '').strip()
    if not niche:
        return jsonify({'success': False, 'error': 'missing_niche', 'message': 'niche is required'}), 400

    published_after = request.args.get('published_after') or f"{datetime.utcnow().year}-01-01T00:00:00Z"
    try:
        min_views = max(0, int(request.args.get('min_views', 30000)))
        limit = max(1, min(25, int(request.args.get('limit', 10))))
    except ValueError:
        return jsonify({'success': False, 'error': 'bad_params', 'message': 'min_views and limit must be integers'}), 400

    result = youtube_service.search_type_beats(niche, published_after, min_views=min_views, want=limit)
    return jsonify(result), 200 if result.get('success') else 502


@app.route('/reveal-instagram', methods=['POST'])
def reveal_instagram():
    """
    Endpoint to reveal Instagram contact for an artist
    
    This endpoint is called when user clicks "Reveal Contacts" on an artist
    with empty Instagram field.
    
    Expected JSON body:
    {
        "artist_name": "Drake"
    }
    
    Returns:
    {
        "success": true/false,
        "artist_name": "Drake",
        "instagram_url": "https://instagram.com/champagnepapi" or null,
        "source": "brave_search" or "cache"
    }
    """
    try:
        # Get artist name from request
        data = request.get_json()
        
        if not data or 'artist_name' not in data:
            return jsonify({
                'success': False,
                'error': 'missing_artist_name',
                'message': 'artist_name is required in request body'
            }), 400
        
        artist_name = data['artist_name']
        
        if not artist_name or not artist_name.strip():
            return jsonify({
                'success': False,
                'error': 'empty_artist_name',
                'message': 'artist_name cannot be empty'
            }), 400
        
        logger.info(f"🔍 Revealing Instagram for: {artist_name}")
        
        # Check if artist is in cache
        is_cached = artist_name in brave_search_service.cache
        
        # Search Instagram via Brave Search
        instagram_url = brave_search_service.search_instagram(artist_name)
        
        # Determine source
        source = "cache" if is_cached else "brave_search"
        
        # Return result
        if instagram_url:
            logger.info(f"✅ Found Instagram for '{artist_name}': {instagram_url}")
            return jsonify({
                'success': True,
                'artist_name': artist_name,
                'instagram_url': instagram_url,
                'source': source
            }), 200
        else:
            logger.info(f"ℹ️ No Instagram found for '{artist_name}'")
            return jsonify({
                'success': True,
                'artist_name': artist_name,
                'instagram_url': None,
                'source': source,
                'message': 'No Instagram profile found'
            }), 200
        
    except Exception as e:
        logger.error(f"❌ Unexpected error in reveal_instagram: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'internal_error',
            'message': f'Internal server error: {str(e)}'
        }), 500


@app.route('/reveal-instagram/batch', methods=['POST'])
def reveal_instagram_batch():
    """
    Batch endpoint to reveal Instagram for multiple artists
    
    Expected JSON body:
    {
        "artists": ["Drake", "Travis Scott", "Lil Baby"]
    }
    
    Returns:
    {
        "success": true,
        "results": [
            {
                "artist_name": "Drake",
                "instagram_url": "https://instagram.com/champagnepapi",
                "source": "brave_search"
            },
            ...
        ]
    }
    
    Note: Respects rate limiting (1 req/sec), so this endpoint may take time
    """
    try:
        # Get artist names from request
        data = request.get_json()
        
        if not data or 'artists' not in data:
            return jsonify({
                'success': False,
                'error': 'missing_artists',
                'message': 'artists array is required in request body'
            }), 400
        
        artist_names = data['artists']
        
        if not isinstance(artist_names, list):
            return jsonify({
                'success': False,
                'error': 'invalid_artists',
                'message': 'artists must be an array of strings'
            }), 400
        
        if len(artist_names) == 0:
            return jsonify({
                'success': False,
                'error': 'empty_artists',
                'message': 'artists array cannot be empty'
            }), 400
        
        logger.info(f"🔍 Batch revealing Instagram for {len(artist_names)} artists")
        
        # Search all artists
        instagram_urls = brave_search_service.batch_search_instagrams(artist_names)
        
        # Build results
        results = []
        for artist_name, instagram_url in zip(artist_names, instagram_urls):
            is_cached = artist_name in brave_search_service.cache
            results.append({
                'artist_name': artist_name,
                'instagram_url': instagram_url,
                'source': 'cache' if is_cached else 'brave_search'
            })
        
        # Stats
        found_count = sum(1 for r in results if r['instagram_url'] is not None)
        
        logger.info(f"✅ Batch reveal complete: {found_count}/{len(artist_names)} found")
        
        return jsonify({
            'success': True,
            'results': results,
            'stats': {
                'total': len(artist_names),
                'found': found_count,
                'not_found': len(artist_names) - found_count
            }
        }), 200
        
    except Exception as e:
        logger.error(f"❌ Unexpected error in batch reveal: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'internal_error',
            'message': f'Internal server error: {str(e)}'
        }), 500
 
 
if __name__ == '__main__':
    # Log environment variables status
    logger.info("🚀 Starting BeatLink Backend...")
    logger.info(f"✅ APIFY_API_TOKEN: {'Set' if os.getenv('APIFY_API_TOKEN') else 'Missing'}")
    logger.info(f"✅ YOUTUBE_API_KEY: {'Set' if os.getenv('YOUTUBE_API_KEY') else 'Missing'}")
    logger.info(f"✅ ACR Cloud credentials: {'Set' if all([os.getenv('ACR_HOST'), os.getenv('ACR_ACCESS_KEY'), os.getenv('ACR_SECRET_KEY')]) else 'Missing'}")
    logger.info(f"✅ Spotify credentials: {'Set' if all([os.getenv('SPOTIFY_CLIENT_ID'), os.getenv('SPOTIFY_CLIENT_SECRET')]) else 'Missing'}")
    logger.info(f"✅ BRAVE_SEARCH_API_KEY: {'Set' if os.getenv('BRAVE_SEARCH_API_KEY') else 'Missing'}")
    
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=False)
