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
    2. Apify Actor (streamers~youtube-video-downloader) for audio download
    """

    def __init__(self):
        self.temp_dir = tempfile.gettempdir()
        self.api_key = os.getenv("YOUTUBE_API_KEY")
        self.apify_token = os.getenv("APIFY_API_TOKEN")
        
        # Apify Actor endpoint (synchronous with dataset items)
        self.apify_endpoint = "https://api.apify.com/v2/acts/streamers~youtube-video-downloader/run-sync-get-dataset-items"
        
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
        Download audio using Apify Actor with optimizations:
        - MP3 format (best for ACR Cloud)
        - 144p quality (lowest, sufficient for audio)
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
            
            # Prepare Apify Actor input with optimizations
            payload = {
                "filenameTemplateParts": ["timestamp", "title", "uploader"],
                "preferredFormat": "mp3",       # MP3 format (best for ACR Cloud)
                "preferredQuality": "144p",     # Lowest quality (sufficient for audio)
                "videos": [
                    {
                        "url": youtube_url
                    }
                ]
            }
            
            logger.info("🚀 Calling Apify Actor (synchronous)...")
            logger.info(f"📤 Input: {payload}")
            
            # Call Apify with token in query parameter
            response = requests.post(
                self.apify_endpoint,
                params={"token": self.apify_token},
                json=payload,
                timeout=180  # 3 minutes timeout for Apify
            )
            
            logger.info(f"📥 Apify response status: {response.status_code}")
            
            if response.status_code != 200 and response.status_code != 201:
                logger.error(f"❌ Apify API error: {response.status_code}")
                logger.error(f"Response: {response.text[:1000]}")
                return None
            
            # Parse dataset items response
            results = response.json()
            
            if not results or len(results) == 0:
                logger.error("❌ No results from Apify Actor")
                return None
            
            # Get first result
            result = results[0]
            logger.info(f"📊 Result keys: {list(result.keys())}")
            
            # Try to find download URL in various possible fields
            download_url = None
            
            # Common field names for download URL
            possible_fields = [
                'downloadUrl', 
                'url', 
                'audioUrl', 
                'fileUrl',
                'videoUrl',
                'link'
            ]
            
            for field in possible_fields:
                if field in result and result[field]:
                    download_url = result[field]
                    logger.info(f"✅ Found download URL in field '{field}'")
                    break
            
            if not download_url:
                logger.error(f"❌ No download URL found in result. Available fields: {list(result.keys())}")
                logger.error(f"Result sample: {str(result)[:500]}")
                return None
            
            logger.info(f"✅ Got download URL from Apify")
            
            # Download audio file from Apify URL
            logger.info(f"⬇️ Downloading audio file from URL...")
            
            audio_response = requests.get(download_url, timeout=90, stream=True)
            
            if audio_response.status_code != 200:
                logger.error(f"❌ Failed to download audio: {audio_response.status_code}")
                return None
            
            # Save raw audio file
            # The file should already be MP3 from Apify
            raw_path = os.path.join(self.temp_dir, f'beatlink_{video_id}_raw.mp3')
            
            with open(raw_path, 'wb') as f:
                for chunk in audio_response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            
            file_size_kb = os.path.getsize(raw_path) // 1024
            logger.info(f"✅ Audio downloaded: {raw_path} ({file_size_kb} KB)")
            
            # OPTIMIZATION: Extract only 30 seconds (not 60) to minimize data transfer
            # This is where we save costs - ACR Cloud doesn't need more than 30s
            mp3_path = os.path.join(self.temp_dir, f'beatlink_{video_id}.mp3')
            
            logger.info("🔄 Extracting 30 seconds (optimized for ACR Cloud)...")
            
            # Use FFmpeg to extract only 30 seconds starting from 15s mark
            # This avoids intros and provides clean audio for fingerprinting
            ffmpeg_result = subprocess.run(
                [
                    'ffmpeg',
                    '-i', raw_path,
                    '-ss', '15',       # Start at 15 seconds (avoid intro)
                    '-t', '30',        # Duration 30 seconds (optimized vs 60s)
                    '-acodec', 'copy', # Copy codec (no re-encoding, faster)
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
                logger.error(f"❌ FFmpeg error: {ffmpeg_result.stderr[-500:]}")
                return None
            
            if os.path.exists(mp3_path):
                size_kb = os.path.getsize(mp3_path) // 1024
                logger.info(f"✅ MP3 ready: {mp3_path} ({size_kb} KB) - Optimized 30s extract")
                return mp3_path
            
            logger.error("❌ MP3 file not found after extraction")
            return None
            
        except requests.exceptions.Timeout:
            logger.error("❌ Apify API timeout (180s)")
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
