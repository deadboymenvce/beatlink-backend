import os
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from services.youtube_service import YouTubeService
from services.acrcloud_service import ACRCloudService
from services.spotify_service import SpotifyService
from services.brave_search_service import BraveSearchService
 
# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
 
# Initialize Flask app
app = Flask(__name__)
CORS(app)
 
# Initialize services
youtube_service = YouTubeService()
acrcloud_service = ACRCloudService()
spotify_service = SpotifyService()
brave_search_service = BraveSearchService()
 
 
@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'message': 'BeatLink API is running',
        'version': '2.0.0'
    }), 200
 
 
@app.route('/scan', methods=['POST'])
def scan_beat():
    """
    Main endpoint to scan a YouTube Type Beat
    
    Expected JSON body:
    {
        "youtube_url": "https://www.youtube.com/watch?v=..."
    }
    
    Returns:
    {
        "success": true/false,
        "uploaded_beat": {...},
        "matched_songs": [...],
        "results_count": int
    }
    """
    try:
        # Get YouTube URL from request
        data = request.get_json()
        
        if not data or 'youtube_url' not in data:
            return jsonify({
                'success': False,
                'error': 'missing_url',
                'message': 'youtube_url is required in request body'
            }), 400
        
        youtube_url = data['youtube_url']
        
        logger.info(f"📥 Scanning YouTube URL: {youtube_url}")
        
        # Step 1: Get video metadata
        logger.info("⬇️ Step 1: Getting video metadata...")
        video_info = youtube_service.get_video_info(youtube_url)
        
        if not video_info['success']:
            return jsonify({
                'success': False,
                'error': video_info['error'],
                'message': video_info['message']
            }), 400
        
        # Step 2: Download audio via Apify
        logger.info("🎵 Step 2: Downloading audio via Apify...")
        audio_path = youtube_service.download_audio(youtube_url)
        
        if not audio_path:
            return jsonify({
                'success': False,
                'error': 'download_failed',
                'message': 'Failed to download audio from YouTube'
            }), 500
        
        # Step 3: Identify audio with ACR Cloud
        logger.info("🔍 Step 3: Identifying audio with ACR Cloud...")
        matches = acrcloud_service.identify_audio(audio_path)
        
        # Clean up audio file
        youtube_service.cleanup_audio(audio_path)
        
        if not matches:
            logger.info("ℹ️ No matches found in ACR Cloud")
            return jsonify({
                'success': True,
                'uploaded_beat': {
                    'title': video_info['title'],
                    'author': video_info['author'],
                    'youtube_url': youtube_url,
                    'views_number': video_info['views'],
                    'thumbnail': video_info['thumbnail']
                },
                'matched_songs': [],
                'results_count': 0
            }), 200
        
        logger.info(f"✅ ACR Cloud found {len(matches)} matches")
        
        # Step 4: Enrich with Spotify metadata
        logger.info("🎵 Step 4: Enriching with Spotify metadata...")
        enriched_songs = spotify_service.enrich_tracks(matches)
        
        logger.info(f"✅ Enriched {len(enriched_songs)} songs with Spotify data")
        
        # FILTER: Keep only songs with complete Spotify data AND a real artist profile
        # Exclude ghost artists: listeners == 0 AND no artist_image means empty/fake Spotify account
        filtered_songs = [
            song for song in enriched_songs
            if song.get('spotify_url')
            and song.get('cover_url')
            and not (song.get('listeners', 0) == 0 and song.get('artist_image') is None)
        ]
        
        logger.info(f"🔍 Filtered from {len(enriched_songs)} to {len(filtered_songs)} complete results")
        
        # Return results
        return jsonify({
            'success': True,
            'uploaded_beat': {
                'title': video_info['title'],
                'author': video_info['author'],
                'youtube_url': youtube_url,
                'views_number': video_info['views'],
                'thumbnail': video_info['thumbnail']
            },
            'matched_songs': filtered_songs,
            'results_count': len(filtered_songs)
        }), 200
        
    except Exception as e:
        logger.error(f"❌ Unexpected error: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'internal_error',
            'message': f'Internal server error: {str(e)}'
        }), 500


