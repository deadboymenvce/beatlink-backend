import os
import re
import logging
import requests
import base64
import time
import random
from concurrent.futures import ThreadPoolExecutor
from services.bio_parser import extract_contacts, has_any_contact
from services.key_rotation import KeyRotator

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
        # Waterfalls through the primary key, then the shared backup-account pool (see
        # services/key_rotation.py) once each one's own quota reports it's nearly spent.
        # Buys time against the cycle running dry — doesn't remove the underlying need to
        # raise a real tier eventually.
        self.key_rotator = KeyRotator('real-time-spotify-data-scraper', 'Primary', 'RAPIDAPI_KEY_SPOTIFY')
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

        # Retry logic. Bumped from 3 to 6 after 2026-07-28: production logs showed the 401
        # below is transient (same artist_id, same key, fails then succeeds seconds later
        # within the same process — a hard-dead subscription would fail every time, not
        # ~intermittently), but 3 attempts at a flat 2s gap wasn't consistently enough
        # window for it to clear. See the 401 branch for the actual backoff change.
        max_retries = 6
        for attempt in range(max_retries):
            try:
                # Rebuilt every attempt (not once, up-front): if a key switch happens
                # mid-retry (note_response fires after every response, including a
                # 401/429), the very next retry of this same call already uses the new key
                # instead of waiting for the next artist.
                used_label, used_key = self.key_rotator.current()
                if used_key is None:
                    # Every account is already known to be out of quota or unsubscribed.
                    # Retrying would be six guaranteed-identical failures per artist, which is
                    # exactly what drained the pool: ~53 requests for 10 artists, 0 results.
                    logger.error(f"❌ No usable RapidAPI account left for {artist_id} — skipping without retrying")
                    break
                headers = {
                    'X-RapidAPI-Key': used_key,
                    'X-RapidAPI-Host': 'real-time-spotify-data-scraper.p.rapidapi.com'
                }
                response = requests.get(url, headers=headers, timeout=10)
                switched = self.key_rotator.note_response(response.headers, used_label=used_label)

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
                    # Two very different things arrive as 429 and used to be treated the same:
                    #   - "you are going too fast"     → waiting genuinely helps
                    #   - "your monthly quota is gone" → waiting can never help, the answer is
                    #     identical in one second and in one hour
                    # note_response already switched keys if the quota was the reason, so a
                    # switch means retry NOW on the fresh key rather than sleeping first.
                    if switched:
                        logger.warning(f"⚠️ 429 for {artist_id} on a spent key — retrying immediately on the next account")
                        continue
                    if self.key_rotator.all_spent():
                        logger.error(f"❌ 429 for {artist_id} and every account is spent — not retrying")
                        break
                    if attempt < max_retries - 1:
                        wait_time = 1  # genuine rate limiting: a short wait is the right answer
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
                    # Same reasoning as the 429 branch: a 401 on a key whose quota just ran out
                    # is not transient, and the 31s of exponential backoff below would be spent
                    # waiting for something that cannot change.
                    if switched:
                        logger.warning(f"⚠️ 401 for {artist_id} on a spent key — retrying immediately on the next account")
                        continue
                    if self.key_rotator.all_spent():
                        logger.error(f"❌ 401 for {artist_id} and every account is spent — not retrying")
                        break
                    if attempt < max_retries - 1:
                        # Exponential backoff (1,2,4,8,16s) capped at 16s, plus up to 0.5s of
                        # jitter so the 4 parallel workers (ENRICH_MAX_WORKERS) don't all
                        # retry in lockstep and hammer the same bad window on RapidAPI's side
                        # together. Total worst-case wait across all 6 attempts: ~31s, still
                        # well inside Gunicorn's 280s request timeout (render.yaml).
                        wait_time = min(2 ** attempt, 16) + random.uniform(0, 0.5)
                        time.sleep(wait_time)
                        continue
                    else:
                        logger.error(f"❌ RapidAPI 401 exhausted for {artist_id} after {max_retries} attempts")
                        break

                elif response.status_code == 403:
                    # On RapidAPI a 403 means this account is not subscribed to THIS api. It
                    # carries no quota headers, so note_response above could not act on it and
                    # the key stayed active — every later call kept picking it and failing the
                    # same way, blocking every account behind it in the waterfall.
                    if self.key_rotator.mark_unusable(used_label, "returned 403 (account not subscribed to this API)"):
                        continue
                    logger.error(f"❌ 403 for {artist_id} and no account is left — check the RapidAPI subscriptions")
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

    # Bio-link aggregator domains — a page listing an artist's own socials/contact, so
    # anything pulled off one is trusted at "artist's own declaration" level, same as a
    # Spotify bio.
    _BIO_LINK_DOMAINS = ('linktr.ee', 'linktree.com', 'beacons.ai', 'bio.link', 'campsite.bio', 'lnk.bio', 'solo.to', 'msha.ke')

    def _fetch_biolink_contacts(self, url):
        """
        Plain GET on a Linktree/Beacons/etc page — free, not a RapidAPI call, no quota
        cost. Runs the raw HTML through the same regex contact parser already used for
        Spotify bios (it matches platform URLs/emails regardless of surrounding markup).
        Best-effort: any failure just means nothing extra was found, never raises.
        """
        try:
            r = requests.get(url, timeout=6, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code != 200:
                return {}
            return extract_contacts(r.text)
        except Exception as e:
            logger.info(f"ℹ️ Bio-link fetch failed for {url}: {e}")
            return {}

    def _search_instagram_google(self, artist_name):
        """
        Instagram fallback via RapidAPI Google Search — called only when Spotify's own
        scrape found no linked Instagram profile. Query mirrors the existing IG resolver
        convention: "{name} instagram". Budget: 1000 requests/month on this key.

        Also checks the SAME response for a bio-link page (Linktree, Beacons, …) among
        the results Google already returned — no second search, no extra RapidAPI cost.
        When one's found, fetches it (see _fetch_biolink_contacts) for whatever
        Instagram/TikTok/email it lists.

        Returns {'instagram_url': str|None, 'tiktok_url': str|None, 'email': str|None}.
        The Instagram match deliberately skips non-profile hits (/p/, /reel/, /stories/,
        /explore/…) since those are useless as a contact link.
        """
        out = {'instagram_url': None, 'tiktok_url': None, 'email': None}
        if not self.google_search_key:
            return out

        url = "https://google-search116.p.rapidapi.com/"
        headers = {
            'x-rapidapi-key': self.google_search_key,
            'x-rapidapi-host': 'google-search116.p.rapidapi.com',
        }
        params = {'query': f"{artist_name} instagram"}
        profile_re = re.compile(r'^https?://(www\.)?instagram\.com/[^/?#]+/?(\?.*)?$', re.IGNORECASE)
        biolink_re = re.compile(r'^https?://(www\.)?(' + '|'.join(re.escape(d) for d in self._BIO_LINK_DOMAINS) + r')/', re.IGNORECASE)

        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = requests.get(url, headers=headers, params=params, timeout=10)

                if response.status_code == 200:
                    data = response.json()
                    biolink_url = None
                    for result in (data.get('results') or []):
                        result_url = (result.get('url') or '').strip()
                        if not out['instagram_url'] and profile_re.match(result_url):
                            out['instagram_url'] = result_url
                            logger.info(f"✅ Google Search found Instagram for '{artist_name}': {result_url}")
                        if not biolink_url and biolink_re.match(result_url):
                            biolink_url = result_url
                    if not out['instagram_url']:
                        logger.info(f"ℹ️ No Instagram profile in Google Search results for '{artist_name}'")
                    if biolink_url:
                        logger.info(f"🔗 Bio-link page found for '{artist_name}': {biolink_url}")
                        bio_contacts = self._fetch_biolink_contacts(biolink_url)
                        if not out['instagram_url'] and bio_contacts.get('instagram'):
                            out['instagram_url'] = bio_contacts['instagram']
                        if bio_contacts.get('tiktok'):
                            out['tiktok_url'] = bio_contacts['tiktok']
                        if bio_contacts.get('email'):
                            out['email'] = bio_contacts['email']
                    return out

                elif response.status_code == 429:
                    if attempt < max_retries - 1:
                        logger.warning(f"⚠️ Google Search rate limit for '{artist_name}', retrying... (attempt {attempt + 1}/{max_retries})")
                        time.sleep(1)
                        continue
                    logger.error(f"❌ Google Search rate limit exhausted for '{artist_name}'")
                    return out

                elif response.status_code == 401:
                    logger.warning(f"⚠️ Google Search 401 for '{artist_name}' (attempt {attempt + 1}/{max_retries}) — check RAPIDAPI_KEY_GOOGLE_SEARCH / monthly quota")
                    if attempt < max_retries - 1:
                        time.sleep(2)
                        continue
                    return out

                else:
                    logger.error(f"❌ Google Search error {response.status_code} for '{artist_name}'")
                    return out

            except requests.Timeout:
                logger.error(f"❌ Google Search timeout for '{artist_name}' (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(0.5)
                    continue
                return out

            except Exception as e:
                logger.error(f"❌ Google Search exception for '{artist_name}': {str(e)}")
                return out

        return out

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
        # >= 15 monthly listeners (was 100 until 2026-08-10; a real sample showed 60 artists
        # in the 20-99 band, 36 with zero contact today — lowered to bring those into reach).
        # Below 15, "{name} instagram" mostly lands on the wrong account (small artists have
        # poorly-differentiated names), so we skip it entirely rather than hand back a
        # probably-wrong contact. Flagged instagram_via_google since this source is the
        # least reliable (the frontend uses the flag to gate reporting).
        if (not data.get('instagram_url') and artist_name
                and data.get('_rapidapi_ok') and (data.get('listeners') or 0) >= 15):
            google_result = self._search_instagram_google(artist_name)
            if google_result.get('instagram_url'):
                data['instagram_url'] = google_result['instagram_url']
                data['instagram_via_google'] = True
            # Bio-link page (Linktree, Beacons, …) found among the SAME Google results —
            # no second search. Only fills gaps, same "artist's own declaration" trust
            # level as the Spotify-bio parse above, so not flagged instagram_via_google.
            if not data.get('tiktok_url') and google_result.get('tiktok_url'):
                data['tiktok_url'] = google_result['tiktok_url']
            if not data.get('email') and google_result.get('email'):
                data['email'] = google_result['email']

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

    def _deezer_track(self, deezer_id):
        """Cover art, ISRC and artist id for a Deezer track, in one keyless call.

        Deezer publishes the ISRC itself, which matters: it means a Deezer match can be
        resolved to Spotify even when ACRCloud did not return an ISRC of its own. YouTube
        matches have no such free fallback and depend entirely on ACRCloud's.

        Strictly best-effort — a scan must never fail because Deezer was slow."""
        try:
            r = requests.get(f"https://api.deezer.com/track/{deezer_id}", timeout=4)
            if r.status_code != 200:
                return {}
            data = r.json() or {}
            album = data.get('album') or {}
            artist = data.get('artist') or {}
            return {
                'cover_url': album.get('cover_big') or album.get('cover_medium') or album.get('cover') or '',
                'isrc': str(data.get('isrc') or '').strip(),
                'artist_id': str(artist.get('id') or ''),
                'artist_name': artist.get('name') or '',
            }
        except Exception:
            return {}

    def _deezer_artist(self, artist_id):
        """Deezer's own artist figures, used only when ISRC resolution failed and there is no
        Spotify artist page to read. nb_fan is Deezer's follower count, NOT Spotify monthly
        listeners — a different metric, so it is never presented under the same label."""
        try:
            r = requests.get(f"https://api.deezer.com/artist/{artist_id}", timeout=4)
            if r.status_code != 200:
                return {}
            d = r.json() or {}
            return {
                'nb_fan': int(d.get('nb_fan') or 0),
                'nb_album': int(d.get('nb_album') or 0),
                'picture': d.get('picture_big') or d.get('picture_medium') or d.get('picture') or '',
            }
        except Exception:
            return {}

    def _spotify_track_by_isrc(self, isrc):
        """Resolve an exact recording on Spotify from its ISRC.

        Returns the same shape as _get_track_details, so a resolved alt-source match rejoins
        the normal enrichment path (artist scrape → listeners, city, Instagram) with no
        special-casing downstream. Exactness is the whole point: an ISRC identifies one
        recording, so unlike a title+artist search there is no homonym risk."""
        if not isrc:
            return {}
        token = self._get_token()
        if not token:
            return {}
        try:
            r = requests.get(
                "https://api.spotify.com/v1/search",
                headers={"Authorization": f"Bearer {token}"},
                params={"q": f"isrc:{isrc}", "type": "track", "limit": 1},
                timeout=8,
            )
            if r.status_code != 200:
                return {}
            items = ((r.json() or {}).get('tracks') or {}).get('items') or []
            if not items:
                return {}
            t = items[0]
            album = t.get('album') or {}
            images = album.get('images') or []
            cover_url = ''
            for img in images:
                if img.get('height') == 300:
                    cover_url = img.get('url', '')
                    break
            if not cover_url and images:
                cover_url = images[0].get('url', '')
            artists = t.get('artists') or []
            return {
                'spotify_url': f"https://open.spotify.com/track/{t.get('id')}",
                'cover_url': cover_url,
                'release_date': album.get('release_date', ''),
                'spotify_author_ID': artists[0]['id'] if artists else None,
                'artist_name': artists[0]['name'] if artists else None,
                'label': album.get('label', ''),
            }
        except Exception as e:
            logger.warning(f"⚠️ ISRC lookup failed for {isrc}: {e}")
            return {}

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
                # ACR Cloud matched this track through a non-Spotify database. Until
                # 2026-08-13 that meant the row was built empty and dropped downstream, which
                # accounted for 39.6% of every match we ever found. It is still a real artist
                # who used the beat, so the row is now built against whichever platform DID
                # answer, and app.py decides whether the caller is allowed to see it.
                #
                # What these rows genuinely lack is the Spotify artist page, which is where
                # listeners/city/Instagram come from — so those stay empty rather than faked,
                # and the UI labels the row with its real source instead of implying Spotify.
                source, source_url, cover_url = None, '', ''
                dz = {}
                if match.get('deezer_id'):
                    source = 'deezer'
                    source_url = f"https://www.deezer.com/track/{match['deezer_id']}"
                    dz = self._deezer_track(match['deezer_id'])
                    cover_url = dz.get('cover_url', '')
                elif match.get('youtube_vid'):
                    source = 'youtube'
                    source_url = f"https://www.youtube.com/watch?v={match['youtube_vid']}"
                    # Derived, not fetched: YouTube thumbnail URLs are addressable from the
                    # video id alone, so this costs nothing and cannot fail at scan time.
                    cover_url = f"https://img.youtube.com/vi/{match['youtube_vid']}/hqdefault.jpg"

                if not source:
                    logger.warning(f"⚠️ No platform link at all for '{match['title']}' by {match['artists']} — will be dropped")

                # ── Tier 1: resolve the exact recording on Spotify via ISRC ──────────────
                # ACRCloud's ISRC first, then Deezer's own (Deezer publishes it, so a Deezer
                # match can still resolve even when ACRCloud returned none). A hit here means
                # the row rejoins the ordinary enrichment path below and ends up with real
                # listeners, city and Instagram — everything an alt-source row otherwise lacks.
                isrc = (match.get('isrc') or '').strip() or dz.get('isrc', '')
                resolved = self._spotify_track_by_isrc(isrc) if isrc else {}

                if resolved.get('spotify_author_ID'):
                    logger.info(f"🔗 '{match['title']}' by {match['artists']} resolved to Spotify via ISRC {isrc} (found on {source})")
                    enriched.append({
                        'title': match['title'],
                        'artists': match['artists'],
                        # Kept empty on purpose: `source` stays deezer/youtube so the card keeps
                        # the branding of whatever actually identified the track, and app.py's
                        # admin gate still applies. Only the ARTIST is borrowed from Spotify.
                        'spotify_url': '',
                        'spotify_author_ID': resolved['spotify_author_ID'],
                        'cover_url': cover_url or resolved.get('cover_url', ''),
                        'release_date': resolved.get('release_date'),
                        'score': match['score'],
                        'source': source,
                        'source_url': source_url,
                        'isrc': isrc,
                    })
                    artist_ids_to_fetch.append((resolved['spotify_author_ID'], resolved.get('artist_name')))
                    continue

                # ── Tier 2: no Spotify counterpart — fall back to the platform's own data ─
                # Only Deezer has a usable public artist endpoint; a YouTube-only match has
                # nothing equivalent and stays bare.
                dz_artist = self._deezer_artist(dz['artist_id']) if dz.get('artist_id') else {}
                if isrc and source:
                    logger.info(f"↩️ '{match['title']}' has ISRC {isrc} but no Spotify counterpart — falling back to {source} data")
                elif source:
                    logger.info(f"🎯 '{match['title']}' by {match['artists']} kept via {source} (no ISRC available)")

                enriched.append({
                    'title': match['title'],
                    'artists': match['artists'],
                    'spotify_url': '',
                    'spotify_author_ID': None,
                    'cover_url': cover_url,
                    'release_date': None,
                    'score': match['score'],
                    # nb_fan is Deezer followers, not Spotify monthly listeners. It rides in the
                    # same field because it answers the same question ("how big is this artist"),
                    # and the UI tells them apart by spotify_author_ID being null — the only
                    # rows whose number did NOT come from a Spotify artist page.
                    'listeners': dz_artist.get('nb_fan', 0),
                    'city': None,
                    'instagram_url': None,
                    'twitter_url': None,
                    'tiktok_url': None,
                    'email': None,
                    'low_signal': False,
                    'last_release_date': None,
                    'artist_image': dz_artist.get('picture') or None,
                    # Deezer's album count stands in for the Spotify discography check, so a
                    # 0-release ghost is filtered on either platform rather than only one.
                    'has_discography': dz_artist.get('nb_album', 1) > 0 if dz_artist else True,
                    'source': source,
                    'source_url': source_url,
                    'isrc': isrc,
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
                'score': match['score'],
                # Stated explicitly rather than inferred from spotify_url being non-empty, so
                # every row carries its own provenance and the UI never has to guess.
                'source': 'spotify',
                'source_url': details.get('spotify_url', ''),
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
        # Deduplicated by artist_id first: a beat scan routinely matches the same artist
        # on more than one track, and artist_ids_to_fetch previously kept every duplicate,
        # each dispatched as its own fully independent fetch+retry. Two uncoordinated calls
        # for the same artist meant double the RapidAPI usage AND a real chance the two
        # calls landed different outcomes (one 401-exhausted, one not) — the same artist
        # showing complete data on one track and a ghost/ok=False on another, within the
        # SAME scan. Fetching each unique id once removes both problems and, by roughly
        # halving the calls on scans with repeat artists, statistically cuts exposure to
        # the RapidAPI 401 flakiness investigated 2026-07-28 too.
        if artist_ids_to_fetch:
            unique_pairs = list({aid: (aid, name) for aid, name in artist_ids_to_fetch}.values())
            logger.info(f"🔍 Fetching {len(unique_pairs)} unique artist(s) data ({len(artist_ids_to_fetch)} track references, parallel)...")

            with ThreadPoolExecutor(max_workers=ENRICH_MAX_WORKERS) as ex:
                scraped_list = list(ex.map(
                    lambda pair: self._get_artist_data_with_cache(pair[0], pair[1]),
                    unique_pairs
                ))
            scraped_by_id = {pair[0]: data for pair, data in zip(unique_pairs, scraped_list)}

            # Step 3: Merge scraped data with enriched tracks — looked up by the track's own
            # artist id, not a positional counter, so every track with the same artist gets
            # the identical (single) fetch result deterministically, not whichever of two
            # independent attempts happened to land first.
            for track in enriched:
                artist_id = track.get('spotify_author_ID')
                if artist_id:
                    scraped = scraped_by_id.get(artist_id)
                    if scraped:
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
                    else:
                        # Should not happen (every id in artist_ids_to_fetch has a
                        # corresponding unique_pairs entry) — defensive fallback only.
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
