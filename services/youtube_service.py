import os
import time
import logging
import tempfile
import subprocess
import re
import requests

logger = logging.getLogger(__name__)


class YouTubeService:
    """
    YouTube service using:
    1. YouTube Data API v3 for metadata
    2. RapidAPI (youtube-mp3-2025) for M4A download
    """

    def __init__(self):
        self.temp_dir = tempfile.gettempdir()
        self.api_key = os.getenv("YOUTUBE_API_KEY")
        # RapidAPI key (account-wide). Set RAPIDAPI_KEY on Render.
        self.rapidapi_key = os.getenv("RAPIDAPI_KEY")
        # youtube-mp3-2025 API — returns JSON with a CDN download link for the M4A.
        # Hardcoded so a stale RAPIDAPI_HOST env can't point at the old endpoint.
        self.rapidapi_host = "youtube-mp3-2025.p.rapidapi.com"
        
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

    def download_audio(self, youtube_url):
        """
        Download audio using RapidAPI (youtube-mp3-2025)

        Two-step API:
        1. GET /v1/social/youtube/audio?id=<id>&quality=128kbps&ext=m4a
           → JSON containing a CDN `linkDownload`
        2. GET that link → the binary M4A file

        After download, we extract 30 seconds with FFmpeg for ACR Cloud.
        """
        video_id = self._extract_video_id(youtube_url)
        
        if not video_id:
            logger.error("❌ Could not extract video ID from URL")
            return None
        
        # Clean up any existing files
        for ext in ('webm', 'm4a', 'mp4', 'mp3', 'wav'):
            f = os.path.join(self.temp_dir, f'beatlink_{video_id}.{ext}')
            if os.path.exists(f):
                os.remove(f)
            f_raw = os.path.join(self.temp_dir, f'beatlink_{video_id}_raw.{ext}')
            if os.path.exists(f_raw):
                os.remove(f_raw)
        
        try:
            logger.info(f"🎵 Downloading audio for {video_id} via RapidAPI (youtube-mp3-2025)...")

            headers = {
                'x-rapidapi-key': self.rapidapi_key,
                'x-rapidapi-host': self.rapidapi_host,
            }

            # The youtube-mp3-2025 API transcodes ON DEMAND: it hands back a CDN link that
            # 500s ("not ready") until the file is converted. So we POLL — re-requesting a
            # fresh link a few times and patiently retrying the CDN with backoff — instead of
            # hitting the same link 3×/3s and giving up at ~40s.
            info_url = f"https://{self.rapidapi_host}/v1/social/youtube/audio"
            params = {'id': video_id, 'quality': '128kbps', 'ext': 'm4a'}
            raw_path = os.path.join(self.temp_dir, f'beatlink_{video_id}_raw.m4a')

            file_resp = None
            download_url = None
            api_calls = 0
            last_cdn_body = ''
            # Budget kept well under Gunicorn's --timeout (currently 240s) so a CDN that
            # never comes ready still returns None in time for app.py's graceful 500 —
            # previously this matched the worker timeout exactly and lost that race,
            # getting SIGKILLed mid-request instead (no error ever reached the frontend).
            download_start = time.time()
            deadline = download_start + 170

            while time.time() < deadline and file_resp is None:
                # (Re)fetch a link when we don't have one. Capped to spare RapidAPI quota:
                # after 6 calls we keep polling the last link until the deadline.
                if download_url is None and api_calls < 6:
                    api_calls += 1
                    try:
                        logger.info(f"🚀 Requesting audio link (call {api_calls}): id={video_id}")
                        info_resp = requests.get(info_url, headers=headers, params=params, timeout=(10, 120))
                    except requests.exceptions.RequestException as e:
                        logger.warning(f"⚠️ API call {api_calls} failed: {e}")
                        time.sleep(5)
                        continue
                    if info_resp.status_code != 200:
                        logger.error(f"❌ RapidAPI error: {info_resp.status_code} - {info_resp.text[:300]}")
                        time.sleep(5)
                        continue
                    data = info_resp.json()
                    if data.get('error'):
                        logger.error(f"❌ API returned error: {data}")
                        return None
                    logger.info(f"🔎 API payload (call {api_calls}): status={data.get('status')} keys={list(data.keys())}")
                    download_url = data.get('linkDownload') or data.get('linkStream')
                    if not download_url:
                        logger.info("⏳ Link not ready yet — converting, will re-request…")
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
                    logger.warning(f"⚠️ CDN not ready: status={r.status_code}, type={ctype}, body={last_cdn_body}")
                    r.close()
                except requests.exceptions.RequestException as e:
                    logger.warning(f"⚠️ CDN download failed: {e}")

                # Still converting → drop this link to grab a fresh one (until the API cap),
                # then back off before the next attempt.
                if api_calls < 6:
                    download_url = None
                time.sleep(6)

            if not file_resp:
                logger.error(f"❌ Could not download the M4A within {int(time.time() - download_start)}s "
                             f"(last CDN body: {last_cdn_body or 'n/a'})")
                return None

            logger.info("⬇️ CDN ready — downloading M4A...")

            with open(raw_path, 'wb') as f:
                for chunk in file_resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            file_size_kb = os.path.getsize(raw_path) // 1024
            logger.info(f"✅ M4A downloaded: {raw_path} ({file_size_kb} KB)")
            
            # OPTIMIZATION: Extract only 30 seconds for ACR Cloud
            # ACR Cloud accepts M4A format, so we keep it as M4A (no conversion needed)
            m4a_path = os.path.join(self.temp_dir, f'beatlink_{video_id}.m4a')
            
            logger.info("🔄 Extracting 30 seconds (optimized for ACR Cloud)...")
            
            # Use FFmpeg to extract 30 seconds starting from 15s mark
            # -ss 15: Start at 15 seconds (skip potential intro/silence)
            # -t 30: Extract 30 seconds duration
            # -acodec copy: Copy codec without re-encoding (faster, no quality loss)
            # Keeps M4A format (ACR Cloud supports it)
            ffmpeg_result = subprocess.run(
                [
                    'ffmpeg',
                    '-i', raw_path,
                    '-ss', '15',       # Start at 15 seconds
                    '-t', '30',        # Extract 30 seconds
                    '-acodec', 'copy', # Copy without re-encoding
                    '-y',              # Overwrite if exists
                    m4a_path
                ],
                capture_output=True,
                text=True,
                timeout=60
            )
            
            # Clean up raw file immediately to save space
            if os.path.exists(raw_path):
                os.remove(raw_path)
                logger.info(f"🗑️ Cleaned up raw file")
            
            if ffmpeg_result.returncode != 0:
                logger.error(f"❌ FFmpeg error: {ffmpeg_result.stderr[-500:]}")
                return None
            
            if os.path.exists(m4a_path):
                size_kb = os.path.getsize(m4a_path) // 1024
                logger.info(f"✅ M4A ready: {m4a_path} ({size_kb} KB) - Optimized 30s extract")
                return m4a_path
            
            logger.error("❌ M4A file not found after extraction")
            return None
            
        except requests.exceptions.Timeout:
            logger.error("❌ RapidAPI timeout (60s) - Server may be slow or overloaded")
            return None
            
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
