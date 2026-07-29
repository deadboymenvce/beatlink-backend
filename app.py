import os
import logging
import itertools
import queue
import threading
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
# Priority scan queue
#
# /scan used to run the whole pipeline (download → identify → enrich) inline in
# the request handler. That's fine one at a time, but under concurrent load every
# request — including a cheap status poll — was stuck behind whatever gunicorn
# thread was mid-scan, and there was no way to make a paying tier's scan jump
# ahead of a free/demo one; requests were just served in whatever order the OS
# handed them to gunicorn's 4 gthread workers.
#
# This splits the two concerns: /scan now only validates + enqueues a job and
# returns immediately (fast, cheap — never blocks on external APIs), while a
# small fixed pool of background worker threads pulls jobs off a priority queue
# and does the actual slow work. The frontend polls /scan/status/<job_id>.
#
# Deliberately in-process (no Redis/Celery): jobs live in memory, so a Render
# restart mid-scan loses anything still queued or running — acceptable at
# current volume, and a lost scan just gets re-submitted. NUM_WORKERS matches
# gunicorn's existing `--threads 4`, so total concurrent scan work is unchanged
# from before; only the queueing/prioritization in front of it is new.
#
# Priority is caller-supplied (the Next.js layer knows the user's plan; this
# service has no auth of its own, same trust boundary /scan already had for
# youtube_url). Mapped defensively across both the current plan ids and the
# planned Starter/Pro/Ultimate rename, so this doesn't need touching again
# when that ships — only the frontend's `plan` string does.
PLAN_PRIORITY = {
    'admin': 0,
    'ultimate': 0, 'scale': 0,
    'pro': 1, 'growth': 1,
    'starter': 2, 'free': 2,
}
DEFAULT_PRIORITY = 3  # unauthenticated / demo / unrecognized plan — served last
NUM_WORKERS = 4
JOB_RETENTION_SECONDS = 60 * 60  # prune finished jobs after 1h so memory can't grow unbounded

_jobs = {}
_jobs_lock = threading.Lock()
_job_queue = queue.PriorityQueue()
_seq = itertools.count()  # tiebreaker so equal-priority jobs stay FIFO, and PriorityQueue
                          # never has to compare two job dicts directly (they aren't orderable)


def _prune_old_jobs():
    cutoff = time.time() - JOB_RETENTION_SECONDS
    with _jobs_lock:
        stale = [jid for jid, j in _jobs.items() if j['status'] in ('done', 'error') and j['created_at'] < cutoff]
        for jid in stale:
            del _jobs[jid]


def execute_scan(youtube_url, scan_id):
    """The actual pipeline — unchanged from the old inline /scan handler, just
    extracted so it can be called from a worker thread instead of the request
    thread. Returns the same response body /scan always returned on success,
    or raises on a hard failure (caught by the worker loop)."""
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

        filtered_songs = [
            song for song in enriched_songs
            if song.get('spotify_url')
            and song.get('cover_url')
            and song.get('has_discography', True)
        ]

        dropped = []
        for song in enriched_songs:
            reasons = []
            if not song.get('spotify_url'):
                reasons.append('no Spotify URL')
            if not song.get('cover_url'):
                reasons.append('no cover')
            if not song.get('has_discography', True):
                reasons.append('ghost artist (0 published tracks)')
            if reasons:
                dropped.append({'name': song.get('artists') or song.get('title'), 'reasons': reasons})

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


def _worker_loop():
    while True:
        priority, seq, job_id = _job_queue.get()
        try:
            with _jobs_lock:
                job = _jobs.get(job_id)
                if job is None:
                    continue  # pruned/unknown — drop
                job['status'] = 'running'
                job['started_at'] = time.time()
                youtube_url, scan_id = job['youtube_url'], job['scan_id']

            try:
                result = execute_scan(youtube_url, scan_id)
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
            _job_queue.task_done()


for _ in range(NUM_WORKERS):
    threading.Thread(target=_worker_loop, daemon=True).start()


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
    priority = PLAN_PRIORITY.get(plan, DEFAULT_PRIORITY)
    with _jobs_lock:
        _jobs[job_id] = {
            'status': 'queued',
            'youtube_url': youtube_url,
            'scan_id': scan_id,
            'created_at': time.time(),
            'started_at': None,
            'finished_at': None,
            'result': None,
        }
    _job_queue.put((priority, next(_seq), job_id))
    logger.info(f"📥 Queued scan job {job_id} (priority={priority}) for {youtube_url}")

    return jsonify({'success': True, 'job_id': job_id}), 202


@app.route('/scan/status/<job_id>', methods=['GET'])
def scan_status(job_id):
    """Poll the result of a job enqueued via POST /scan."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return jsonify({'success': False, 'error': 'not_found', 'message': 'Unknown or expired job_id'}), 404
        body = {'success': True, 'status': job['status']}
        if job['status'] in ('done', 'error'):
            body['result'] = job['result']
        return jsonify(body), 200


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
