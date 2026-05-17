import os
import logging
import requests
import time
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)


class BraveSearchService:
    """
    Service to search for Instagram profiles using Brave Search API
    
    Features:
    - Search Instagram profiles by artist name
    - Returns first result containing 'instagram.com'
    - Caching to reduce API calls
    - Rate limiting (1 req/sec as per Brave API limits)
    - Robust error handling
    
    Brave Search API:
    - Free tier: 2000 requests/month
    - Rate limit: 1 request/second
    - No credit card required
    """

    def __init__(self):
        self.api_key = os.getenv("BRAVE_SEARCH_API_KEY")
        self.base_url = "https://api.search.brave.com/res/v1/web/search"
        self.cache = {}  # Format: {artist_name: {'url': ..., 'timestamp': ...}}
        self.last_request_time = 0  # For rate limiting
        self.min_request_interval = 1.0  # 1 second between requests
        
        if self.api_key:
            logger.info("✅ Brave Search API configured")
        else:
            logger.error("❌ BRAVE_SEARCH_API_KEY not configured")

    def _rate_limit(self):
        """
        Enforce rate limit of 1 request/second
        Brave Search API free tier allows max 1 req/sec
        """
        current_time = time.time()
        time_since_last_request = current_time - self.last_request_time
        
        if time_since_last_request < self.min_request_interval:
            sleep_time = self.min_request_interval - time_since_last_request
            logger.debug(f"⏳ Rate limiting: sleeping {sleep_time:.2f}s")
            time.sleep(sleep_time)
        
        self.last_request_time = time.time()

    def _extract_instagram_url(self, search_results):
        """
        Extract first Instagram URL from Brave search results
        
        Args:
            search_results: List of search result objects from Brave API
        
        Returns:
            First Instagram URL found, or None
        """
        if not search_results:
            return None
        
        for result in search_results:
            url = result.get('url', '')
            
            # Check if URL contains instagram.com
            if 'instagram.com/' in url:
                # Skip Instagram official pages (explore, accounts, etc.)
                skip_paths = ['/explore', '/accounts', '/p/', '/tv/', '/reels/', '/stories/']
                if any(path in url for path in skip_paths):
                    continue
                
                logger.info(f"✅ Found Instagram URL: {url}")
                return url
        
        logger.warning("⚠️ No Instagram URL found in search results")
        return None

    def search_instagram(self, artist_name):
        """
        Search for Instagram profile of an artist
        
        Strategy:
        1. Check cache first
        2. Search Brave with query "{artist_name} instagram"
        3. Return FIRST result containing instagram.com
        4. No validation - systematically return first Instagram result
        
        Args:
            artist_name: Name of the artist to search
        
        Returns:
            Instagram URL (string) or None if not found
        """
        if not artist_name or not artist_name.strip():
            logger.error("❌ Empty artist name provided")
            return None
        
        artist_name = artist_name.strip()
        
        # STEP 1: Check cache
        if artist_name in self.cache:
            cached_url = self.cache[artist_name].get('url')
            cache_age = time.time() - self.cache[artist_name].get('timestamp', 0)
            
            # Cache valid for 7 days (604800 seconds)
            if cache_age < 604800:
                logger.info(f"✅ Using cached Instagram for '{artist_name}': {cached_url}")
                return cached_url
            else:
                logger.info(f"🔄 Cache expired for '{artist_name}' (age: {cache_age/86400:.1f} days)")
        
        # STEP 2: Check API key
        if not self.api_key:
            logger.error("❌ BRAVE_SEARCH_API_KEY not configured")
            return None
        
        # STEP 3: Rate limit enforcement
        self._rate_limit()
        
        # STEP 4: Build query
        query = f"{artist_name} instagram"
        encoded_query = quote_plus(query)
        
        logger.info(f"🔍 Searching Brave for: '{query}'")
        
        try:
            # STEP 5: Call Brave Search API
            response = requests.get(
                self.base_url,
                params={
                    "q": query,
                    "count": 10  # Get top 10 results
                },
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": self.api_key
                },
                timeout=10
            )
            
            # STEP 6: Handle response
            if response.status_code == 200:
                data = response.json()
                
                # Extract web results
                web_results = data.get('web', {}).get('results', [])
                
                if not web_results:
                    logger.info(f"ℹ️ No results from Brave for '{artist_name}'")
                    # Cache negative result to avoid re-searching
                    self.cache[artist_name] = {
                        'url': None,
                        'timestamp': time.time()
                    }
                    return None
                
                logger.info(f"📊 Brave returned {len(web_results)} results")
                
                # STEP 7: Extract first Instagram URL
                instagram_url = self._extract_instagram_url(web_results)
                
                # STEP 8: Cache result
                self.cache[artist_name] = {
                    'url': instagram_url,
                    'timestamp': time.time()
                }
                
                if instagram_url:
                    logger.info(f"✅ Found Instagram for '{artist_name}': {instagram_url}")
                else:
                    logger.info(f"ℹ️ No Instagram found for '{artist_name}'")
                
                return instagram_url
            
            elif response.status_code == 401:
                logger.error("❌ Brave API authentication failed - check BRAVE_SEARCH_API_KEY")
                return None
            
            elif response.status_code == 429:
                logger.error("❌ Brave API rate limit exceeded (1 req/sec or 2000/month)")
                return None
            
            else:
                logger.error(f"❌ Brave API error: {response.status_code}")
                try:
                    error_data = response.json()
                    logger.error(f"Error details: {error_data}")
                except:
                    logger.error(f"Response text: {response.text[:500]}")
                return None
        
        except requests.Timeout:
            logger.error(f"❌ Brave API timeout for '{artist_name}'")
            return None
        
        except Exception as e:
            logger.error(f"❌ Brave Search error for '{artist_name}': {str(e)}", exc_info=True)
            return None

    def batch_search_instagrams(self, artist_names):
        """
        Search Instagram profiles for multiple artists
        
        Respects rate limiting (1 req/sec) automatically
        
        Args:
            artist_names: List of artist names (strings)
        
        Returns:
            List of Instagram URLs (same order as input, None if not found)
        """
        results = []
        
        logger.info(f"🔍 Batch searching {len(artist_names)} artists...")
        
        for i, artist_name in enumerate(artist_names, 1):
            logger.info(f"[{i}/{len(artist_names)}] Searching '{artist_name}'...")
            
            instagram_url = self.search_instagram(artist_name)
            results.append(instagram_url)
        
        # Stats
        found_count = sum(1 for url in results if url is not None)
        logger.info(f"✅ Found Instagram for {found_count}/{len(artist_names)} artists")
        
        return results

    def clear_cache(self):
        """Clear all cached results"""
        self.cache = {}
        logger.info("🗑️ Cache cleared")

    def get_cache_stats(self):
        """
        Get cache statistics
        
        Returns:
            Dict with cache stats
        """
        total_entries = len(self.cache)
        cached_urls = sum(1 for entry in self.cache.values() if entry.get('url') is not None)
        cached_nulls = total_entries - cached_urls
        
        return {
            'total_entries': total_entries,
            'urls_found': cached_urls,
            'urls_not_found': cached_nulls
        }
