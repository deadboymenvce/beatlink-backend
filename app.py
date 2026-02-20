import os
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from services.youtube_service import YouTubeService
from services.acrcloud_service import ACRCloudService
from services.spotify_service import SpotifyService

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
            'matched_songs': enriched_songs,
            'results_count': len(enriched_songs)
        }), 200
        
    except Exception as e:
        logger.error(f"❌ Unexpected error: {str(e)}", exc_info=True)
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
    
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=False)
