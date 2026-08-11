import os
import time
import logging
import tempfile
import subprocess
import re
import requests

from services.api_usage_tracker import record_api_usage
from services.key_rotation import KeyRotator

logger = logging.getLogger(__name__)

# We only ever keep a 30s slice (from the 15s mark) for ACRCloud, so there's no reason to
# pull the whole 3-4 min file. ~1.5 MB covers ~90s @128kbps / ~180s @64kbps — always well
# past the 45s the FFmpeg extract needs, with margin for the container's framing.
PARTIAL_CAP_BYTES = 1_500_000


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
        # Fallback: youtube-mp3-2025 — async, hands back a CDN link that has to be polled
        # until the on-demand transcode finishes. Slower and less predictable, only used when
        # the sync API above fails or is unavailable.
        # Hardcoded so a stale RAPIDAPI_HOST env can't point at the old endpoint.
        self.rapidapi_host = "youtube-mp3-2025.p.rapidapi.com"
        # This provider (only — the sync one above stays on the single RAPIDAPI_KEY)
        # waterfalls through the shared backup-account pool once RAPIDAPI_KEY's own quota
        # is nearly spent. See services/key_rotation.py.
        self.cdn_key_rotator = KeyRotator('youtube-mp3-2025', 'prodconnect@gmail.com', 'RAPIDAPI_KEY')

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
        failures here are cheap and we move on quickly. Normally the primary provider, but
        called second for now (see download_audio) while its RapidAPI quota is tight.
        When max_bytes is set, stops reading once that many bytes are on disk (partial
        download — see PARTIAL_CAP_BYTES). Returns True if raw_path was written.
        """
        headers = {
            'Content-Type': 'application/json',
            'x-rapidapi-host': self.rapidapi_sync_host,
            'x-rapidapi-key': self.rapidapi_key,
        }
        url = f"https://{self.rapidapi_sync_host}/download-m4a/{video_id}"

        # Single attempt with a generous read timeout: this provider transcodes the file
        # server-side BEFORE it streams a single byte, so the first byte can take ~40-50s.
        # The old 30s read-timeout cut that off mid-transcode and forced a wasteful retry
        # (which is exactly what made scans feel slow). 90s covers virtually any transcode;
        # once bytes start flowing the partial cap stops us in ~1s. On any failure we fall
        # through to the CDN provider rather than retrying the slow transcode here.
        try:
            logger.info(f"🚀 [sync] Requesting M4A: id={video_id}")
            r = requests.get(url, headers=headers, timeout=(10, 90), stream=True)
            record_api_usage('youtube-mp3-audio-video-downloader', r.headers)
        except requests.exceptions.RequestException as e:
            logger.warning(f"⚠️ [sync] Request failed: {e}")
            return False

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

    def _fetch_raw_via_cdn_api(self, video_id, raw_path, max_bytes=None):
        """
        youtube-mp3-2025 — async, on-demand transcode. Hands back a CDN link that 504s
        ("not ready") until conversion finishes, so this polls: re-requesting a fresh link
        a few times and retrying the CDN with backoff. Normally the fallback provider, but
        called first for now (see download_audio) to spare the sync provider's tight quota.
        When max_bytes is set, stops reading once that many bytes are on disk (partial
        download). Returns True if raw_path was written, False otherwise (never raises).
        """
        info_url = f"https://{self.rapidapi_host}/v1/social/youtube/audio"
        # 64kbps instead of 128 — ACRCloud fingerprints, it doesn't listen, so half the
        # bitrate = ~half the bytes to transcode/transfer. If this provider ever rejects
        # 64kbps the CDN call just fails and the parallel sync provider carries the scan.
        params = {'id': video_id, 'quality': '64kbps', 'ext': 'm4a'}

        file_resp = None
        download_url = None
        api_calls = 0
        last_cdn_body = ''
        # Budget kept well under Gunicorn's --timeout (currently 240s), with headroom left
        # for the sync attempt that already ran, plus ACRCloud/Spotify/response afterward.
        # Previously this matched the worker timeout exactly and lost that race, getting
        # SIGKILLed mid-request instead of reaching app.py's graceful 500.
        download_start = time.time()
        # Fallback budget: the sync provider may already have spent up to ~90s before we
        # get here, so cap the CDN at 120s to stay under Gunicorn's --timeout with room
        # for the moov re-download + ACRCloud + Spotify + response.
        deadline = download_start + 120

        while time.time() < deadline and file_resp is None:
            # (Re)fetch a link when we don't have one. Capped to spare RapidAPI quota:
            # after 6 calls we keep polling the last link until the deadline.
            if download_url is None and api_calls < 6:
                api_calls += 1
                try:
                    # Rebuilt every call (not once, up-front): if note_response rotates to
                    # the next account mid-poll, the very next call already uses it instead
                    # of waiting for the next video.
                    headers = {
                        'x-rapidapi-key': self.cdn_key_rotator.current()[1],
                        'x-rapidapi-host': self.rapidapi_host,
                    }
                    logger.info(f"🚀 [cdn] Requesting audio link (call {api_calls}): id={video_id}")
                    info_resp = requests.get(info_url, headers=headers, params=params, timeout=(10, 120))
                    # Only this call goes through the RapidAPI gateway — the CDN download
                    # below hits a plain file link, no RapidAPI headers to record there.
                    self.cdn_key_rotator.note_response(info_resp.headers)
                except requests.exceptions.RequestException as e:
                    logger.warning(f"⚠️ [cdn] API call {api_calls} failed: {e}")
                    time.sleep(5)
                    continue
                if info_resp.status_code != 200:
                    logger.error(f"❌ [cdn] RapidAPI error: {info_resp.status_code} - {info_resp.text[:300]}")
                    time.sleep(5)
                    continue
                data = info_resp.json()
                if data.get('error'):
                    logger.error(f"❌ [cdn] API returned error: {data}")
                    return False
                logger.info(f"🔎 [cdn] API payload (call {api_calls}): status={data.get('status')} keys={list(data.keys())}")
                download_url = data.get('linkDownload') or data.get('linkStream')
                if not download_url:
                    logger.info("⏳ [cdn] Link not ready yet — converting, will re-request…")
                    time.sleep(7)
                    continue

            # Try the CDN link.
            try:
                r = requests.get(download_url, timeout=(10, 120), stream=True)
                ctype = r.headers.get('content-type', '')
                if r.status_code == 200 and ('audio' in ctype or 'mp4' in ctype or 'octet-stream' in ctype):
                    file_resp = r
                    break
                try:
                    last_cdn_body = r.text[:200]
                except Exception:
                    last_cdn_body = ''
                logger.warning(f"⚠️ [cdn] CDN not ready: status={r.status_code}, type={ctype}, body={last_cdn_body}")
                r.close()
            except requests.exceptions.RequestException as e:
                logger.warning(f"⚠️ [cdn] CDN download failed: {e}")

            # Still converting → drop this link to grab a fresh one (until the API cap),
            # then back off before the next attempt.
            if api_calls < 6:
                download_url = None
            time.sleep(6)

        if not file_resp:
            logger.error(f"❌ [cdn] Could not download the M4A within {int(time.time() - download_start)}s "
                         f"(last CDN body: {last_cdn_body or 'n/a'})")
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
        Get a 30s M4A slice for ACRCloud. Sequential, CDN-provider first: as of 2026-08-09
        the sync provider's RapidAPI quota was down to ~12/100 requests for the cycle, so
        priority is flipped to spare it — the slower CDN-polling provider now goes first,
        and the fast sync one is only touched as a fallback if the CDN one fails. Flip this
        back to sync-first once the sync provider's quota has real headroom again (it's the
        faster, more reliable path when both are healthy). Only a partial slice of the file
        is pulled, not the whole track.

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

            # Privilege the CDN provider while the sync provider's quota is tight; only
            # fall back to sync if the CDN one actually fails.
            winner = None
            if self._fetch_raw_via_cdn_api(video_id, raw_path, PARTIAL_CAP_BYTES):
                winner = 'cdn'
            else:
                logger.warning("⚠️ CDN provider failed — falling back to sync provider...")
                if self._fetch_raw_via_sync_api(video_id, raw_path, PARTIAL_CAP_BYTES):
                    winner = 'sync'

            if not winner:
                logger.error("❌ Both audio providers failed")
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
