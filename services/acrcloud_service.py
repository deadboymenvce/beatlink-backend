import os
import logging
import base64
import hashlib
import hmac
import time
import requests

logger = logging.getLogger(__name__)


class ACRCloudService:
    """Service to identify audio using ACR Cloud fingerprinting"""

    def __init__(self):
        self.host = os.getenv("ACR_HOST")
        self.access_key = os.getenv("ACR_ACCESS_KEY")
        self.secret_key = os.getenv("ACR_SECRET_KEY")
        
        if all([self.host, self.access_key, self.secret_key]):
            logger.info("✅ ACR Cloud credentials configured")
        else:
            logger.error("❌ ACR Cloud credentials missing")

    def _generate_signature(self, string_to_sign):
        """Generate HMAC signature for ACR Cloud API"""
        return base64.b64encode(
            hmac.new(
                self.secret_key.encode('utf-8'),
                string_to_sign.encode('utf-8'),
                digestmod=hashlib.sha1
            ).digest()
        ).decode('utf-8')

    def identify_audio(self, audio_file_path):
        """
        Identify audio file using ACR Cloud
        
        Returns:
        List of matched songs with score >= 85
        [
            {
                'title': 'Song Title',
                'artists': 'Artist Name',
                'spotify_id': 'spotify:track:xxx',
                'score': 92.5
            },
            ...
        ]
        """
        if not os.path.exists(audio_file_path):
            logger.error(f"❌ Audio file not found: {audio_file_path}")
            return []
        
        try:
            logger.info(f"🔍 Identifying audio with ACR Cloud...")
            
            # Read audio file
            with open(audio_file_path, 'rb') as f:
                audio_data = f.read()
            
            # Prepare request
            http_method = "POST"
            http_uri = "/v1/identify"
            data_type = "audio"
            signature_version = "1"
            timestamp = str(int(time.time()))
            
            # Generate signature
            string_to_sign = f"{http_method}\n{http_uri}\n{self.access_key}\n{data_type}\n{signature_version}\n{timestamp}"
            signature = self._generate_signature(string_to_sign)
            
            # Prepare form data
            files = {
                'sample': ('audio.mp3', audio_data, 'audio/mpeg')
            }
            
            data = {
                'access_key': self.access_key,
                'data_type': data_type,
                'signature_version': signature_version,
                'signature': signature,
                'sample_bytes': len(audio_data),
                'timestamp': timestamp
            }
            
            # Send request
            url = f"https://{self.host}{http_uri}"
            
            response = requests.post(
                url,
                files=files,
                data=data,
                timeout=30
            )
            
            if response.status_code != 200:
                logger.error(f"❌ ACR Cloud API error: {response.status_code}")
                return []
            
            result = response.json()
            
            status = result.get('status', {})
            status_code = status.get('code', -1)
            status_msg = status.get('msg', 'Unknown error')
            
            if status_code != 0:
                if status_code == 1001:
                    logger.info("ℹ️ No matches found in ACR Cloud")
                elif status_code == 3001:
                    logger.error("❌ ACR Cloud quota exceeded")
                else:
                    logger.error(f"❌ ACR Cloud error: {status_code} - {status_msg}")
                return []
            
            # Parse results
            metadata = result.get('metadata', {})
            music_list = metadata.get('music', [])
            
            if not music_list:
                logger.info("ℹ️ No music matches found")
                return []
            
            # Filter and format results
            matches = []
            
            for music in music_list:
                score = music.get('score', 0)
                
                # Only keep matches with score >= 85 (high confidence)
                if score < 85:
                    continue
                
                title = music.get('title', 'Unknown')
                artists = music.get('artists', [])
                artist_names = ', '.join([a.get('name', 'Unknown') for a in artists])
                
                # Get Spotify ID if available
                external_metadata = music.get('external_metadata', {})
                spotify_data = external_metadata.get('spotify', {})
                spotify_track = spotify_data.get('track', {})
                spotify_id = spotify_track.get('id', '')
                
                match = {
                    'title': title,
                    'artists': artist_names,
                    'spotify_id': f"spotify:track:{spotify_id}" if spotify_id else '',
                    'score': score
                }
                
                matches.append(match)
            
            logger.info(f"✅ Found {len(matches)} matches with score >= 85")
            
            return matches
            
        except requests.exceptions.Timeout:
            logger.error("❌ ACR Cloud timeout (30s)")
            return []
            
        except Exception as e:
            logger.error(f"❌ ACR Cloud error: {str(e)}", exc_info=True)
            return []
