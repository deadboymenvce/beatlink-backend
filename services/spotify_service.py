import os
import re
import logging
import requests
import base64
import time
from concurrent.futures import ThreadPoolExecutor
from services.bio_parser import extract_contacts, has_any_contact
from services.api_usage_tracker import record_api_usage

logger = logging.getLogger(__name__)

# Bounded concurrency for the per-artist RapidAPI calls: enough to collapse the old
# sequential-with-1s-sleep loop, low enough that the existing 429-retry logic absorbs any
# rate-limit blips rather than triggering a storm of them.
ENRICH_MAX_WORKERS = 4


class SpotifyService:
    """Service to enrich track metadata using Spotify API + RapidAPI scraping"""

    def __init__(self):
        self.client_id = os.getenv("SPOTIFY_CLIENT_ID")
        self.client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        # Use dedicated RapidAPI key for Spotify Scraper (500/month - Compte B)
        self.rapidapi_key = os.getenv("RAPIDAPI_KEY_SPOTIFY")
        # Instagram fallback when Spotify has no linked profile (1000/month budget)
        self.google_search_key = os.getenv("RAPIDAPI_KEY_GOOGLE_SEARCH")
        self.token = None
        self.token_expires_at = 0
        self.cache = {}  # Cache format: {artist_id: {'data': {...}, 'timestamp': 123}}

        if all([self.client_id, self.client_secret]):
            logger.info("✅ Spotify credentials configured")
        else:
            logger.error("❌ Spotify credentials missing")

        if self.rapidapi_key:
            logger.info("✅ RAPIDAPI_KEY_SPOTIFY configured (Spotify Scraper)")
        else:
            logger.warning("⚠️ RAPIDAPI_KEY_SPOTIFY missing - artist data will use fallback values")

        if self.google_search_key:
            logger.info("✅ RAPIDAPI_KEY_GOOGLE_SEARCH configured (Instagram fallback)")
        else:
            logger.warning("⚠️ RAPIDAPI_KEY_GOOGLE_SEARCH missing - no Instagram fallback for unlinked artists")

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
            
            # Get first artist ID + name (name is used for the Instagram Google Search fallback)
            artists = data.get('artists', [])
            spotify_author_id = artists[0]['id'] if artists else None
            artist_name = artists[0]['name'] if artists else None

            return {
                'spotify_url': spotify_url,
                'cover_url': cover_url,
                'release_date': release_date,
                'spotify_author_ID': spotify_author_id,
                'artist_name': artist_name,
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
            logger.warning(f"⚠️ No RAPIDAPI_KEY_SPOTIFY - returning fallback for {artist_id}")
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
                record_api_usage('real-time-spotify-data-scraper', response.headers)

                if response.status_code == 200:
                    data = response.json()

                    # Parse listeners (REQUIRED - never None)
                    artist_data = (data.get('data') or {}).get('artist') or {}
                    stats = artist_data.get('stats') or {}
                    listeners = stats.get('monthlyListeners') or 0

                    # Ensure listeners is int
                    if not isinstance(listeners, int):
                        listeners = 0

                    # Parse city (OPTIONAL)
                    top_cities = (stats.get('topCities') or {}).get('items') or []
                    city = None
                    if top_cities:
                        city_name = top_cities[0].get('city', '')
                        country = top_cities[0].get('country', '')
                        if city_name and country:
                            city = f"{city_name}, {country}"

                    # Parse Instagram and Twitter (OPTIONAL)
                    profile = artist_data.get('profile') or {}
                    # Biography free text — parsed later for handles the artist wrote but
                    # didn't link (many small artists put "instagram: @x" straight in the bio).
                    bio_text = (profile.get('biography') or {}).get('text')
                    external_links = (profile.get('externalLinks') or {}).get('items') or []
                    instagram_url = None
                    twitter_url = None

                    for link in external_links:
                        link_name = link.get('name')
                        if link_name == 'INSTAGRAM':
                            instagram_url = link.get('url')
                        elif link_name == 'TWITTER':
                            twitter_url = link.get('url')

                        # Stop early if we found both
                        if instagram_url and twitter_url:
                            break

                    # Parse last release date (OPTIONAL)
                    # Try to get complete date from singles (has day/month/year)
                    # Fallback to latest if singles not available (may only have year)
                    discography = artist_data.get('discography') or {}
                    last_release_date = None

                    # Method 1: Extract from singles (most complete)
                    singles_items = (discography.get('singles') or {}).get('items') or []

                    if singles_items:
                        releases_items = (singles_items[0].get('releases') or {}).get('items') or []

                        if releases_items:
                            date_info = releases_items[0].get('date') or {}

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
                        date_info = (discography.get('latest') or {}).get('date') or {}

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

                    # Parse artist profile image (OPTIONAL)
                    visuals = artist_data.get('visuals') or {}
                    avatar_image = visuals.get('avatarImage') or {}
                    sources = avatar_image.get('sources') or []
                    artist_image = None

                    if sources:
                        artist_image = sources[0].get('url', '')

                    # Detect ghost artist: no singles, no albums, no compilations
                    n_singles = (discography.get('singles') or {}).get('totalCount') or 0
                    n_albums = (discography.get('albums') or {}).get('totalCount') or 0
                    n_compilations = (discography.get('compilations') or {}).get('totalCount') or 0
                    has_discography = (n_singles + n_albums + n_compilations) > 0

                    logger.info(f"✅ RapidAPI success for {artist_id}: {listeners} listeners, discography={has_discography}")

                    return {
                        'listeners': listeners,
                        'city': city,
                        'instagram_url': instagram_url,
                        'twitter_url': twitter_url,
                        'tiktok_url': None,   # never in Spotify externalLinks — only ever from the bio
                        'email': None,
                        'bio': bio_text,
                        'last_release_date': last_release_date,
                        'artist_image': artist_image,
                        'has_discography': has_discography,
                        '_rapidapi_ok': True
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
                
                elif response.status_code == 401:
                    limit = response.headers.get('X-RateLimit-Requests-Limit', '?')
                    remaining = response.headers.get('X-RateLimit-Requests-Remaining', '?')
                    reset = response.headers.get('X-RateLimit-Requests-Reset', '?')
                    logger.warning(f"⚠️ RapidAPI 401 for {artist_id} — limit:{limit} remaining:{remaining} reset:{reset} (attempt {attempt + 1}/{max_retries})")
                    if attempt < max_retries - 1:
                        time.sleep(2)
                        continue
                    else:
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
        
        # All RapidAPI retries exhausted — return empty values flagged as failed
        # _rapidapi_ok=False means the filter in app.py will NOT treat this as a confirmed ghost artist
        logger.warning(f"⚠️ RapidAPI unavailable for {artist_id}, returning partial result")
        return {
            'listeners': 0,
            'city': None,
            'instagram_url': None,
            'twitter_url': None,
            'tiktok_url': None,
            'email': None,
            'bio': None,
            'last_release_date': None,
            'artist_image': None,
            '_rapidapi_ok': False
        }

    def _search_instagram_google(self, artist_name):
        """
        Instagram fallback via RapidAPI Google Search — called only when Spotify's own
        scrape found no linked Instagram profile. Query mirrors the existing IG resolver
        convention: "{name} instagram". Budget: 1000 requests/month on this key.

        Returns a profile URL (instagram.com/<username>) or None — deliberately skips
        non-profile hits (/p/, /reel/, /stories/, /explore/…) since those are useless as
        a contact link.
        """
        if not self.google_search_key:
            return None

        url = "https://google-search116.p.rapidapi.com/"
        headers = {
            'x-rapidapi-key': self.google_search_key,
            'x-rapidapi-host': 'google-search116.p.rapidapi.com',
        }
        params = {'query': f"{artist_name} instagram"}
        profile_re = re.compile(r'^https?://(www\.)?instagram\.com/[^/?#]+/?(\?.*)?$', re.IGNORECASE)

        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = requests.get(url, headers=headers, params=params, timeout=10)

                if response.status_code == 200:
                    data = response.json()
                    for result in (data.get('results') or []):
                        result_url = (result.get('url') or '').strip()
                        if profile_re.match(result_url):
                            logger.info(f"✅ Google Search found Instagram for '{artist_name}': {result_url}")
                            return result_url
                    logger.info(f"ℹ️ No Instagram profile in Google Search results for '{artist_name}'")
                    return None

                elif response.status_code == 429:
                    if attempt < max_retries - 1:
                        logger.warning(f"⚠️ Google Search rate limit for '{artist_name}', retrying... (attempt {attempt + 1}/{max_retries})")
                        time.sleep(1)
                        continue
                    logger.error(f"❌ Google Search rate limit exhausted for '{artist_name}'")
                    return None

                elif response.status_code == 401:
                    logger.warning(f"⚠️ Google Search 401 for '{artist_name}' (attempt {attempt + 1}/{max_retries}) — check RAPIDAPI_KEY_GOOGLE_SEARCH / monthly quota")
                    if attempt < max_retries - 1:
                        time.sleep(2)
                        continue
                    return None

                else:
                    logger.error(f"❌ Google Search error {response.status_code} for '{artist_name}'")
                    return None

            except requests.Timeout:
                logger.error(f"❌ Google Search timeout for '{artist_name}' (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(0.5)
                    continue
                return None

            except Exception as e:
                logger.error(f"❌ Google Search exception for '{artist_name}': {str(e)}")
                return None

        return None

    def _get_artist_data_with_cache(self, artist_id, artist_name=None):
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

        # Bio-declared contacts — many artists write their handles straight into the bio
        # without linking them ("instagram: @x", "x on ig", "tiktok.com/@x"…). This is the
        # artist's own declaration → reliable, treated like a linked profile (NOT flagged
        # instagram_via_google). It's also the ONLY source of TikTok, since Spotify's
        # externalLinks never carry TikTok. Runs for every artist without a linked handle.
        contacts = extract_contacts(data.get('bio'))
        if not data.get('instagram_url') and contacts['instagram']:
            data['instagram_url'] = contacts['instagram']
        if not data.get('tiktok_url') and contacts['tiktok']:
            data['tiktok_url'] = contacts['tiktok']
        if not data.get('twitter_url') and contacts['twitter']:
            data['twitter_url'] = contacts['twitter']
        if not data.get('email') and contacts['email']:
            data['email'] = contacts['email']

        # Name-based Instagram search — LAST resort, and ONLY for artists we've CONFIRMED have
        # >= 100 monthly listeners. Below that, "{name} instagram" mostly lands on the wrong
        # account (small artists have poorly-differentiated names), so we skip it entirely
        # rather than hand back a probably-wrong contact. Flagged instagram_via_google since
        # this source is the least reliable (the frontend uses the flag to gate reporting).
        if (not data.get('instagram_url') and artist_name
                and data.get('_rapidapi_ok') and (data.get('listeners') or 0) >= 100):
            google_ig = self._search_instagram_google(artist_name)
            if google_ig:
                data['instagram_url'] = google_ig
                data['instagram_via_google'] = True

        # low_signal: a CONFIRMED sub-100 artist with NO reachable contact anywhere (no linked
        # or bio Instagram/Twitter/TikTok, no bio email). Not dropped — kept and flagged (shown
        # as the existing "theft suspected" badge, driven by a SEPARATE column the availability
        # cron never touches). Only when RapidAPI actually succeeded: a failed lookup means
        # "unknown", not "tiny", so we never tag on an API failure.
        has_contact = bool(data.get('instagram_url') or data.get('tiktok_url')
                           or data.get('twitter_url') or data.get('email'))
        data['low_signal'] = bool(
            data.get('_rapidapi_ok') and (data.get('listeners') or 0) < 100 and not has_contact
        )

        # Cache real RapidAPI successes, OR a partial failure that still landed an Instagram
        # via the name search — either way there's something worth reusing for the next user
        # who scans this same artist. A total dud (RapidAPI failed AND nothing found) is never
        # cached, so it gets retried next time.
        if data.get('_rapidapi_ok', False) or data.get('instagram_url'):
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

        # Step 1: Get Spotify track details — in parallel across matches (order preserved by
        # executor.map, which the index-aligned merge below relies on). Pre-fetch the token
        # once so the threads all reuse it instead of racing to refresh it.
        self._get_token()
        with ThreadPoolExecutor(max_workers=ENRICH_MAX_WORKERS) as ex:
            details_list = list(ex.map(
                lambda m: self._get_track_details(m['spotify_id']) if m.get('spotify_id') else None,
                matches
            ))

        for match, details in zip(matches, details_list):
            if details is None:
                # ACR Cloud matched this track via a non-Spotify database (Gracenote, Deezer…)
                # Without a Spotify track ID we cannot get artist data or cover art → will be filtered
                logger.warning(f"⚠️ No Spotify ID for '{match['title']}' by {match['artists']} — will be dropped (ACR match without Spotify link)")
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
                    'twitter_url': None,
                    'tiktok_url': None,
                    'email': None,
                    'low_signal': False,
                    'last_release_date': None,
                    'artist_image': None
                })
                continue

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

            # Collect artist ID + name for RapidAPI scraping (name feeds the Instagram
            # Google Search fallback — from Spotify's official API, not the raw ACR
            # Cloud credit string, which can hold multiple/feat. artists).
            artist_id = details.get('spotify_author_ID')
            if artist_id:
                artist_ids_to_fetch.append((artist_id, details.get('artist_name')))

        logger.info(f"✅ Enriched {len(enriched)} tracks with Spotify API data")

        # Step 2: Fetch artist data from RapidAPI — in parallel (bounded), cache-backed.
        # Replaces the old sequential loop with a fixed 1s sleep between each artist; the
        # existing 429-retry/backoff inside _get_artist_data_rapidapi is the rate-limit
        # safety net. Order preserved by executor.map for the index-aligned merge below.
        if artist_ids_to_fetch:
            logger.info(f"🔍 Fetching {len(artist_ids_to_fetch)} artist(s) data (parallel)...")

            with ThreadPoolExecutor(max_workers=ENRICH_MAX_WORKERS) as ex:
                scraped_data = list(ex.map(
                    lambda pair: self._get_artist_data_with_cache(pair[0], pair[1]),
                    artist_ids_to_fetch
                ))

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
                        track['twitter_url'] = scraped.get('twitter_url')
                        track['tiktok_url'] = scraped.get('tiktok_url')
                        track['email'] = scraped.get('email')
                        track['low_signal'] = scraped.get('low_signal', False)
                        track['last_release_date'] = scraped.get('last_release_date')
                        track['artist_image'] = scraped.get('artist_image')
                        track['has_discography'] = scraped.get('has_discography', True)
                        track['_rapidapi_ok'] = scraped.get('_rapidapi_ok', False)
                        track['instagram_via_google'] = scraped.get('instagram_via_google', False)
                        scrape_index += 1
                    else:
                        # Fallback if index mismatch
                        track['listeners'] = 0
                        track['city'] = None
                        track['instagram_url'] = None
                        track['twitter_url'] = None
                        track['tiktok_url'] = None
                        track['email'] = None
                        track['low_signal'] = False
                        track['last_release_date'] = None
                        track['artist_image'] = None
                else:
                    # No artist ID, use fallbacks
                    track['listeners'] = 0
                    track['city'] = None
                    track['instagram_url'] = None
                    track['twitter_url'] = None
                    track['tiktok_url'] = None
                    track['email'] = None
                    track['low_signal'] = False
                    track['last_release_date'] = None
                    track['artist_image'] = None
            
            logger.info(f"✅ Merged artist data with {len(enriched)} track(s)")
        else:
            # No artists to fetch, add fallback values
            logger.info("ℹ️ No artists to fetch")
            for track in enriched:
                if 'listeners' not in track:
                    track['listeners'] = 0
                    track['city'] = None
                    track['instagram_url'] = None
                    track['twitter_url'] = None
                    track['tiktok_url'] = None
                    track['email'] = None
                    track['low_signal'] = False
                    track['last_release_date'] = None
                    track['artist_image'] = None

        return enriched
