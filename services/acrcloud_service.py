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
        List of matched songs with complete data (score >= 55%)
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
            
            logger.info(f"📊 ACR Cloud returned {len(music_list)} raw results")
            
            # Format and FILTER results
            matches = []
            skipped = 0
            low_score_skipped = 0
            
            for music in music_list:
                score = music.get('score', 0)
                
                # FILTER: Score must be >= 40
                if score < 40:
                    logger.warning(f"⚠️ Skipping result with low score: {score:.1f}%")
                    skipped += 1
                    low_score_skipped += 1
                    continue
                
                # GET TITLE with validation
                title = music.get('title', '').strip()
                if not title:
                    logger.warning(f"⚠️ Skipping result without title")
                    skipped += 1
                    continue
                
                # GET ARTISTS with validation
                artists = music.get('artists', [])
                if not artists or len(artists) == 0:
                    logger.warning(f"⚠️ Skipping '{title}' - no artists")
                    skipped += 1
                    continue
                
                # Build artist names string
                artist_names_list = []
                for artist in artists:
                    name = artist.get('name', '').strip()
                    if name:
                        artist_names_list.append(name)
                
                if not artist_names_list:
                    logger.warning(f"⚠️ Skipping '{title}' - empty artist names")
                    skipped += 1
                    continue
                
                artist_names = ', '.join(artist_names_list)
                
                # Platform links. Spotify stays the primary one (it is the only source that
                # carries the artist page BeatLink scrapes for listeners/city/Instagram), but
                # a match ACRCloud found through a non-Spotify database is still a real artist
                # who used the beat — 39.6% of all matches were being discarded for having no
                # Spotify link at all. Deezer and YouTube Music were enabled in the ACRCloud
                # console on 2026-08-13; the ids come back under these keys.
                external_metadata = music.get('external_metadata', {})
                spotify_data = external_metadata.get('spotify', {})
                spotify_track = spotify_data.get('track', {})
                spotify_id = spotify_track.get('id', '')

                deezer_track = external_metadata.get('deezer', {}).get('track', {})
                deezer_id = str(deezer_track.get('id', '') or '')
                # YouTube is the odd one out: a bare video id under 'vid', no nested track.
                youtube_vid = str(external_metadata.get('youtube', {}).get('vid', '') or '')

                # ISRC lives in external_IDS, a separate object from external_METADATA above —
                # easy to look for in the wrong place. It is the exact identifier of this one
                # recording, which is what lets a non-Spotify match be resolved to its Spotify
                # counterpart with no ambiguity (a title+artist search cannot: it returns
                # generic-title collisions, e.g. "Levels" matching an unrelated famous track).
                isrc = str(music.get('external_ids', {}).get('isrc', '') or '').strip()

                # DEBUG: which platforms did ACRCloud actually return for this match?
                # Lets us confirm whether dropped (no-Spotify) tracks carry deezer/youtube IDs
                # (cross-platform, just unlinked to Spotify) or no external links at all.
                logger.info(
                    f"🔎 ext_metadata '{title}' by {artist_names}: "
                    f"platforms={list(external_metadata.keys())} spotify={'YES' if spotify_id else 'NO'}"
                )
                
                # Create match object
                match = {
                    'title': title,
                    'artists': artist_names,
                    'spotify_id': f"spotify:track:{spotify_id}" if spotify_id else '',
                    'deezer_id': deezer_id,
                    'youtube_vid': youtube_vid,
                    'isrc': isrc,
                    'score': score
                }
                
                matches.append(match)
            
            if skipped > 0:
                logger.info(f"⚠️ Skipped {skipped} invalid results ({low_score_skipped} due to low score)")
            
            logger.info(f"✅ Found {len(matches)} valid matches (score >= 40%)")
            
            return matches
            
        except requests.exceptions.Timeout:
            logger.error("❌ ACR Cloud timeout (30s)")
            return []
            
        except Exception as e:
            logger.error(f"❌ ACR Cloud error: {str(e)}", exc_info=True)
            return []
 
