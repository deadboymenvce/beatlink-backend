import os
import time
import logging
import tempfile
import subprocess
import re
import json
import requests

from services.api_usage_tracker import record_api_usage
from services.key_rotation import KeyRotator

logger = logging.getLogger(__name__)

# We only ever keep a 30s slice (from the 15s mark) for ACRCloud, so there's no reason to
# pull the whole 3-4 min file. ~1.5 MB covers ~90s @128kbps / ~180s @64kbps — always well
# past the 45s the FFmpeg extract needs, with margin for the container's framing.
PARTIAL_CAP_BYTES = 1_500_000

# youtube-mp3-audio-video-downloader (nikzeferis), the "sync" provider: one request, M4A
# straight back. OFF since 2026-08-16.
#
# Only one account (prodconnect@gmail.com) was ever subscribed to it, and that account is
# out of monthly quota. The seven pool accounts are subscribed to youtube-mp3-2025 only, so
# they answer 403 here — calling this provider means eight requests that cannot succeed
# before the CDN path is even tried.
#
# Keeping the code rather than deleting it is deliberate: the quota resets at the start of
# each billing cycle, and this is the faster and more reliable of the two providers when it
# has headroom. Flip this back to True once the primary account's quota has reset, or once
# the pool accounts are subscribed to this product too.
#
# Worth being precise about what this fixes: it does NOT fix a failing scan. The sync path
# gives up in about five seconds, and it is the CDN provider that decides whether a scan
# succeeds. This removes noise from the logs and the first few seconds of a cold scan.
SYNC_PROVIDER_ENABLED = False


def _iso8601_seconds(duration):
    """PT4M13S → 253. Returns 0 for anything unparseable, which drops the video rather
    than letting an unknown length through as if it were a full beat."""
    m = re.match(r'^P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$', duration or '')
    if not m:
        return 0
    d, h, mi, sec = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


