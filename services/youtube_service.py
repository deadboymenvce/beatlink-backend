import os
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
    2. RapidAPI (YouTube MP3 Audio Video Downloader) for M4A download
    """

    def __init__(self):
        self.temp_dir = tempfile.gettempdir()
        self.api_key = os.getenv("YOUTUBE_API_KEY")
        self.rapidapi_key = os.getenv("RAPIDAPI_KEY")
        self.rapidapi_host = os.getenv("RAPIDAPI_HOST", "youtube-mp3-audio-video-downloader.p.rapidapi.com")
        
        if self.api_key:
            logger.info("✅ YOUTUBE_API_KEY configured")
        else:
            logger.warning("⚠️ YOUTUBE_API_KEY not set")
        
        if self.rapidapi_key:
            logger.info("✅ RAPIDAPI_KEY configured")
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
        Download audio using RapidAPI (YouTube MP3 Audio Video Downloader)
        
        This API:
        - Downloads M4A format (faster than MP3)
        - Returns the file directly as binary stream (not JSON)
        - Cost: ~0.000619$ per request (16x cheaper than Apify)
        - Speed: ~30 seconds (2x faster than Apify)
        
        After download, we extract 30 seconds with FFmpeg for ACR Cloud
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
            logger.info(f"🎵 Downloading audio for {video_id} via RapidAPI...")
            
            # Build API endpoint
            # The API uses video ID directly in the URL path
            api_url = f"https://{self.rapidapi_host}/download-m4a/{video_id}"
            
            headers = {
                'X-RapidAPI-Key': self.rapidapi_key,
                'X-RapidAPI-Host': self.rapidapi_host
            }
            
            logger.info(f"🚀 Calling RapidAPI (may take ~30 seconds)...")
            logger.info(f"📤 Endpoint: {api_url}")
            
            # Call RapidAPI
            # Timeout: 60s (observed time is ~30s, so this gives margin)
            response = requests.get(
                api_url,
                headers=headers,
                timeout=(10, 120),
                stream=True  # Important: stream the binary response
            )
            
            logger.info(f"📥 RapidAPI response status: {response.status_code}")
            
            if response.status_code != 200:
                logger.error(f"❌ RapidAPI error: {response.status_code}")
                if response.status_code == 522:
                    logger.error("Error 522: Connection timed out (server issue)")
                elif response.status_code == 524:
                    logger.error("Error 524: Timeout occurred (server took too long)")
                else:
                    logger.error(f"Response text: {response.text[:500]}")
                return None
            
            # Check content type
            content_type = response.headers.get('content-type', '')
            logger.info(f"📦 Content-Type: {content_type}")
            
            if 'octet-stream' not in content_type and 'audio' not in content_type:
                logger.error(f"❌ Unexpected content type: {content_type}")
                return None
            
            # Save M4A file directly from stream
            # The response IS the file, not a JSON with a URL
            raw_path = os.path.join(self.temp_dir, f'beatlink_{video_id}_raw.m4a')
            
            logger.info(f"⬇️ Saving M4A file from stream...")
            
            with open(raw_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
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
