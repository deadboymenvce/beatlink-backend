"""
Spotify Service - Handles all Spotify API interactions and web scraping
Enriches track metadata from ACRCloud with Spotify data + artist scraping
"""

import os
import logging
import base64
import requests
from typing import List, Dict, Optional
from .spotify_scraper_service import SpotifyScraperService

logger = logging.getLogger(__name__)


class SpotifyService:
    """Service to interact with Spotify Web API and scrape artist data"""
    
    def __init__(self):
        self.client_id = os.getenv('SPOTIFY_CLIENT_ID')
        self.client_secret = os.getenv('SPOTIFY_CLIENT_SECRET')
        self.access_token = None
        self.scraper = SpotifyScraperService()
        
        if not self.client_id or not self.client_secret:
            logger.error("❌ Spotify credentials not found in environment variables")
            raise ValueError("Missing Spotify credentials")
        
        logger.info("✅ SpotifyService initialized")
        self._get_access_token()
    
    def _get_access_token(self):
        """Get Spotify API access token using Client Credentials flow"""
        try:
            auth_string = f"{self.client_id}:{self.client_secret}"
            auth_bytes = auth_string.encode("utf-8")
            auth_base64 = base64.b64encode(auth_bytes).decode("utf-8")
            
            url = "https://accounts.spotify.com/api/token"
            headers = {
                "Authorization": f"Basic {auth_base64}",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            data = {"grant_type": "client_credentials"}
            
            response = requests.post(url, headers=headers, data=data)
            response.raise_for_status()
            
            json_result = response.json()
            self.access_token = json_result["access_token"]
            logger.info("✅ Spotify access token obtained")
            
        except Exception as e:
            logger.error(f"❌ Failed to get Spotify access token: {e}")
            raise
    
    def _get_auth_header(self) -> Dict[str, str]:
        """Get authorization header for Spotify API requests"""
        return {"Authorization": f"Bearer {self.access_token}"}
    
    def get_track(self, track_id: str) -> Optional[Dict]:
        """
        Get track information from Spotify API
        
        Args:
            track_id: Spotify track ID
            
        Returns:
            Track data dict or None if not found
        """
        try:
            url = f"https://api.spotify.com/v1/tracks/{track_id}"
            headers = self._get_auth_header()
            
            response = requests.get(url, headers=headers)
            
            if response.status_code == 401:
                # Token expired, get new one
                logger.info("🔄 Access token expired, refreshing...")
                self._get_access_token()
                headers = self._get_auth_header()
                response = requests.get(url, headers=headers)
            
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(f"⚠️ Track {track_id} not found (HTTP {response.status_code})")
                return None
                
        except Exception as e:
            logger.error(f"❌ Error fetching track {track_id}: {e}")
            return None
    
    def enrich_tracks(self, acr_matches: List[Dict]) -> List[Dict]:
        """
        Enrich ACRCloud matches with Spotify metadata + artist scraping
        
        This is the main method that:
        1. Fetches track data from Spotify API
        2. Extracts artist IDs
        3. Scrapes artist pages for listeners, city, Instagram
        4. Merges all data together
        
        Args:
            acr_matches: List of matches from ACRCloud
            
        Returns:
            List of enriched track dicts with all metadata
        """
        if not acr_matches:
            return []
        
        logger.info(f"🎵 Enriching {len(acr_matches)} tracks with Spotify data...")
        
        enriched_songs = []
        artist_ids_to_scrape = []
        
        # Step 1: Fetch track data from Spotify API
        for match in acr_matches:
            try:
                # Extract Spotify track ID from ACRCloud match
                spotify_data = match.get('spotify', {})
                track_data = spotify_data.get('track', {})
                track_id = track_data.get('id')
                
                if not track_id:
                    logger.warning("⚠️ No Spotify track ID in ACRCloud match")
                    continue
                
                # Get full track info from Spotify API
                full_track = self.get_track(track_id)
                
                if not full_track:
                    continue
                
                # Extract basic track info
                artists_list = full_track.get('artists', [])
                artist_names = ', '.join([artist['name'] for artist in artists_list])
                
                # Get first artist ID for scraping
                spotify_author_id = artists_list[0]['id'] if artists_list else None
                
                # Get album info
                album = full_track.get('album', {})
                images = album.get('images', [])
                cover_url = images[0]['url'] if images else None
                
                # Build enriched song object (without scraped data yet)
                enriched_song = {
                    'title': full_track.get('name'),
                    'artists': artist_names,
                    'spotify_url': full_track.get('external_urls', {}).get('spotify'),
                    'spotify_author_ID': spotify_author_id,  # NEW
                    'cover_url': cover_url,
                    'release_date': album.get('release_date'),
                    'score': match.get('score', 0)
                }
                
                enriched_songs.append(enriched_song)
                
                # Collect artist ID for scraping
                if spotify_author_id:
                    artist_ids_to_scrape.append(spotify_author_id)
                
            except Exception as e:
                logger.error(f"❌ Error enriching track: {e}")
                continue
        
        logger.info(f"✅ Enriched {len(enriched_songs)} tracks with Spotify API data")
        
        # Step 2: Scrape all artist pages in parallel
        if artist_ids_to_scrape:
            logger.info(f"🔍 Scraping {len(artist_ids_to_scrape)} artist pages...")
            
            try:
                scraped_data = self.scraper.scrape_artists(artist_ids_to_scrape)
                
                # Step 3: Merge scraped data with enriched songs
                for i, song in enumerate(enriched_songs):
                    if i < len(scraped_data):
                        scraped = scraped_data[i]
                        song['listeners'] = scraped.get('listeners', 0)  # NEW
                        song['city'] = scraped.get('city')  # NEW (can be None)
                        song['instagram_url'] = scraped.get('instagram_url')  # NEW (can be None)
                    else:
                        # Fallback if scraping failed for this artist
                        song['listeners'] = 0
                        song['city'] = None
                        song['instagram_url'] = None
                
                logger.info(f"✅ Merged scraped data with {len(enriched_songs)} tracks")
                
            except Exception as e:
                logger.error(f"❌ Error during artist scraping: {e}")
                # If scraping completely fails, add default values
                for song in enriched_songs:
                    song['listeners'] = 0
                    song['city'] = None
                    song['instagram_url'] = None
        else:
            # No artists to scrape, add default values
            for song in enriched_songs:
                song['listeners'] = 0
                song['city'] = None
                song['instagram_url'] = None
        
        return enriched_songs