class YouTubeService:
    """
    YouTube service using:
    1. YouTube Data API v3 for metadata
    2. RapidAPI (youtube-mp3-2025) for M4A download
    """

    def __init__(self):
        self.temp_dir = tempfile.gettempdir()
        self.api_key = os.getenv("YOUTUBE_API_KEY")
        # RapidAPI key (account-wide, same key covers both hosts below). Set RAPIDAPI_KEY on Render.
        self.rapidapi_key = os.getenv("RAPIDAPI_KEY")
        # Primary: synchronous, returns the M4A directly in one request (fast, ~30s observed).
        # Marked [Deprecated!!] in the RapidAPI dashboard but still live and working as of
        # 2026-07-17 — kept as the fast path anyway since a dead/removed endpoint just fails
        # fast and falls through to the CDN path below, never a regression either way.
        self.rapidapi_sync_host = "youtube-mp3-audio-video-downloader.p.rapidapi.com"
        # Youtube Convert MP3/M4A — the audio provider. Same underlying service as the old
        # youtube-mp3-2025 (same zm.io.vn CDN, same payload shape), reached through a
        # different RapidAPI listing, on a plan of 300 requests/month. That budget is why
        # _fetch_raw_via_cdn_api makes exactly one request per beat.
        # Hardcoded so a stale RAPIDAPI_HOST env can't point at the old endpoint.
        self.rapidapi_host = "youtube-convert-mp3-m4a.p.rapidapi.com"
        # Deliberately a single key (RAPIDAPI_KEY) rather than the account pool, 2026-08-16.
        # Only prodconnect512@gmail.com is subscribed to this listing so far, and the point
        # right now is to get one account working end to end. KeyRotator stays in the repo
        # and the pool can be rebound here once the other accounts are subscribed — see
        # services/key_rotation.py and the sync rotator below, still wired for that.
        self.sync_key_rotator = KeyRotator('youtube-mp3-audio-video-downloader', 'prodconnect@gmail.com', 'RAPIDAPI_KEY')

        if self.api_key:
            logger.info("✅ YOUTUBE_API_KEY configured")
        else:
            logger.warning("⚠️ YOUTUBE_API_KEY not set")
        
        if self.rapidapi_key:
            logger.info("✅ RAPIDAPI_KEY configured (YouTube Downloader)")
        else:
            logger.error("❌ RAPIDAPI_KEY not set")
        
        logger.info(f"📁 Temp directory: {self.temp_dir}")
        logger.info(f"🎬 Using RapidAPI: {self.rapidapi_host}")

    def _extract_video_id(self, url):
        """Extract video ID from various YouTube URL formats"""
        patterns = [
            r'(?:v=|\/)([0-9A-Za-z_-]{11})',
            r'youtu\.be\/([0-9A-Za-z_-]{11})',
            r'^([0-9A-Za-z_-]{11})$'
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None

    def search_type_beats(self, niche, published_after, min_views=30000, want=10):
        """Find the most-watched "{niche} type beat" videos published since a date.

        Used to auto-fill the scanner's suggestions for a niche nobody has curated by
        hand. Ordered by view count, never by date: views are the only signal available
        up front that a beat had enough reach to have real buyers behind it.

        QUOTA — the reason this must never be called per user. search.list costs 100 units
        against a 10,000/day quota, while the videos.list below and every scan's metadata
        lookup cost 1. One careless call per session would starve the scanner itself, so
        the caller is expected to cache the result per niche, not per user.

        Two calls on purpose: search.list won't return view counts, so the ids it gives
        back are re-read through videos.list (1 unit for up to 50) to get statistics and
        duration. That is also what lets Shorts be dropped — a 40-second clip is not a
        beat anyone bought.
        """
        if not self.api_key:
            return {'success': False, 'error': 'missing_api_key', 'message': 'YOUTUBE_API_KEY non configurée'}

        query = f"{niche} type beat".strip()
        try:
            search = requests.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "part": "id",
                    "type": "video",
                    "q": query,
                    "order": "viewCount",
                    "publishedAfter": published_after,
                    "maxResults": 50,
                    "key": self.api_key,
                },
                timeout=20,
            )
            record_api_usage('youtube-data-api', search.headers)
            if search.status_code != 200:
                logger.error(f"❌ YouTube search failed ({search.status_code}) for '{query}': {search.text[:300]}")
                return {'success': False, 'error': 'api_error', 'message': f'YouTube search error: {search.status_code}'}

            ids = [i['id']['videoId'] for i in search.json().get('items', []) if i.get('id', {}).get('videoId')]
            if not ids:
                return {'success': True, 'query': query, 'videos': []}

            details = requests.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={
                    "id": ",".join(ids[:50]),
                    "part": "snippet,statistics,contentDetails",
                    "key": self.api_key,
                },
                timeout=20,
            )
            record_api_usage('youtube-data-api', details.headers)
            if details.status_code != 200:
                logger.error(f"❌ YouTube videos lookup failed ({details.status_code})")
                return {'success': False, 'error': 'api_error', 'message': f'YouTube API error: {details.status_code}'}

            out = []
            for item in details.json().get('items', []):
                stats = item.get('statistics', {})
                snip = item.get('snippet', {})
                views = int(stats.get('viewCount', 0) or 0)
                if views < min_views:
                    continue
                if _iso8601_seconds(item.get('contentDetails', {}).get('duration', '')) < 90:
                    continue  # Short / teaser, not a beat
                thumbs = snip.get('thumbnails', {})
                thumb = (thumbs.get('high') or thumbs.get('medium') or thumbs.get('default') or {}).get('url')
                out.append({
                    'video_id': item['id'],
                    'youtube_url': f"https://www.youtube.com/watch?v={item['id']}",
                    'title': snip.get('title'),
                    'author': snip.get('channelTitle'),
                    'thumbnail_url': thumb,
                    'views_number': views,
                    'published_at': snip.get('publishedAt'),
                })

            out.sort(key=lambda v: v['views_number'], reverse=True)
            logger.info(f"🔎 '{query}' → {len(out)} usable of {len(ids)} results (min {min_views} views)")
            return {'success': True, 'query': query, 'videos': out[:want]}

        except Exception as e:
            logger.error(f"❌ YouTube search error for '{query}': {e}", exc_info=True)
            return {'success': False, 'error': 'search_failed', 'message': str(e)}


    def get_video_info(self, youtube_url):
        """
        Fetch video metadata using YouTube Data API v3
        """
        video_id = self._extract_video_id(youtube_url)
        
        if not video_id:
            return {
                'success': False,
                'error': 'invalid_url',
                'message': 'URL YouTube invalide'
            }
        
        if not self.api_key:
            return {
                'success': False,
                'error': 'missing_api_key',
                'message': 'YOUTUBE_API_KEY non configurée'
            }
        
        try:
            logger.info(f"📋 Fetching metadata for video {video_id}...")
            
            response = requests.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={
                    "id": video_id,
                    "part": "snippet,statistics",
                    "key": self.api_key
                },
                timeout=15
            )
            
            if response.status_code != 200:
                logger.error(f"❌ YouTube API error: {response.status_code}")
                try:
                    error_data = response.json()
                    logger.error(f"Error details: {error_data}")
                except:
                    logger.error(f"Response text: {response.text[:500]}")
                return {
                    'success': False,
                    'error': 'api_error',
                    'message': f'YouTube API error: {response.status_code}'
                }
            
            data = response.json()
            items = data.get('items', [])
            
            if not items:
                return {
                    'success': False,
                    'error': 'video_unavailable',
                    'message': 'Vidéo introuvable, privée ou supprimée'
                }
            
            snippet = items[0].get('snippet', {})
            statistics = items[0].get('statistics', {})
            thumbnails = snippet.get('thumbnails', {})
            
            # Get best quality thumbnail
            thumbnail = ''
            for quality in ('maxres', 'high', 'medium', 'default'):
                if quality in thumbnails:
                    thumbnail = thumbnails[quality].get('url', '')
                    break
            
            title = snippet.get('title', 'Unknown Title')
            logger.info(f"✅ Metadata retrieved: {title[:60]}")
            
            return {
                'success': True,
                'title': title,
                'author': snippet.get('channelTitle', 'Unknown Author'),
                'views': int(statistics.get('viewCount', 0)),
                'thumbnail': thumbnail,
                'duration': 0
            }
            
        except requests.exceptions.Timeout:
            logger.error("❌ YouTube API timeout (15s)")
            return {
                'success': False,
                'error': 'timeout',
                'message': 'YouTube API timeout'
            }
            
        except Exception as e:
            logger.error(f"❌ Error fetching video info: {str(e)}", exc_info=True)
            return {
                'success': False,
                'error': 'unknown_error',
                'message': f'Error: {str(e)}'
            }

    def _fetch_raw_via_sync_api(self, video_id, raw_path, max_bytes=None):
        """
        youtube-mp3-audio-video-downloader — one request, returns the M4A binary directly
        (no polling). Fast (~30s observed) and simple: it either works or it doesn't, so
        failures here are cheap and we move on quickly. The primary provider, tried first
        (see download_audio).
        When max_bytes is set, stops reading once that many bytes are on disk (partial
        download — see PARTIAL_CAP_BYTES). Returns True if raw_path was written.
        """
        url = f"https://{self.rapidapi_sync_host}/download-m4a/{video_id}"

        # One attempt PER ACCOUNT, waterfalling on the two answers that mean "this key is
        # done here" and nothing else: 429 (monthly quota spent) and 403 (this account isn't
        # subscribed to this product). Every other failure still falls straight through to
        # the CDN provider, since retrying a slow transcode on another key would cost ~90s
        # to learn nothing. The loop terminates on its own: the rotator only ever moves
        # forward and hands back (None, None) once every account is written off.
        #
        # Generous read timeout because this provider transcodes server-side BEFORE it
        # streams a single byte, so the first byte can take ~40-50s. The old 30s read-timeout
        # cut that off mid-transcode and forced a wasteful retry, which is exactly what made
        # scans feel slow. 90s covers virtually any transcode; once bytes start flowing the
        # partial cap stops us in ~1s.
        while True:
            sync_label, sync_key = self.sync_key_rotator.current()
            if sync_key is None:
                logger.error("❌ [sync] No usable RapidAPI account left for this provider")
                return False

            headers = {
                'Content-Type': 'application/json',
                'x-rapidapi-host': self.rapidapi_sync_host,
                'x-rapidapi-key': sync_key,
            }

            try:
                logger.info(f"🚀 [sync] Requesting M4A: id={video_id} (account: {sync_label})")
                r = requests.get(url, headers=headers, timeout=(10, 90), stream=True)
            except requests.exceptions.RequestException as e:
                logger.warning(f"⚠️ [sync] Request failed: {e}")
                return False

            # Records usage against the key that actually made the call, and advances on its
            # own when RapidAPI's remaining-requests header says this was the last one.
            switched = self.sync_key_rotator.note_response(r.headers, used_label=sync_label)

            if r.status_code in (403, 429):
                body = r.text[:200]
                r.close()
                logger.warning(f"⚠️ [sync] {r.status_code} on '{sync_label}': {body}")
                if not switched:
                    reason = ('is not subscribed to this API' if r.status_code == 403
                              else 'is out of monthly quota')
                    switched = self.sync_key_rotator.mark_unusable(sync_label, reason)
                if switched:
                    continue
                logger.error("❌ [sync] Every configured account is spent on this provider")
                return False

            break

        if r.status_code != 200:
            logger.warning(f"⚠️ [sync] Non-200: {r.status_code} - {r.text[:200]}")
            return False

        ctype = r.headers.get('content-type', '')
        if 'octet-stream' not in ctype and 'audio' not in ctype:
            logger.warning(f"⚠️ [sync] Unexpected content-type: {ctype}")
            return False

        written = 0
        with open(raw_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)
                    if max_bytes and written >= max_bytes:
                        break
        r.close()
        logger.info(f"✅ [sync] M4A downloaded ({written // 1024} KB, partial={bool(max_bytes)})")
        return True

    def _await_cdn_conversion(self, progress_url, deadline):
        """Watch this provider's transcode until it finishes, reconnecting as needed.

        `linkDownloadProgress` is a Server-Sent Events URL, not a percentage. Its events look
        like:

            event:progress
            data:{"elapsed_time":0.09,"ext":"m4a","progress":25,"quality":"default",
                  "status":"in_progress","video_id":"vjWwR5FGj1k"}

        Three things learned the hard way on 2026-08-15, all of them load-bearing:

        1. The stream gets cut mid-transcode ("Response ended prematurely" at 25%). That is
           routine for a long-lived SSE and says nothing about the job, so a drop means
           reconnect to the SAME url, not give up.
        2. Giving up meant asking the API for a new link, and a new link is a new CDN node
           (cdn-ytb -> cdn-ytb-247 / cdn-ytb-mega in the same scan) which restarts the
           transcode from 0%. Every retry threw away the progress it was waiting on.
        3. The read timeout has to be per-connection. It was being derived from the time left
           in the whole budget, so late attempts got a 13s timeout and were killed while the
           transcode was healthy and advancing.
        """
        last_progress = -1
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                with requests.get(progress_url, stream=True, timeout=(10, min(45, remaining))) as sse:
                    if sse.status_code != 200:
                        logger.warning(f"⚠️ [cdn] progress stream returned {sse.status_code}")
                        return 'unknown' 
                    for line in sse.iter_lines(decode_unicode=True):
                        if time.time() > deadline:
                            logger.warning(f"⚠️ [cdn] transcode still at {last_progress}% at the deadline")
                            return 'unknown' 
                        if not line or not line.startswith('data:'):
                            continue
                        try:
                            ev = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        status = str(ev.get('status', '')).lower()
                        progress = ev.get('progress')
                        if isinstance(progress, (int, float)) and progress != last_progress:
                            last_progress = progress
                            logger.info(f"📶 [cdn] transcode {progress}% ({status or 'n/a'})")
                        if status in ('error', 'failed', 'failure'):
                            # The provider itself says it could not fetch the video from
                            # YouTube ("Download failed or timeout"). Reproduced by hand
                            # against the CDN with a fresh token: it is their fetch that
                            # fails, not our request. Distinguished from an unknown outcome
                            # so the caller can stop now instead of burning the whole budget
                            # hammering a file that will never exist.
                            logger.error(f"❌ [cdn] provider reported {status}: "
                                         f"{str(ev.get('message'))[:160]}")
                            return 'error' 
                        if status in ('done', 'completed', 'complete', 'finished', 'success')                                 or (isinstance(progress, (int, float)) and progress >= 100):
                            logger.info("✅ [cdn] transcode complete")
                            return 'done' 
            except requests.exceptions.RequestException as e:
                # A dropped stream is not a failed transcode — reconnect and keep watching.
                logger.info(f"↻ [cdn] progress stream dropped at {last_progress}% "
                            f"({e.__class__.__name__}) — reconnecting")
                time.sleep(2)
                continue
            # Closed cleanly with no terminal event: this provider ends the stream when the
            # job is done, so that IS the completion signal.
            logger.info(f"✅ [cdn] progress stream closed at {last_progress}% — treating as complete")
            return 'done' 

        logger.warning(f"⚠️ [cdn] transcode still at {last_progress}% at the deadline")
        return 'unknown' 

    def _fetch_raw_via_cdn_api(self, video_id, raw_path, max_bytes=None):
        """Youtube Convert MP3/M4A — EXACTLY ONE RapidAPI request per beat.

        That budget is the whole design. The plan is 300 requests/month and a beat has to
        cost one of them, so the API is called once, for the link, and never again for the
        same video. Everything after that (the progress stream, the download, every retry)
        goes straight to the CDN host, which is outside RapidAPI and free.

        The previous version could spend six requests on a single failing beat, which is how
        a quota meant to cover 300 scans covered 50.

        ── quality=128kbps is load-bearing ──────────────────────────────────────────
        The endpoint's own docs: "If quality is missing/invalid, it defaults to audio."
        Omitting it lands on quality=default, which makes the provider transcode on demand —
        the slow, fragile path that produced hours of 504s, "status":"error" and restarted
        conversions on 2026-08-15. 128kbps is the native rate, so the file is served as it
        already exists. 64kbps (the original setting) is a real option but transcodes too.
        Do not remove this parameter, and do not "optimise" it downwards.
        """
        info_url = f"https://{self.rapidapi_host}/v1/social/youtube/audio"
        params = {'id': video_id, 'ext': 'm4a', 'quality': '128kbps'}
        headers = {
            'x-rapidapi-key': self.rapidapi_key,
            'x-rapidapi-host': self.rapidapi_host,
        }

        if not self.rapidapi_key:
            logger.error("❌ [cdn] RAPIDAPI_KEY not set")
            return False

        download_start = time.time()
        # 170s total, under app.py's MAX_JOB_SECONDS (240s), leaving ~70s for ACRCloud +
        # Spotify + the response. Nothing HTTP is waiting on this: POST /scan already
        # returned 202 and the work runs in its own thread (app.py's _run_job).
        deadline = download_start + 170

        # ── The one and only RapidAPI call ──────────────────────────────────────────
        try:
            logger.info(f"🚀 [cdn] Requesting audio link (1 request): id={video_id}")
            info_resp = requests.get(info_url, headers=headers, params=params, timeout=(10, 60))
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ [cdn] API call failed: {e}")
            return False

        record_api_usage('youtube-convert-mp3-m4a', info_resp.headers)

        if info_resp.status_code != 200:
            logger.error(f"❌ [cdn] RapidAPI error: {info_resp.status_code} - {info_resp.text[:300]}")
            return False

        try:
            data = info_resp.json()
        except ValueError:
            logger.error(f"❌ [cdn] non-JSON response: {info_resp.text[:200]}")
            return False

        if data.get('error'):
            logger.error(f"❌ [cdn] API returned an error: {str(data.get('error'))[:200]}")
            return False

        download_url = data.get('linkDownload') or data.get('linkStream')
        progress_url = data.get('linkDownloadProgress')
        logger.info(f"🔎 [cdn] link acquired for \"{str(data.get('title'))[:60]}\" "
                    f"({data.get('lengthSeconds')}s), progress={'yes' if progress_url else 'no'}")

        if not download_url:
            logger.error("❌ [cdn] payload carried no download link")
            return False

        # ── Everything below is CDN-only: no quota is consumed ──────────────────────
        if progress_url:
            outcome = self._await_cdn_conversion(progress_url, deadline)
            if outcome == 'error':
                # Their fetch from YouTube failed. The file will never appear, so retrying
                # the link for the remaining ~160s only delays the failure the user sees.
                logger.error("❌ [cdn] provider could not fetch this video — giving up now")
                return False

        file_resp = None
        attempt = 0
        while time.time() < deadline and file_resp is None:
            attempt += 1
            try:
                r = requests.get(download_url, timeout=(10, 120), stream=True)
                ctype = r.headers.get('content-type', '')
                if r.status_code == 200 and ('audio' in ctype or 'mp4' in ctype or 'octet-stream' in ctype):
                    file_resp = r
                    break
                body = ''
                try:
                    body = r.text[:160]
                except Exception:
                    pass
                logger.warning(f"⚠️ [cdn] download attempt {attempt}: status={r.status_code} type={ctype} body={body}")
                r.close()
            except requests.exceptions.RequestException as e:
                logger.warning(f"⚠️ [cdn] download attempt {attempt} failed: {e}")
            time.sleep(6)

        if not file_resp:
            logger.error(f"❌ [cdn] Could not download the M4A within {int(time.time() - download_start)}s "
                         f"after {attempt} attempt(s) on the same link")
            return False

        logger.info("⬇️ [cdn] CDN ready — downloading M4A...")
        written = 0
        with open(raw_path, 'wb') as f:
            for chunk in file_resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)
                    if max_bytes and written >= max_bytes:
                        break
        file_resp.close()
        logger.info(f"✅ [cdn] M4A downloaded ({written // 1024} KB, partial={bool(max_bytes)})")
        return True

    def _extract_30s(self, raw_path, video_id):
        """
        FFmpeg-extract the 30s ACRCloud slice (from the 15s mark) out of raw_path.
        Returns the m4a path, or None if extraction failed (e.g. a partial download that
        truncated the container's moov atom, which the caller handles by re-downloading
        the full file). Always removes raw_path.
        """
        if not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
            return None

        m4a_path = os.path.join(self.temp_dir, f'beatlink_{video_id}.m4a')
        logger.info("🔄 Extracting 30 seconds (optimized for ACR Cloud)...")
        try:
            ffmpeg_result = subprocess.run(
                [
                    'ffmpeg',
                    '-i', raw_path,
                    '-ss', '15',       # Start at 15 seconds
                    '-t', '30',        # Extract 30 seconds
                    '-acodec', 'copy', # Copy without re-encoding (faster, no quality loss)
                    '-y',              # Overwrite if exists
                    m4a_path
                ],
                capture_output=True,
                text=True,
                timeout=60
            )
        except Exception as e:
            logger.error(f"❌ FFmpeg run failed: {e}")
            if os.path.exists(raw_path):
                os.remove(raw_path)
            return None

        if os.path.exists(raw_path):
            os.remove(raw_path)

        if ffmpeg_result.returncode != 0:
            logger.warning(f"⚠️ FFmpeg extract failed (returncode {ffmpeg_result.returncode}): {ffmpeg_result.stderr[-300:]}")
            return None

        if os.path.exists(m4a_path) and os.path.getsize(m4a_path) > 0:
            logger.info(f"✅ M4A ready: {m4a_path} ({os.path.getsize(m4a_path) // 1024} KB) - 30s extract")
            return m4a_path

        return None

    def download_audio(self, youtube_url):
        """
        Get a 30s M4A slice for ACRCloud. Sequential, sync-provider first.

        Order flipped back on 2026-08-16, which is what the CDN-first note here always said
        to do "once the sync provider's quota has real headroom again". It does now: the sync
        path stopped being a single key and waterfalls through the whole account pool, same
        as the CDN one (see _fetch_raw_via_sync_api). The reason to spare it is gone.

        The incident that forced it is the other half: youtube-mp3-2025 broke on its own
        side, and CDN-first meant every scan burned ~150s polling a provider that could not
        answer before the working path was even tried. Sync is also the faster and more
        reliable of the two when both are healthy, so this is the right resting state, not a
        temporary workaround. Only a partial slice of the file is pulled, not the whole track.

        If the partial file can't be decoded (its M4A moov atom sat past the cut), we
        re-download the FULL file from whichever provider won and extract from that — so
        the partial-download optimization can never turn into a failed scan.
        """
        video_id = self._extract_video_id(youtube_url)

        if not video_id:
            logger.error("❌ Could not extract video ID from URL")
            return None

        # Clean up any existing files (all raw variants included)
        for ext in ('webm', 'm4a', 'mp4', 'mp3', 'wav'):
            for name in (f'beatlink_{video_id}.{ext}',
                         f'beatlink_{video_id}_raw.{ext}',
                         f'beatlink_{video_id}_raw_full.{ext}'):
                p = os.path.join(self.temp_dir, name)
                if os.path.exists(p):
                    os.remove(p)

        raw_path = os.path.join(self.temp_dir, f'beatlink_{video_id}_raw.m4a')

        try:
            logger.info(f"🎵 Downloading audio for {video_id}...")

            # Fast path first, when it is worth calling at all; the slower CDN poller runs
            # either way if it fails. See SYNC_PROVIDER_ENABLED for why it is currently off.
            winner = None
            if SYNC_PROVIDER_ENABLED and self._fetch_raw_via_sync_api(video_id, raw_path, PARTIAL_CAP_BYTES):
                winner = 'sync'
            else:
                if SYNC_PROVIDER_ENABLED:
                    logger.warning("⚠️ Sync provider failed — falling back to CDN provider...")
                if self._fetch_raw_via_cdn_api(video_id, raw_path, PARTIAL_CAP_BYTES):
                    winner = 'cdn'

            if not winner:
                logger.error("❌ Audio download failed on every enabled provider")
                return None

            m4a_path = self._extract_30s(raw_path, video_id)
            if m4a_path:
                return m4a_path

            # Partial slice couldn't be decoded — re-download the FULL file from the winner.
            logger.warning(f"⚠️ Partial extract failed — re-downloading full file from [{winner}]...")
            full_path = os.path.join(self.temp_dir, f'beatlink_{video_id}_raw_full.m4a')
            fetch_fn = self._fetch_raw_via_sync_api if winner == 'sync' else self._fetch_raw_via_cdn_api
            if not fetch_fn(video_id, full_path, None):
                logger.error("❌ Full re-download failed")
                return None
            return self._extract_30s(full_path, video_id)

        except Exception as e:
            logger.error(f"❌ Download error: {str(e)}", exc_info=True)
            return None

    def cleanup_audio(self, audio_path):
        """Delete temporary audio file"""
        try:
            if audio_path and os.path.exists(audio_path):
                os.remove(audio_path)
                logger.info(f"🗑️ Cleaned up: {audio_path}")
                return True
        except Exception as e:
            logger.warning(f"⚠️ Cleanup failed: {str(e)}")
        return False
