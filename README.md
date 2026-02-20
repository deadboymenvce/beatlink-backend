# 🎵 BeatLink Backend API

Backend API pour BeatLink.io - Le SaaS de prospection clients pour beatmakers.

## 🏗️ Architecture

**Frontend :** Bubble.io  
**Backend :** Python Flask sur Render  
**APIs utilisées :**
- YouTube Data API v3 (métadonnées vidéo)
- Apify Actor (téléchargement audio YouTube)
- ACR Cloud (audio fingerprinting)
- Spotify API (enrichissement métadonnées)

## 📋 Endpoints

### `GET /health`
Health check endpoint

**Response:**
```json
{
  "status": "healthy",
  "message": "BeatLink API is running",
  "version": "2.0.0"
}
```

### `POST /scan`
Scanner un Type Beat YouTube et trouver les tracks Spotify qui l'utilisent

**Request:**
```json
{
  "youtube_url": "https://www.youtube.com/watch?v=..."
}
```

**Response Success:**
```json
{
  "success": true,
  "uploaded_beat": {
    "title": "Type Beat Title",
    "author": "Producer Name",
    "youtube_url": "https://...",
    "views_number": 123456,
    "thumbnail": "https://..."
  },
  "matched_songs": [
    {
      "title": "Song Title",
      "artists": "Artist Name",
      "spotify_url": "https://open.spotify.com/track/...",
      "cover_url": "https://...",
      "release_date": "2024-01-01",
      "label": "Label Name",
      "score": 92.5
    }
  ],
  "results_count": 1
}
```

**Response Error:**
```json
{
  "success": false,
  "error": "error_code",
  "message": "Error description"
}
```

## ⚙️ Variables d'environnement

```
APIFY_API_TOKEN=apify_api_...
YOUTUBE_API_KEY=AIzaSy...
ACR_HOST=identify-eu-west-1.acrcloud.com
ACR_ACCESS_KEY=...
ACR_SECRET_KEY=...
SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...
```

## 🚀 Déploiement

### 1. GitHub

```bash
# Créer le repo sur GitHub
# Upload tous les fichiers
```

### 2. Render

```bash
# Créer un nouveau Web Service
# Connecter le repo GitHub
# Configurer les 7 variables d'environnement
# Déployer
```

### 3. Test

```bash
curl https://beatlink-api.onrender.com/health

curl -X POST https://beatlink-api.onrender.com/scan \
  -H "Content-Type: application/json" \
  -d '{"youtube_url": "https://www.youtube.com/watch?v=..."}'
```

## 🎯 Optimisations

Le backend est optimisé pour minimiser les coûts Apify :
- Audio only (pas de vidéo)
- Qualité basse 128kbps (suffisant pour ACR Cloud)
- Extraction de 30 secondes seulement (optimisation coûts)

## 📊 Coûts estimés

**Apify :** ~$0.012-0.018 par scan  
**ACR Cloud :** Free tier (50-100 scans/jour)  
**Spotify API :** Gratuit  
**YouTube Data API :** Gratuit (10k requêtes/jour)

## 📝 License

Proprietary - BeatLink.io
