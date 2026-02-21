import os
import logging
import requests
import base64

logger = logging.getLogger(__name__)


class SpotifyService:
    """Service to enrich track metadata using Spotify API"""

    def __init__(self):
        self.client_id = os.getenv("SPOTIFY_CLIENT_ID")
        self.client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        self.token = None
        self.token_expires_at = 0
        
        if all([self.client_id, self.client_secret]):
            logger.info("✅ Spotify credentials configured")
        else:
            logger.error("❌ Spotify credentials missing")

    def _get_token(self):
        """Get Spotify API access token (client credentials flow)"""
        import time
        
        # Return cached token if still valid
        if self.token and time.time() < self.token_expires_at:
            return self.token
        
        try:
            # Encode credentials
            credentials = f"{self.client_id}:{self.client_secret}"
            credentials_b64 = base64.b64encode(credentials.encode()).decode()
            
            # Request token
            response = requests.post(
                "https://accounts.spotify.com/api/token",
                headers={
                    "Authorization": f"Basic {credentials_b64}",
                    "Content-Type": "application/x-www-form-urlencoded"
                },
                data={"grant_type": "client_credentials"},
                timeout=10
            )
            
            if response.status_code != 200:
                logger.error(f"❌ Failed to get Spotify token: {response.status_code}")
                return None
            
            data = response.json()
            self.token = data.get('access_token')
            expires_in = data.get('expires_in', 3600)
            self.token_expires_at = time.time() + expires_in - 60  # Refresh 1 min early
            
            logger.info("✅ Spotify token refreshed")
            return self.token
            
        except Exception as e:
            logger.error(f"❌ Error getting Spotify token: {str(e)}")
            return None

    def _get_track_details(self, spotify_id):
        """
        Get track details from Spotify API
        
        Args:
            spotify_id: 'spotify:track:xxx' or just the track ID
        
        Returns:
            {
                'spotify_url': str,
                'cover_url': str,
                'release_date': str,
                'label': str
            }
        """
        # Extract track ID from spotify:track:xxx format
        if spotify_id.startswith('spotify:track:'):
            track_id = spotify_id.split(':')[2]
        else:
            track_id = spotify_id
        
        token = self._get_token()
        if not token:
            return {}
        
        try:
            response = requests.get(
                f"https://api.spotify.com/v1/tracks/{track_id}",
                headers={"Authorization": f"Bearer {token}"},
                params={"market": "from_token"},  # Fix 403 errors for region-restricted tracks
                timeout=10
            )
            
            if response.status_code != 200:
                logger.warning(f"⚠️ Spotify API error for track {track_id}: {response.status_code}")
                return {}
            
            data = response.json()
            
            # Extract album info
            album = data.get('album', {})
            images = album.get('images', [])
            
            # Get cover image (300x300 preferred)
            cover_url = ''
            if images:
                # Try to find 300x300 image
                for img in images:
                    if img.get('height') == 300:
                        cover_url = img.get('url', '')
                        break
                # Fallback to first image
                if not cover_url:
                    cover_url = images[0].get('url', '')
            
            # Get label
            label = album.get('label', '')
            
            # Get release date
            release_date = album.get('release_date', '')
            
            # Build Spotify URL
            spotify_url = f"https://open.spotify.com/track/{track_id}"
            
            return {
                'spotify_url': spotify_url,
                'cover_url': cover_url,
                'release_date': release_date,
                'label': label
            }
            
        except Exception as e:
            logger.warning(f"⚠️ Error getting track details: {str(e)}")
            return {}

    def enrich_tracks(self, matches):
        """
        Enrich ACR Cloud matches with Spotify metadata
        
        Args:
            matches: List of ACR Cloud matches
        
        Returns:
            List of enriched tracks
        """
        if not matches:
            return []
        
        enriched = []
        
        for match in matches:
            spotify_id = match.get('spotify_id', '')
            
            if not spotify_id:
                # No Spotify ID, return basic info
                enriched.append({
                    'title': match['title'],
                    'artists': match['artists'],
                    'spotify_url': '',
                    'cover_url': '',
                    'release_date': '',
                    'label': '',
                    'score': match['score']
                })
                continue
            
            # Get Spotify details
            details = self._get_track_details(spotify_id)
            
            enriched.append({
                'title': match['title'],
                'artists': match['artists'],
                'spotify_url': details.get('spotify_url', ''),
                'cover_url': details.get('cover_url', ''),
                'release_date': details.get('release_date', ''),
                'label': details.get('label', ''),
                'score': match['score']
            })
        
        return enriched
