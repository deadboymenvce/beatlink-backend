import os
import logging
import requests
import base64
import time
 
logger = logging.getLogger(__name__)
 
 
class SpotifyService:
    """Service to enrich track metadata using Spotify API + RapidAPI scraping"""
 
    def __init__(self):
        self.client_id = os.getenv("SPOTIFY_CLIENT_ID")
        self.client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        self.rapidapi_key = os.getenv("RAPIDAPI_KEY")
        self.token = None
        self.token_expires_at = 0
        self.cache = {}  # Cache format: {artist_id: {'data': {...}, 'timestamp': 123}}
        
        if all([self.client_id, self.client_secret]):
            logger.info("✅ Spotify credentials configured")
        else:
            logger.error("❌ Spotify credentials missing")
        
        if self.rapidapi_key:
            logger.info("✅ RapidAPI key configured")
        else:
            logger.warning("⚠️ RapidAPI key missing - artist data will use fallback values")
 
    def _get_token(self):
        """Get Spotify API access token (client credentials flow)"""
        
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
                'spotify_author_ID': str,  # First artist ID
                'label': str  # Kept for compatibility
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
            
            # Get label (kept for compatibility)
            label = album.get('label', '')
            
            # Get release date (keep as string - Bubble handles it)
            release_date = album.get('release_date', '')
            
            # Build Spotify URL
            spotify_url = f"https://open.spotify.com/track/{track_id}"
            
            # Get first artist ID for scraping
            artists = data.get('artists', [])
            spotify_author_id = artists[0]['id'] if artists else None
            
            return {
                'spotify_url': spotify_url,
                'cover_url': cover_url,
                'release_date': release_date,
                'spotify_author_ID': spotify_author_id,
                'label': label
            }
            
        except Exception as e:
            logger.warning(f"⚠️ Error getting track details: {str(e)}")
            return {}
 
    def _get_artist_data_rapidapi(self, artist_id):
        """
        Fetch artist data from RapidAPI (Real-Time Spotify Data Scraper)
        
        Args:
            artist_id: Spotify artist ID
        
        Returns:
            {
                'listeners': int (NEVER None),
                'city': str or None,
                'instagram_url': str or None
            }
        """
        if not self.rapidapi_key:
            logger.warning(f"⚠️ No RapidAPI key - returning fallback for {artist_id}")
            return {'listeners': 0, 'city': None, 'instagram_url': None}
        
        url = f"https://real-time-spotify-data-scraper.p.rapidapi.com/artist_overview/?id={artist_id}"
        headers = {
            'X-RapidAPI-Key': self.rapidapi_key,
            'X-RapidAPI-Host': 'real-time-spotify-data-scraper.p.rapidapi.com'
        }
        
        # Retry logic (max 3 attempts)
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = requests.get(url, headers=headers, timeout=10)
                
                if response.status_code == 200:
                    data = response.json()
                    
                    # Parse listeners (REQUIRED - never None)
                    artist_data = data.get('data', {}).get('artist', {})
                    stats = artist_data.get('stats', {})
                    listeners = stats.get('monthlyListeners', 0)
                    
                    # Ensure listeners is int
                    if not isinstance(listeners, int):
                        listeners = 0
                    
                    # Parse city (OPTIONAL)
                    top_cities = stats.get('topCities', {}).get('items', [])
                    city = None
                    if top_cities and len(top_cities) > 0:
                        city_name = top_cities[0].get('city', '')
                        country = top_cities[0].get('country', '')
                        if city_name and country:
                            city = f"{city_name}, {country}"
                    
                    # Parse Instagram (OPTIONAL)
                    profile = artist_data.get('profile', {})
                    external_links = profile.get('externalLinks', {}).get('items', [])
                    instagram_url = None
                    for link in external_links:
                        if link.get('name') == 'INSTAGRAM':
                            instagram_url = link.get('url')
                            break
                    
                    # Parse last release date (OPTIONAL)
                    # Try to get complete date from singles (has day/month/year)
                    # Fallback to latest if singles not available (may only have year)
                    discography = artist_data.get('discography', {})
                    last_release_date = None
                    
                    # Method 1: Extract from singles (most complete)
                    singles = discography.get('singles', {})
                    singles_items = singles.get('items', [])
                    
                    if singles_items and len(singles_items) > 0:
                        releases = singles_items[0].get('releases', {})
                        releases_items = releases.get('items', [])
                        
                        if releases_items and len(releases_items) > 0:
                            date_info = releases_items[0].get('date', {})
                            
                            if date_info:
                                year = date_info.get('year')
                                month = date_info.get('month')
                                day = date_info.get('day')
                                
                                if year:
                                    # Format: YYYY-MM-DD (like Results page)
                                    if day and month:
                                        last_release_date = f"{year}-{month:02d}-{day:02d}"
                                    elif month:
                                        last_release_date = f"{year}-{month:02d}-01"
                                    else:
                                        last_release_date = f"{year}-01-01"
                    
                    # Method 2: Fallback to latest if singles failed
                    if not last_release_date:
                        latest = discography.get('latest', {})
                        date_info = latest.get('date', {})
                        
                        if date_info:
                            year = date_info.get('year')
                            month = date_info.get('month')
                            day = date_info.get('day')
                            
                            if year:
                                if day and month:
                                    last_release_date = f"{year}-{month:02d}-{day:02d}"
                                elif month:
                                    last_release_date = f"{year}-{month:02d}-01"
                                else:
                                    last_release_date = f"{year}-01-01"
                    
                    logger.info(f"✅ RapidAPI success for {artist_id}: {listeners} listeners")
                    
                    return {
                        'listeners': listeners,
                        'city': city,
                        'instagram_url': instagram_url,
                        'last_release_date': last_release_date
                    }
                
                elif response.status_code == 429:
                    # Rate limited - retry with backoff
                    if attempt < max_retries - 1:
                        wait_time = 1  # Wait 1 second
                        logger.warning(f"⚠️ RapidAPI rate limit for {artist_id}, retrying in {wait_time}s... (attempt {attempt + 1}/{max_retries})")
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"❌ RapidAPI rate limit exhausted for {artist_id}")
                        break
                
                else:
                    logger.error(f"❌ RapidAPI error {response.status_code} for {artist_id}")
                    break
            
            except requests.Timeout:
                logger.error(f"❌ RapidAPI timeout for {artist_id} (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(0.5)
                    continue
                break
            
            except Exception as e:
                logger.error(f"❌ RapidAPI exception for {artist_id}: {str(e)}")
                break
        
        # Fallback if all retries failed
        logger.warning(f"⚠️ Using fallback values for {artist_id}")
        return {'listeners': 0, 'city': None, 'instagram_url': None, 'last_release_date': None}
 
    def _get_artist_data_with_cache(self, artist_id):
        """
        Get artist data with 24h cache
        
        Cache reduces RapidAPI requests by ~50-70% for popular artists
        
        Args:
            artist_id: Spotify artist ID
        
        Returns:
            {
                'listeners': int,
                'city': str or None,
                'instagram_url': str or None
            }
        """
        # Check cache
        if artist_id in self.cache:
            cached = self.cache[artist_id]
            age = time.time() - cached['timestamp']
            
            # Cache valid for 24h (86400 seconds)
            if age < 86400:
                logger.info(f"✅ Cache hit for {artist_id} (age: {age/3600:.1f}h)")
                return cached['data']
            else:
                logger.info(f"🔄 Cache expired for {artist_id} (age: {age/3600:.1f}h)")
        
        # Cache miss or expired - fetch from RapidAPI
        logger.info(f"🌐 Fetching {artist_id} from RapidAPI...")
        data = self._get_artist_data_rapidapi(artist_id)
        
        # Store in cache
        self.cache[artist_id] = {
            'data': data,
            'timestamp': time.time()
        }
        
        return data
 
    def enrich_tracks(self, matches):
        """
        Enrich ACR Cloud matches with Spotify metadata + RapidAPI artist data
        
        Args:
            matches: List of ACR Cloud matches (pre-processed format)
                     Each match has: spotify_id, title, artists, score
        
        Returns:
            List of enriched tracks with complete metadata
        """
        if not matches:
            return []
        
        enriched = []
        artist_ids_to_fetch = []
        
        # Step 1: Get Spotify details for each match
        for match in matches:
            spotify_id = match.get('spotify_id', '')
            
            if not spotify_id:
                # No Spotify ID, return basic info with fallback values
                enriched.append({
                    'title': match['title'],
                    'artists': match['artists'],
                    'spotify_url': '',
                    'spotify_author_ID': None,
                    'cover_url': '',
                    'release_date': None,
                    'score': match['score'],
                    'listeners': 0,
                    'city': None,
                    'instagram_url': None,
                    'last_release_date': None
                })
                continue
            
            # Get Spotify details via official API
            details = self._get_track_details(spotify_id)
            
            # Build enriched track
            enriched_track = {
                'title': match['title'],
                'artists': match['artists'],
                'spotify_url': details.get('spotify_url', ''),
                'spotify_author_ID': details.get('spotify_author_ID'),
                'cover_url': details.get('cover_url', ''),
                'release_date': details.get('release_date'),
                'score': match['score']
            }
            
            enriched.append(enriched_track)
            
            # Collect artist ID for RapidAPI scraping
            artist_id = details.get('spotify_author_ID')
            if artist_id:
                artist_ids_to_fetch.append(artist_id)
        
        logger.info(f"✅ Enriched {len(enriched)} tracks with Spotify API data")
        
        # Step 2: Fetch artist data from RapidAPI (sequential with cache)
        if artist_ids_to_fetch:
            logger.info(f"🔍 Fetching {len(artist_ids_to_fetch)} artist(s) data...")
            
            scraped_data = []
            for artist_id in artist_ids_to_fetch:
                data = self._get_artist_data_with_cache(artist_id)
                scraped_data.append(data)
            
            # Step 3: Merge scraped data with enriched tracks
            scrape_index = 0
            for track in enriched:
                if track.get('spotify_author_ID'):
                    # This track has an artist - merge scraped data
                    if scrape_index < len(scraped_data):
                        scraped = scraped_data[scrape_index]
                        track['listeners'] = scraped.get('listeners', 0)
                        track['city'] = scraped.get('city')
                        track['instagram_url'] = scraped.get('instagram_url')
                        track['last_release_date'] = scraped.get('last_release_date')
                        scrape_index += 1
                    else:
                        # Fallback if index mismatch
                        track['listeners'] = 0
                        track['city'] = None
                        track['instagram_url'] = None
                        track['last_release_date'] = None
                else:
                    # No artist ID, use fallbacks
                    track['listeners'] = 0
                    track['city'] = None
                    track['instagram_url'] = None
                    track['last_release_date'] = None
            
            logger.info(f"✅ Merged artist data with {len(enriched)} track(s)")
        else:
            # No artists to fetch, add fallback values
            logger.info("ℹ️ No artists to fetch")
            for track in enriched:
                if 'listeners' not in track:
                    track['listeners'] = 0
                    track['city'] = None
                    track['instagram_url'] = None
                    track['last_release_date'] = None
        
        return enriched
