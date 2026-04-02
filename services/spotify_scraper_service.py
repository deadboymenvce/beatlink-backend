"""
Spotify Scraper Service - Extracts artist metadata from Spotify web pages
Provides: monthly_listeners, top_city, instagram_url

This service scrapes public Spotify artist pages to extract:
- monthly_listeners (REQUIRED - never returns None)
- top_city (OPTIONAL - returns None if not found)
- instagram_url (OPTIONAL - returns None if not found)

Uses async scraping for performance (parallel requests).
Includes retry logic and defensive parsing.
"""

import re
import json
import asyncio
import logging
import random
from typing import Dict, List, Optional
from bs4 import BeautifulSoup

# Try to import aiohttp, fallback to requests if not available
try:
    import aiohttp
    ASYNC_AVAILABLE = True
except ImportError:
    import requests
    ASYNC_AVAILABLE = False

logger = logging.getLogger(__name__)

# User agents for rotation (appear as normal browsers)
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15'
]


class SpotifyScraperService:
    """Service to scrape artist metadata from Spotify web pages"""
    
    def __init__(self):
        self.base_url = "https://open.spotify.com/artist/"
        logger.info("✅ SpotifyScraperService initialized")
    
    def _get_random_headers(self) -> Dict[str, str]:
        """Generate random headers to appear as a normal browser"""
        return {
            'User-Agent': random.choice(USER_AGENTS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0'
        }
    
    def _parse_listeners(self, soup: BeautifulSoup, artist_id: str) -> int:
        """
        Extract monthly listeners count from page HTML
        
        CRITICAL: This must ALWAYS return a number, never None.
        If parsing fails, returns 0 as fallback.
        
        Args:
            soup: BeautifulSoup parsed HTML
            artist_id: Artist ID for logging
            
        Returns:
            int: Monthly listeners count (0 if not found)
        """
        try:
            # Method 1: Look for text containing "monthly listeners"
            # Example: "24,567 monthly listeners" or "1.2M monthly listeners"
            text_elements = soup.find_all(string=re.compile(r'monthly listeners', re.IGNORECASE))
            
            for elem in text_elements:
                text = elem.strip()
                # Extract number before "monthly listeners"
                match = re.search(r'([\d,\.]+[KMB]?)\s*monthly listeners', text, re.IGNORECASE)
                if match:
                    number_str = match.group(1)
                    # Parse number (handle K, M, B suffixes)
                    listeners = self._parse_number_with_suffix(number_str)
                    if listeners is not None:
                        logger.info(f"✅ Found listeners for {artist_id}: {listeners:,}")
                        return listeners
            
            # Method 2: Look in data attributes
            listeners_div = soup.find('div', {'data-testid': 'monthly-listeners'})
            if listeners_div:
                text = listeners_div.get_text(strip=True)
                match = re.search(r'([\d,\.]+[KMB]?)', text)
                if match:
                    listeners = self._parse_number_with_suffix(match.group(1))
                    if listeners is not None:
                        logger.info(f"✅ Found listeners (method 2) for {artist_id}: {listeners:,}")
                        return listeners
            
            # Method 3: Search in JSON-LD structured data (if present)
            json_ld = soup.find('script', {'type': 'application/ld+json'})
            if json_ld:
                try:
                    data = json.loads(json_ld.string)
                    if isinstance(data, dict) and 'aggregateRating' in data:
                        count = data['aggregateRating'].get('ratingCount', 0)
                        if count:
                            logger.info(f"✅ Found listeners (JSON-LD) for {artist_id}: {count:,}")
                            return int(count)
                except:
                    pass
            
            # If all methods fail, log warning and return 0
            logger.warning(f"⚠️ Could not find listeners for {artist_id} - returning 0")
            return 0
            
        except Exception as e:
            logger.error(f"❌ Error parsing listeners for {artist_id}: {e}")
            return 0  # NEVER return None
    
    def _parse_number_with_suffix(self, number_str: str) -> Optional[int]:
        """
        Parse number string with K/M/B suffix
        Examples: "24.5K" -> 24500, "1.2M" -> 1200000
        """
        try:
            number_str = number_str.replace(',', '').strip()
            
            if 'K' in number_str.upper():
                return int(float(number_str.upper().replace('K', '')) * 1000)
            elif 'M' in number_str.upper():
                return int(float(number_str.upper().replace('M', '')) * 1000000)
            elif 'B' in number_str.upper():
                return int(float(number_str.upper().replace('B', '')) * 1000000000)
            else:
                return int(float(number_str))
        except:
            return None
    
    def _parse_city(self, soup: BeautifulSoup, artist_id: str) -> Optional[str]:
        """
        Extract top city from artist page
        Example: "São Paulo, BR"
        
        Returns None if not found (acceptable).
        """
        try:
            # Method 1: Look in About section for city pattern
            # Spotify typically shows cities in format "City, COUNTRY_CODE"
            city_patterns = [
                r'([A-Za-zÀ-ÿ\s\-\.]+),\s*([A-Z]{2})',  # "São Paulo, BR"
                r'([A-Za-zÀ-ÿ\s\-\.]+),\s*([A-Z]{2,3})'  # Also handles 3-letter codes
            ]
            
            # Search in all text content
            page_text = soup.get_text()
            for pattern in city_patterns:
                matches = re.finditer(pattern, page_text)
                for match in matches:
                    city = match.group(1).strip()
                    country = match.group(2).strip()
                    # Validate it's a reasonable city name (not random text)
                    if len(city) > 2 and len(country) == 2:
                        result = f"{city}, {country}"
                        logger.info(f"✅ Found city for {artist_id}: {result}")
                        return result
            
            logger.info(f"ℹ️ No city found for {artist_id} (acceptable)")
            return None
            
        except Exception as e:
            logger.error(f"❌ Error parsing city for {artist_id}: {e}")
            return None
    
    def _parse_instagram(self, soup: BeautifulSoup, artist_id: str) -> Optional[str]:
        """
        Extract Instagram URL from artist page links
        Looks in external links section
        
        Returns None if not found (acceptable).
        """
        try:
            # Find all links that point to instagram.com
            instagram_links = soup.find_all('a', href=re.compile(r'instagram\.com'))
            
            if instagram_links:
                # Return the first Instagram link found
                url = instagram_links[0].get('href')
                # Clean up URL if needed
                if url and 'instagram.com' in url:
                    logger.info(f"✅ Found Instagram for {artist_id}: {url}")
                    return url
            
            logger.info(f"ℹ️ No Instagram found for {artist_id} (acceptable)")
            return None
            
        except Exception as e:
            logger.error(f"❌ Error parsing Instagram for {artist_id}: {e}")
            return None
    
    def scrape_artist_sync(self, artist_id: str, max_retries: int = 3) -> Dict:
        """
        Scrape artist data synchronously (fallback if aiohttp not available)
        
        Args:
            artist_id: Spotify artist ID
            max_retries: Number of retry attempts if request fails
            
        Returns:
            dict with keys: listeners, city, instagram_url
        """
        import requests
        
        url = f"{self.base_url}{artist_id}"
        
        for attempt in range(max_retries):
            try:
                headers = self._get_random_headers()
                response = requests.get(url, headers=headers, timeout=10)
                
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    
                    return {
                        'listeners': self._parse_listeners(soup, artist_id),
                        'city': self._parse_city(soup, artist_id),
                        'instagram_url': self._parse_instagram(soup, artist_id)
                    }
                elif response.status_code == 429:
                    # Rate limited - wait before retry
                    wait_time = (2 ** attempt)  # Exponential backoff
                    logger.warning(f"⚠️ Rate limited for {artist_id}, waiting {wait_time}s...")
                    import time
                    time.sleep(wait_time)
                else:
                    logger.error(f"❌ HTTP {response.status_code} for {artist_id}")
                    
            except Exception as e:
                logger.error(f"❌ Error scraping {artist_id} (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    import time
                    time.sleep(1)
        
        # If all retries failed, return safe defaults
        logger.error(f"❌ All retries failed for {artist_id} - returning defaults")
        return {
            'listeners': 0,  # Safe default
            'city': None,
            'instagram_url': None
        }
    
    async def scrape_artist_async(self, session: aiohttp.ClientSession, artist_id: str, max_retries: int = 3) -> Dict:
        """
        Scrape artist data asynchronously (for parallel scraping)
        
        Args:
            session: aiohttp ClientSession
            artist_id: Spotify artist ID
            max_retries: Number of retry attempts
            
        Returns:
            dict with keys: listeners, city, instagram_url
        """
        url = f"{self.base_url}{artist_id}"
        
        for attempt in range(max_retries):
            try:
                headers = self._get_random_headers()
                
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        html = await response.text()
                        soup = BeautifulSoup(html, 'html.parser')
                        
                        return {
                            'listeners': self._parse_listeners(soup, artist_id),
                            'city': self._parse_city(soup, artist_id),
                            'instagram_url': self._parse_instagram(soup, artist_id)
                        }
                    elif response.status == 429:
                        # Rate limited - exponential backoff
                        wait_time = (2 ** attempt)
                        logger.warning(f"⚠️ Rate limited for {artist_id}, waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"❌ HTTP {response.status} for {artist_id}")
                        
            except asyncio.TimeoutError:
                logger.error(f"❌ Timeout for {artist_id} (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"❌ Error scraping {artist_id} (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
        
        # If all retries failed, return safe defaults
        logger.error(f"❌ All retries failed for {artist_id} - returning defaults")
        return {
            'listeners': 0,
            'city': None,
            'instagram_url': None
        }
    
    async def scrape_artists_parallel(self, artist_ids: List[str]) -> List[Dict]:
        """
        Scrape multiple artists in parallel for maximum performance
        
        Args:
            artist_ids: List of Spotify artist IDs
            
        Returns:
            List of dicts with scraped data (same order as input)
        """
        if not ASYNC_AVAILABLE:
            logger.warning("⚠️ aiohttp not available, falling back to synchronous scraping")
            return [self.scrape_artist_sync(aid) for aid in artist_ids]
        
        logger.info(f"🔄 Scraping {len(artist_ids)} artists in parallel...")
        
        async with aiohttp.ClientSession() as session:
            tasks = [self.scrape_artist_async(session, artist_id) for artist_id in artist_ids]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Handle any exceptions in results
            final_results = []
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"❌ Exception for artist {artist_ids[i]}: {result}")
                    final_results.append({
                        'listeners': 0,
                        'city': None,
                        'instagram_url': None
                    })
                else:
                    final_results.append(result)
            
            logger.info(f"✅ Scraped {len(final_results)} artists")
            return final_results
    
    def scrape_artists(self, artist_ids: List[str]) -> List[Dict]:
        """
        Main entry point - scrape multiple artists
        Uses async if available, otherwise falls back to sync
        
        Args:
            artist_ids: List of Spotify artist IDs
            
        Returns:
            List of dicts with keys: listeners, city, instagram_url
        """
        if not artist_ids:
            return []
        
        if ASYNC_AVAILABLE:
            # Use async scraping (parallel)
            try:
                return asyncio.run(self.scrape_artists_parallel(artist_ids))
            except Exception as e:
                logger.error(f"❌ Async scraping failed: {e}, falling back to sync")
                return [self.scrape_artist_sync(aid) for aid in artist_ids]
        else:
            # Use sync scraping (sequential)
            logger.info(f"🔄 Scraping {len(artist_ids)} artists sequentially...")
            return [self.scrape_artist_sync(aid) for aid in artist_ids]