import os
import logging
import tempfile
import subprocess
import re
import requests
import time

logger = logging.getLogger(__name__)


class YouTubeService:
    """
    YouTube service using:
    1. YouTube Data API v3 for metadata
    2. Apify Actor (streamers/youtube-video-downloader) for audio download
    """

    def __init__(self):
        self.temp_dir = tempfile.gettempdir()
        self.api_key = os.getenv("YOUTUBE_API_KEY")
        self.apify_token = os.getenv("APIFY_API_TOKEN")
        
        # Apify Actor ID
        self.actor_id = "streamers/youtube-video-downloader"
        
        if self.api_key:
            logger.info("✅ YOUTUBE_API_KEY configured")
        else:
            logger.warning("⚠️ YOUTUBE_API_KEY not set")
        
        if self.apify_token:
            logger.info("✅ APIFY_API_TOKEN configured")
        else:
            logger.error("❌ APIFY_API_TOKEN not set")
        
        logger.info(f"📁 Temp directory: {self.temp_dir}")

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
        Download audio using Apify Actor with optimizations for ACR Cloud:
        - Audio-only format (no video)
        - Low quality (128kbps sufficient for fingerprinting)
        - Extract only 30 seconds (cost optimization)
        """
        video_id = self._extract_video_id(youtube_url) or 'unknown'
        
        # Clean up any existing files
        for ext in ('webm', 'm4a', 'mp4', 'mp3', 'wav'):
            f = os.path.join(self.temp_dir, f'beatlink_{video_id}.{ext}')
            if os.path.exists(f):
                os.remove(f)
            f_raw = os.path.join(self.temp_dir, f'beatlink_{video_id}_raw.{ext}')
            if os.path.exists(f_raw):
                os.remove(f_raw)
        
        try:
            logger.info(f"🎵 Downloading audio for {video_id} via Apify...")
            
            # Call Apify Actor with optimized settings
            apify_url = f"https://api.apify.com/v2/acts/{self.actor_id}/run-sync-get-dataset-items"
            
            headers = {
                "Authorization": f"Bearer {self.apify_token}",
                "Content-Type": "application/json"
            }
            
            # Actor input - request audio only, low quality
            payload = {
                "videoUrls": [youtube_url],
                "downloadFormat": "audio",  # Audio only, no video
                "quality": "low"  # Low quality sufficient for ACR Cloud
            }
            
            logger.info("🚀 Calling Apify Actor...")
            
            response = requests.post(
                apify_url,
                headers=headers,
                json=payload,
                params={"token": self.apify_token},
                timeout=120  # 2 minutes timeout for Apify
            )
            
            if response.status_code != 200 and response.status_code != 201:
                logger.error(f"❌ Apify API error: {response.status_code}")
                logger.error(f"Response: {response.text[:500]}")
                return None
            
            results = response.json()
            
            if not results or len(results) == 0:
                logger.error("❌ No results from Apify Actor")
                return None
            
            result = results[0]
            
            # Get download URL from Apify response
            download_url = result.get('downloadUrl') or result.get('url') or result.get('audioUrl')
            
            if not download_url:
                logger.error(f"❌ No download URL in Apify response. Keys: {list(result.keys())}")
                return None
            
            logger.info(f"✅ Got download URL from Apify")
            
            # Download audio file from Apify URL
            logger.info(f"⬇️ Downloading audio file...")
            
            audio_response = requests.get(download_url, timeout=60, stream=True)
            
            if audio_response.status_code != 200:
                logger.error(f"❌ Failed to download audio from URL")
                return None
            
            # Save raw audio file
            # Detect file extension from URL or default to webm
            ext = 'webm'
            if '.mp3' in download_url.lower():
                ext = 'mp3'
            elif '.m4a' in download_url.lower():
                ext = 'm4a'
            elif '.mp4' in download_url.lower():
                ext = 'mp4'
            
            raw_path = os.path.join(self.temp_dir, f'beatlink_{video_id}_raw.{ext}')
            
            with open(raw_path, 'wb') as f:
                for chunk in audio_response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            
            logger.info(f"✅ Audio downloaded: {raw_path}")
            
            # OPTIMIZATION: Extract only 30 seconds (not 60) to minimize data transfer
            # This is where we save costs - ACR Cloud doesn't need more than 30s
            mp3_path = os.path.join(self.temp_dir, f'beatlink_{video_id}.mp3')
            
            logger.info("🔄 Converting to MP3 and extracting 30 seconds (optimized for ACR Cloud)...")
            
            # Use FFmpeg to:
            # 1. Convert to MP3 format (best for ACR Cloud)
            # 2. Extract only 30 seconds starting from 15s mark
            # 3. Low quality 128kbps (sufficient for fingerprinting)
            ffmpeg_result = subprocess.run(
                [
                    'ffmpeg',
                    '-i', raw_path,
                    '-ss', '15',       # Start at 15 seconds (avoid intro)
                    '-t', '30',        # Duration 30 seconds (optimized vs 60s)
                    '-vn',             # No video
                    '-acodec', 'libmp3lame',
                    '-b:a', '128k',    # 128kbps (low quality, perfect for ACR Cloud)
                    '-ar', '44100',    # Standard sample rate
                    '-ac', '2',        # Stereo
                    '-y',              # Overwrite
                    mp3_path
                ],
                capture_output=True,
                text=True,
                timeout=60
            )
            
            # Clean up raw file immediately to save space
            if os.path.exists(raw_path):
                os.remove(raw_path)
            
            if ffmpeg_result.returncode != 0:
                logger.error(f"❌ FFmpeg error: {ffmpeg_result.stderr[-300:]}")
                return None
            
            if os.path.exists(mp3_path):
                size_kb = os.path.getsize(mp3_path) // 1024
                logger.info(f"✅ MP3 ready: {mp3_path} ({size_kb} KB) - Optimized 30s @ 128kbps")
                return mp3_path
            
            logger.error("❌ MP3 file not found after conversion")
            return None
            
        except requests.exceptions.Timeout:
            logger.error("❌ Apify API timeout (120s)")
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
