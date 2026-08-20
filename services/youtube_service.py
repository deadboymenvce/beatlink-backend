import os
import time
import logging
import tempfile
import subprocess
import re
import requests

from services.api_usage_tracker import record_api_usage
from services.key_rotation import KeyRotator

logger = logging.getLogger(__name__)

# Le fournisseur decoupe lui-meme, cote serveur. On ne demande donc que 30 secondes de son
# a partir de la 30e, au lieu de telecharger 4 minutes pour en jeter 95%. Demarrer a 30s et
# non a 0 evite les intros et les tags vocaux, qui degradent l'empreinte ACRCloud.
#
# Le fichier rendu fait ~730 Ko en Ogg/Opus 64 kbps. Mesure du 20/08 sur b6sEej3QfyU :
# 115,0 s et 2 043 233 octets sans decoupage, 40,0 s et 732 722 octets avec. Le decoupage
# est donc bien applique cote serveur, meme si le champ "size" de la reponse, lui, renvoie
# la taille du morceau entier dans les deux cas.
TRIM_START_S = 30
TRIM_DURATION_S = 30
AUDIO_QUALITY = 251          # itag YouTube : opus 64 kbps

# Le lien renvoye repond 404 tant que le fichier n'est pas fabrique, exactement comme chez
# le fournisseur precedent. La difference est le DELAI : l'endpoint rend la main en ~18 s
# et le fichier est disponible ~2 s plus tard, soit 20 s en tout. Mesure du 20/08 sur
# 6GmJfKJoFm0, une video jamais demandee a ce fournisseur, donc sans cache possible.
#
# Chez le fournisseur precedent la meme mesure donnait ~300 s, ce qui consommait a lui seul
# la totalite du budget d'un scan et ne laissait rien a ACRCloud ni a Spotify. C'etait la
# cause de l'incident des 19 et 20 aout, pas un bug de ce fichier.
#
# 90 s de budget, soit 45 fois le delai constate. Il ne s'agit pas d'esperer mieux : si le
# fichier n'est pas la au bout de 90 s, c'est que le fournisseur est en panne, et attendre
# davantage ne fait que retarder l'echec.
PREP_BUDGET_S = 90
POLL_EVERY_S = 3

def _iso8601_seconds(duration):
    """PT4M13S → 253. Returns 0 for anything unparseable, which drops the video rather
    than letting an unknown length through as if it were a full beat."""
    m = re.match(r'^P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$', duration or '')
    if not m:
        return 0
    d, h, mi, sec = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