@app.route('/reveal-instagram', methods=['POST'])
def reveal_instagram():
    """
    Endpoint to reveal Instagram contact for an artist
    
    This endpoint is called when user clicks "Reveal Contacts" on an artist
    with empty Instagram field.
    
    Expected JSON body:
    {
        "artist_name": "Drake"
    }
    
    Returns:
    {
        "success": true/false,
        "artist_name": "Drake",
        "instagram_url": "https://instagram.com/champagnepapi" or null,
        "source": "brave_search" or "cache"
    }
    """
    try:
        # Get artist name from request
        data = request.get_json()
        
        if not data or 'artist_name' not in data:
            return jsonify({
                'success': False,
                'error': 'missing_artist_name',
                'message': 'artist_name is required in request body'
            }), 400
        
        artist_name = data['artist_name']
        
        if not artist_name or not artist_name.strip():
            return jsonify({
                'success': False,
                'error': 'empty_artist_name',
                'message': 'artist_name cannot be empty'
            }), 400
        
        logger.info(f"🔍 Revealing Instagram for: {artist_name}")
        
        # Check if artist is in cache
        is_cached = artist_name in brave_search_service.cache
        
        # Search Instagram via Brave Search
        instagram_url = brave_search_service.search_instagram(artist_name)
        
        # Determine source
        source = "cache" if is_cached else "brave_search"
        
        # Return result
        if instagram_url:
            logger.info(f"✅ Found Instagram for '{artist_name}': {instagram_url}")
            return jsonify({
                'success': True,
                'artist_name': artist_name,
                'instagram_url': instagram_url,
                'source': source
            }), 200
        else:
            logger.info(f"ℹ️ No Instagram found for '{artist_name}'")
            return jsonify({
                'success': True,
                'artist_name': artist_name,
                'instagram_url': None,
                'source': source,
                'message': 'No Instagram profile found'
            }), 200
        
    except Exception as e:
        logger.error(f"❌ Unexpected error in reveal_instagram: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'internal_error',
            'message': f'Internal server error: {str(e)}'
        }), 500


@app.route('/reveal-instagram/batch', methods=['POST'])
def reveal_instagram_batch():
    """
    Batch endpoint to reveal Instagram for multiple artists
    
    Expected JSON body:
    {
        "artists": ["Drake", "Travis Scott", "Lil Baby"]
    }
    
    Returns:
    {
        "success": true,
        "results": [
            {
                "artist_name": "Drake",
                "instagram_url": "https://instagram.com/champagnepapi",
                "source": "brave_search"
            },
            ...
        ]
    }
    
    Note: Respects rate limiting (1 req/sec), so this endpoint may take time
    """
    try:
        # Get artist names from request
        data = request.get_json()
        
        if not data or 'artists' not in data:
            return jsonify({
                'success': False,
                'error': 'missing_artists',
                'message': 'artists array is required in request body'
            }), 400
        
        artist_names = data['artists']
        
        if not isinstance(artist_names, list):
            return jsonify({
                'success': False,
                'error': 'invalid_artists',
                'message': 'artists must be an array of strings'
            }), 400
        
        if len(artist_names) == 0:
            return jsonify({
                'success': False,
                'error': 'empty_artists',
                'message': 'artists array cannot be empty'
            }), 400
        
        logger.info(f"🔍 Batch revealing Instagram for {len(artist_names)} artists")
        
        # Search all artists
        instagram_urls = brave_search_service.batch_search_instagrams(artist_names)
        
        # Build results
        results = []
        for artist_name, instagram_url in zip(artist_names, instagram_urls):
            is_cached = artist_name in brave_search_service.cache
            results.append({
                'artist_name': artist_name,
                'instagram_url': instagram_url,
                'source': 'cache' if is_cached else 'brave_search'
            })
        
        # Stats
        found_count = sum(1 for r in results if r['instagram_url'] is not None)
        
        logger.info(f"✅ Batch reveal complete: {found_count}/{len(artist_names)} found")
        
        return jsonify({
            'success': True,
            'results': results,
            'stats': {
                'total': len(artist_names),
                'found': found_count,
                'not_found': len(artist_names) - found_count
            }
        }), 200
        
    except Exception as e:
        logger.error(f"❌ Unexpected error in batch reveal: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'error': 'internal_error',
            'message': f'Internal server error: {str(e)}'
        }), 500
 
 
if __name__ == '__main__':
    # Log environment variables status
    logger.info("🚀 Starting BeatLink Backend...")
    logger.info(f"✅ APIFY_API_TOKEN: {'Set' if os.getenv('APIFY_API_TOKEN') else 'Missing'}")
    logger.info(f"✅ YOUTUBE_API_KEY: {'Set' if os.getenv('YOUTUBE_API_KEY') else 'Missing'}")
    logger.info(f"✅ ACR Cloud credentials: {'Set' if all([os.getenv('ACR_HOST'), os.getenv('ACR_ACCESS_KEY'), os.getenv('ACR_SECRET_KEY')]) else 'Missing'}")
    logger.info(f"✅ Spotify credentials: {'Set' if all([os.getenv('SPOTIFY_CLIENT_ID'), os.getenv('SPOTIFY_CLIENT_SECRET')]) else 'Missing'}")
    logger.info(f"✅ BRAVE_SEARCH_API_KEY: {'Set' if os.getenv('BRAVE_SEARCH_API_KEY') else 'Missing'}")
    
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=False)
