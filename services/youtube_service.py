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
        # zm.io.vn (sold on RapidAPI as youtube-mp3-2025, then as youtube-convert-mp3-m4a)
        # is gone. Both listings were the same service: they returned a link to a file they
        # had not built yet, and their progress stream ended in "Download failed or timeout"
        # because they could not read the video from YouTube. Removed 2026-08-16 after a full
        # evening of failed scans. There is one provider now, the one above.
        # Waterfalls through the account pool: RAPIDAPI_KEY (slot 1, prodconnect512) is out of
        # monthly quota and answers 429, so the rotator moves to RAPIDAPI_KEY_POOL_2
        # (777asthma), which is subscribed and working as of 2026-08-16. Slots 3-8 are not
        # subscribed yet and will answer 403 until they are; the rotator writes each one off
        # on that answer and carries on, so adding them later needs no code change.
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
        logger.info(f"🎬 Using RapidAPI: {self.rapidapi_sync_host}")

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
        """YouTube MP3 Audio Video Downloader — one request, a file that already exists.

        GET /get_m4a_download_link/{video_id} answers immediately with:

            {"comment": "The file is ready for download. ... only 10 minutes",
             "file":          "https://s7.<host>/dl_<id>-<hash>.m4a",
             "reserved_file": "https://s7.<mirror>/dl_<id>-<hash>.m4a"}

        No transcode queue, no progress stream, no waiting: the file is prepared before the
        response is sent. Verified 2026-08-16 against vjWwR5FGj1k, the exact video that had
        failed every other route all night — 200, application/octet-stream, 4.2 MB in 1.5s,
        valid `ftypisom` header.

        That is why this replaced youtube-mp3-2025 / youtube-convert-mp3-m4a entirely. Those
        two were the same zm.io.vn service behind different RapidAPI listings, they handed
        back a link to a file they had not built yet, and their own progress stream ended in
        "Download failed or timeout" because they could not read the video from YouTube.

        Two links, both used: `file` first, `reserved_file` as the mirror. Costs nothing
        extra since only the API call above counts against the RapidAPI quota — the file
        hosts are outside it.

        Note: the API replies with Content-Type text/html while sending JSON, so the body is
        parsed directly instead of trusting the header.
        """
        url = f"https://{self.rapidapi_sync_host}/get_m4a_download_link/{video_id}"

        # One attempt PER ACCOUNT, advancing only on the two answers that mean this key is
        # finished here: 429 (monthly quota spent) and 403 (not subscribed to this product).
        while True:
            label, key = self.sync_key_rotator.current()
            if key is None:
                logger.error("❌ [dl] No usable RapidAPI account left")
                return False

            headers = {
                'Content-Type': 'application/json',
                'x-rapidapi-host': self.rapidapi_sync_host,
                'x-rapidapi-key': key,
            }

            try:
                logger.info(f"🚀 [dl] Requesting download link: id={video_id} (account: {label})")
                r = requests.get(url, headers=headers, timeout=(10, 90))
            except requests.exceptions.RequestException as e:
                logger.warning(f"⚠️ [dl] Request failed: {e}")
                return False

            switched = self.sync_key_rotator.note_response(r.headers, used_label=label)

            if r.status_code in (403, 429):
                logger.warning(f"⚠️ [dl] {r.status_code} on '{label}': {r.text[:160]}")
                if not switched:
                    reason = ('is not subscribed to this API' if r.status_code == 403
                              else 'is out of monthly quota')
                    switched = self.sync_key_rotator.mark_unusable(label, reason)
                if switched:
                    continue
                logger.error("❌ [dl] Every configured account is spent")
                return False
            break

        if r.status_code != 200:
            logger.warning(f"⚠️ [dl] Non-200: {r.status_code} - {r.text[:200]}")
            return False

        try:
            data = r.json()
        except ValueError:
            logger.error(f"❌ [dl] non-JSON response: {r.text[:200]}")
            return False

        links = [u for u in (data.get('file'), data.get('reserved_file')) if u]
        if not links:
            logger.error(f"❌ [dl] no download link in response: {str(data)[:200]}")
            return False
        logger.info(f"🔎 [dl] link ready ({len(links)} host(s)) — {str(data.get('comment'))[:80]}")

        for i, link in enumerate(links):
            which = 'primary' if i == 0 else 'mirror'
            try:
                fr = requests.get(link, timeout=(10, 120), stream=True)
            except requests.exceptions.RequestException as e:
                logger.warning(f"⚠️ [dl] {which} host failed: {e}")
                continue

            ctype = fr.headers.get('content-type', '')
            if fr.status_code != 200 or not ('audio' in ctype or 'mp4' in ctype or 'octet-stream' in ctype):
                logger.warning(f"⚠️ [dl] {which} host: status={fr.status_code} type={ctype}")
                fr.close()
                continue

            written = 0
            with open(raw_path, 'wb') as f:
                for chunk in fr.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
                        if max_bytes and written >= max_bytes:
                            break
            fr.close()
            logger.info(f"✅ [dl] M4A downloaded from {which} ({written // 1024} KB, partial={bool(max_bytes)})")
            return True

        logger.error("❌ [dl] every download host refused the file")
        return False

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

            if not self._fetch_raw_via_sync_api(video_id, raw_path, PARTIAL_CAP_BYTES):
                logger.error("❌ Audio download failed")
                return None
            winner = 'dl'

            m4a_path = self._extract_30s(raw_path, video_id)
            if m4a_path:
                return m4a_path

            # Partial slice couldn't be decoded — re-download the FULL file from the winner.
            logger.warning(f"⚠️ Partial extract failed — re-downloading full file from [{winner}]...")
            full_path = os.path.join(self.temp_dir, f'beatlink_{video_id}_raw_full.m4a')
            fetch_fn = self._fetch_raw_via_sync_api
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