class YouTubeService:
    """
    YouTube service using:
    1. YouTube Data API v3 for metadata
    2. RapidAPI (youtube-mp3-2025) for M4A download
    """

    def __init__(self):
        self.temp_dir = tempfile.gettempdir()
        self.api_key = os.getenv("YOUTUBE_API_KEY")
        # RapidAPI key (account-wide, same key covers both hosts below). Set RAPIDAPI_KEY on Render.
        self.rapidapi_key = os.getenv("RAPIDAPI_KEY")
        # youtube-video-fast-downloader-24-7. Une seule requete par scan : l'endpoint
        # /download_audio rend un lien, et les sondages de ce lien tapent directement le CDN
        # du fournisseur, hors marketplace, donc ne consomment pas de quota.
        #
        # Le fournisseur precedent (youtube-mp3-audio-video-downloader) a ete retire. Ce
        # n'etait pas une panne passagere : sa route synchrone /download-m4a est classee
        # Deprecated chez lui et repond 524 apres 125 s, et sa route asynchrone mettait
        # jusqu'a 300 s a fabriquer le fichier alors qu'un scan entier n'a que 300 s. Les
        # deux fournisseurs partagent d'ailleurs la meme infrastructure (memes hotes CDN
        # s7.12388101.xyz et s7.postixx.de, meme format de hash) : c'est le meme service
        # revendu deux fois. Ce qui change ici, et qui seul justifie la bascule, c'est que
        # cette fiche expose trim_start_time / trim_duration, donc un fragment a fabriquer
        # au lieu d'un morceau entier.
        self.rapidapi_sync_host = "youtube-video-fast-downloader-24-7.p.rapidapi.com"
        # La cascade de comptes est inchangee. L'abonnement a cette API n'existe pour
        # l'instant que sur le compte 777asthma (RAPIDAPI_KEY_POOL_2) : le slot 1 repondra
        # donc 403, le rotateur le retirera et passera au suivant tout seul, sans qu'on ait
        # a coder le cas. Abonner les autres comptes plus tard ne demandera aucun code.
        self.sync_key_rotator = KeyRotator('youtube-video-fast-downloader-24-7',
                                           'prodconnect512@gmail.com', 'RAPIDAPI_KEY')

        if self.api_key:
            logger.info("✅ YOUTUBE_API_KEY configured")
        else:
            logger.warning("⚠️ YOUTUBE_API_KEY not set")
        
        if self.rapidapi_key:
            logger.info("✅ RAPIDAPI_KEY configured (YouTube Downloader)")
        else:
            logger.error("❌ RAPIDAPI_KEY not set")
        
        logger.info(f"📁 Temp directory: {self.temp_dir}")
        logger.info(f"🎬 Using RapidAPI: {self.rapidapi_sync_host} (trim {TRIM_START_S}s+{TRIM_DURATION_S}s, quality {AUDIO_QUALITY})")

    def _extract_video_id(self, url):
        """Extract video ID from various YouTube URL formats"""
        patterns = [
            r'(?:v=|\/)([0-9A-Za-z_-]{11})',
            r'youtu\.be\/([0-9A-Za-z_-]{11})',
            r'^([0-9A-Za-z_-]{11})$'
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None

    def search_type_beats(self, niche, published_after, min_views=30000, want=10):
        """Find the most-watched "{niche} type beat" videos published since a date.

        Used to auto-fill the scanner's suggestions for a niche nobody has curated by
        hand. Ordered by view count, never by date: views are the only signal available
        up front that a beat had enough reach to have real buyers behind it.

        QUOTA — the reason this must never be called per user. search.list costs 100 units
        against a 10,000/day quota, while the videos.list below and every scan's metadata
        lookup cost 1. One careless call per session would starve the scanner itself, so
        the caller is expected to cache the result per niche, not per user.

        Two calls on purpose: search.list won't return view counts, so the ids it gives
        back are re-read through videos.list (1 unit for up to 50) to get statistics and
        duration. That is also what lets Shorts be dropped — a 40-second clip is not a
        beat anyone bought.
        """
        if not self.api_key:
            return {'success': False, 'error': 'missing_api_key', 'message': 'YOUTUBE_API_KEY non configurée'}

        query = f"{niche} type beat".strip()
        try:
            search = requests.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={
                    "part": "id",
                    "type": "video",
                    "q": query,
                    "order": "viewCount",
                    "publishedAfter": published_after,
                    "maxResults": 50,
                    "key": self.api_key,
                },
                timeout=20,
            )
            record_api_usage('youtube-data-api', search.headers)
            if search.status_code != 200:
                logger.error(f"❌ YouTube search failed ({search.status_code}) for '{query}': {search.text[:300]}")
                return {'success': False, 'error': 'api_error', 'message': f'YouTube search error: {search.status_code}'}

            ids = [i['id']['videoId'] for i in search.json().get('items', []) if i.get('id', {}).get('videoId')]
            if not ids:
                return {'success': True, 'query': query, 'videos': []}

            details = requests.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={
                    "id": ",".join(ids[:50]),
                    "part": "snippet,statistics,contentDetails",
                    "key": self.api_key,
                },
                timeout=20,
            )
            record_api_usage('youtube-data-api', details.headers)
            if details.status_code != 200:
                logger.error(f"❌ YouTube videos lookup failed ({details.status_code})")
                return {'success': False, 'error': 'api_error', 'message': f'YouTube API error: {details.status_code}'}

            out = []
            for item in details.json().get('items', []):
                stats = item.get('statistics', {})
                snip = item.get('snippet', {})
                views = int(stats.get('viewCount', 0) or 0)
                if views < min_views:
                    continue
                if _iso8601_seconds(item.get('contentDetails', {}).get('duration', '')) < 90:
                    continue  # Short / teaser, not a beat
                thumbs = snip.get('thumbnails', {})
                thumb = (thumbs.get('high') or thumbs.get('medium') or thumbs.get('default') or {}).get('url')
                out.append({
                    'video_id': item['id'],
                    'youtube_url': f"https://www.youtube.com/watch?v={item['id']}",
                    'title': snip.get('title'),
                    'author': snip.get('channelTitle'),
                    'thumbnail_url': thumb,
                    'views_number': views,
                    'published_at': snip.get('publishedAt'),
                })

            out.sort(key=lambda v: v['views_number'], reverse=True)
            logger.info(f"🔎 '{query}' → {len(out)} usable of {len(ids)} results (min {min_views} views)")
            return {'success': True, 'query': query, 'videos': out[:want]}

        except Exception as e:
            logger.error(f"❌ YouTube search error for '{query}': {e}", exc_info=True)
            return {'success': False, 'error': 'search_failed', 'message': str(e)}


    def get_video_info(self, youtube_url):
        """
        Fetch video metadata using YouTube Data API v3
        """
        video_id = self._extract_video_id(youtube_url)
        
        if not video_id:
            return {
                'success': False,
                'error': 'invalid_url',
                'message': 'URL YouTube invalide'
            }
        
        if not self.api_key:
            return {
                'success': False,
                'error': 'missing_api_key',
                'message': 'YOUTUBE_API_KEY non configurée'
            }
        
        try:
            logger.info(f"📋 Fetching metadata for video {video_id}...")
            
            response = requests.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={
                    "id": video_id,
                    "part": "snippet,statistics",
                    "key": self.api_key
                },
                timeout=15
            )
            
            if response.status_code != 200:
                logger.error(f"❌ YouTube API error: {response.status_code}")
                try:
                    error_data = response.json()
                    logger.error(f"Error details: {error_data}")
                except:
                    logger.error(f"Response text: {response.text[:500]}")
                return {
                    'success': False,
                    'error': 'api_error',
                    'message': f'YouTube API error: {response.status_code}'
                }
            
            data = response.json()
            items = data.get('items', [])
            
            if not items:
                return {
                    'success': False,
                    'error': 'video_unavailable',
                    'message': 'Vidéo introuvable, privée ou supprimée'
                }
            
            snippet = items[0].get('snippet', {})
            statistics = items[0].get('statistics', {})
            thumbnails = snippet.get('thumbnails', {})
            
            # Get best quality thumbnail
            thumbnail = ''
            for quality in ('maxres', 'high', 'medium', 'default'):
                if quality in thumbnails:
                    thumbnail = thumbnails[quality].get('url', '')
                    break
            
            title = snippet.get('title', 'Unknown Title')
            logger.info(f"✅ Metadata retrieved: {title[:60]}")
            
            return {
                'success': True,
                'title': title,
                'author': snippet.get('channelTitle', 'Unknown Author'),
                'views': int(statistics.get('viewCount', 0)),
                'thumbnail': thumbnail,
                'duration': 0
            }
            
        except requests.exceptions.Timeout:
            logger.error("❌ YouTube API timeout (15s)")
            return {
                'success': False,
                'error': 'timeout',
                'message': 'YouTube API timeout'
            }
            
        except Exception as e:
            logger.error(f"❌ Error fetching video info: {str(e)}", exc_info=True)
            return {
                'success': False,
                'error': 'unknown_error',
                'message': f'Error: {str(e)}'
            }

    def _fetch_raw_via_sync_api(self, video_id, raw_path):
        """Une requete RapidAPI, puis le fichier deja decoupe a 30 secondes.

        GET /download_audio/{id}?quality=251&trim_start_time=30&trim_duration=30 bloque
        ~18 s puis repond :

            {"size": ..., "bitrate": 64000, "type": "audio",
             "mime": "audio/m4a; codecs=...",          <- mensonge, le fichier est de l'Opus
             "file":          "https://s2-audio.<hote>/dl_<id>-<hash>.opus",
             "reserved_file": "https://s7.<miroir>/dl_<id>-<hash>.opus"}

        Le champ "mime" annonce du m4a alors que l'extension et le contenu reel sont de
        l'Ogg/Opus (en-tete "OggS", verifie a ffprobe). On ne s'y fie donc pas, et c'est la
        raison pour laquelle l'etape suivante reencode au lieu de copier le flux.

        Les deux liens sont essayes a chaque tour : ce sont deux hotes pour le meme fichier
        et l'un peut le publier avant l'autre.
        """
        url = (f"https://{self.rapidapi_sync_host}/download_audio/{video_id}"
               f"?quality={AUDIO_QUALITY}&trim_start_time={TRIM_START_S}"
               f"&trim_duration={TRIM_DURATION_S}")

        # ── 1. Le lien : une seule requete RapidAPI par beat ─────────────────
        while True:
            label, key = self.sync_key_rotator.current()
            if key is None:
                logger.error("❌ [dl] No usable RapidAPI account left")
                return False

            headers = {
                'Content-Type': 'application/json',
                'x-rapidapi-host': self.rapidapi_sync_host,
                'x-rapidapi-key': key,
            }
            try:
                logger.info(f"🚀 [dl] Requesting download link: id={video_id} (account: {label})")
                # 120 s de lecture et non 60 : l'endpoint fabrique le fragment AVANT de
                # repondre, il bloque donc ~18 s, parfois plus sur une longue video.
                r = requests.get(url, headers=headers, timeout=(10, 120))
            except requests.exceptions.RequestException as e:
                logger.warning(f"⚠️ [dl] Request failed: {e}")
                return False

            switched = self.sync_key_rotator.note_response(r.headers, used_label=label)

            if r.status_code in (403, 429):
                logger.warning(f"⚠️ [dl] {r.status_code} on '{label}': {r.text[:160]}")
                if not switched:
                    reason = ('is not subscribed to this API' if r.status_code == 403
                              else 'is out of monthly quota')
                    switched = self.sync_key_rotator.mark_unusable(label, reason)
                if switched:
                    continue
                logger.error("❌ [dl] Every configured account is spent")
                return False
            break

        if r.status_code != 200:
            logger.warning(f"⚠️ [dl] Non-200: {r.status_code} - {r.text[:200]}")
            return False

        try:
            data = r.json()
        except ValueError:
            logger.error(f"❌ [dl] non-JSON response: {r.text[:200]}")
            return False

        links = [u for u in (data.get('file'), data.get('reserved_file')) if u]
        if not links:
            logger.error(f"❌ [dl] no download link in response: {str(data)[:200]}")
            return False
        logger.info(f"🔎 [dl] link ready in ~{PREP_BUDGET_S}s max ({len(links)} host(s))")

        # ── 2. L'attente : le 404 veut dire "pas encore pret" ────────────────
        deadline = time.time() + PREP_BUDGET_S
        started = time.time()
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            for i, link in enumerate(links):
                which = 'primary' if i == 0 else 'mirror'
                try:
                    fr = requests.get(link, timeout=(10, 120), stream=True)
                except requests.exceptions.RequestException as e:
                    logger.warning(f"⚠️ [dl] {which} host failed: {e}")
                    continue

                if fr.status_code == 404:
                    fr.close()
                    continue        # en cours de fabrication, c'est normal

                # 'ogg' ajoute a la liste : c'est le type que ce fournisseur renvoie.
                ctype = fr.headers.get('content-type', '')
                if fr.status_code != 200 or not ('audio' in ctype or 'ogg' in ctype
                                                 or 'mpeg' in ctype or 'mp4' in ctype
                                                 or 'octet-stream' in ctype):
                    logger.warning(f"⚠️ [dl] {which} host: status={fr.status_code} type={ctype}")
                    fr.close()
                    continue

                written = 0
                with open(raw_path, 'wb') as f:
                    for chunk in fr.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            written += len(chunk)
                fr.close()
                logger.info(f"✅ [dl] downloaded from {which} after {attempt} poll(s), "
                            f"{int(time.time() - started)}s ({written // 1024} KB)")
                return True

            logger.info(f"⏳ [dl] not ready yet (poll {attempt})")
            time.sleep(POLL_EVERY_S)

        logger.error(f"❌ [dl] file never became available within {PREP_BUDGET_S}s")
        return False

    def _to_acr_sample(self, raw_path, video_id):
        """Normalise le fichier telecharge en un echantillon que ACRCloud recevra.

        On REENCODE, la ou l'ancienne version copiait le flux. Copier imposait de deviner
        le conteneur juste, et c'est exactement ce qui a casse le 19/08 : un flux MP3 n'a
        pas de tag dans un conteneur MP4, ffmpeg refusait, et l'echec declenchait un
        retelechargement complet. Le reencodage de 30 secondes coute une seconde de
        processeur et supprime toute la classe de bugs.

        MP3 vise en premier parce que services/acrcloud_service.py etiquette le fichier en
        dur ('audio.mp3', 'audio/mpeg') quel que soit son contenu : lui envoyer du MP3 est
        le seul cas ou l'etiquette dit la verite.

        Repli en WAV PCM si l'encodage MP3 echoue. libmp3lame est une bibliotheque externe,
        absente de certaines compilations de ffmpeg, alors que pcm_s16le est interne et
        toujours present. render.yaml n'installe pas ffmpeg (il vient de l'image de base),
        donc le depot ne peut rien garantir sur les encodeurs disponibles : le repli n'est
        pas de la prudence decorative, c'est la seule reponse a une inconnue reelle.
        """
        if not os.path.exists(raw_path) or os.path.getsize(raw_path) == 0:
            logger.error("❌ Downloaded file is missing or empty")
            return None

        for codec, ext, extra in (('libmp3lame', 'mp3', ['-b:a', '128k']),
                                  ('pcm_s16le', 'wav', ['-ar', '16000', '-ac', '1'])):
            out_path = os.path.join(self.temp_dir, f'beatlink_{video_id}.{ext}')
            try:
                logger.info(f"🔄 Encoding ACR sample as {ext} ({codec})")
                res = subprocess.run(
                    ['ffmpeg', '-y', '-v', 'error', '-i', raw_path,
                     '-t', '30', '-vn', '-c:a', codec] + extra + [out_path],
                    capture_output=True, timeout=90,
                )
            except Exception as e:
                logger.warning(f"⚠️ FFmpeg {ext} encode raised: {e}")
                continue

            if res.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                logger.info(f"✅ ACR sample ready: {out_path} "
                            f"({os.path.getsize(out_path) // 1024} KB)")
                return out_path

            logger.warning(f"⚠️ FFmpeg {ext} encode failed (rc={res.returncode}): "
                           f"{res.stderr.decode('utf-8', 'replace')[:200]}")

        logger.error("❌ Could not produce an ACR sample in any format")
        return None

    def download_audio(self, youtube_url):
        """Rend un echantillon de 30 secondes pour ACRCloud, ou None.

        Le chemin est desormais unique et lineaire : une requete, un fichier deja decoupe,
        un reencodage. Tout ce qui l'entourait a disparu avec la cause qui le justifiait.

        Le telechargement partiel (PARTIAL_CAP_BYTES) coupait le fichier a 1,5 Mo pour ne
        pas tirer 4 minutes de son inutiles. Le fournisseur decoupe maintenant lui-meme :
        le fichier fait 730 Ko et il est entierement utile.

        Le retelechargement complet en cas d'echec d'extraction existait parce qu'une coupe
        partielle pouvait tomber au mauvais endroit d'un conteneur MP4. Il n'y a plus de
        coupe partielle, et le reencodage ne depend plus du conteneur : ce repli couteux
        (210 s de plus pour reproduire le meme echec) n'a plus d'objet.
        """
        video_id = self._extract_video_id(youtube_url)

        if not video_id:
            logger.error("❌ Could not extract video ID from URL")
            return None

        # Un restant d'un scan precedent sur le meme beat serait relu tel quel : on nettoie
        # les trois noms que ce beat peut produire avant de commencer.
        for name in (f'beatlink_{video_id}_raw.opus',
                     f'beatlink_{video_id}.mp3',
                     f'beatlink_{video_id}.wav'):
            stale = os.path.join(self.temp_dir, name)
            if os.path.exists(stale):
                os.remove(stale)

        raw_path = os.path.join(self.temp_dir, f'beatlink_{video_id}_raw.opus')

        try:
            logger.info(f"🎵 Downloading audio for {video_id}...")
            if not self._fetch_raw_via_sync_api(video_id, raw_path):
                logger.error("❌ Audio download failed")
                return None
            return self._to_acr_sample(raw_path, video_id)
        except Exception as e:
            logger.error(f"❌ Download error: {str(e)}", exc_info=True)
            return None

    def cleanup_audio(self, audio_path):
        """Delete temporary audio file"""
        try:
            if audio_path and os.path.exists(audio_path):
                os.remove(audio_path)
                logger.info(f"🗑️ Cleaned up: {audio_path}")
                return True
        except Exception as e:
            logger.warning(f"⚠️ Cleanup failed: {str(e)}")
        return False
