import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Optional, List, Dict
import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class TrackInfo:
    title: str
    artist: str
    duration: int  # in seconds
    spotify_url: str
    cover_url: Optional[str]
    genre: str


# Pre-mapped official Spotify playlists for genre random track discovery (no API keys required)
GENRE_PLAYLISTS: Dict[str, str] = {
    "pop": "37i9dQZF1DXcBWIGoYBM5M",         # Today's Top Hits
    "rock": "37i9dQZF1DWXRqgorJj26U",        # Rock Classics
    "hip-hop": "37i9dQZF1DX0XUsuxWHRQd",     # RapCaviar
    "electronic": "37i9dQZF1DX4dyzvuaRJ0n",  # mint
    "indie": "37i9dQZF1DX2Nc3B70tvx0",       # Ultimate Indie
    "jazz": "37i9dQZF1DXbITWG1ZJKYt",        # Jazz Classics
    "metal": "37i9dQZF1DX9qNs32fujYe",       # Heavy Metal
    "r-n-b": "37i9dQZF1DX4Y4RhrZqHhr",       # R&B Classics
    "classical": "37i9dQZF1DWWEJlAGA9gs0",   # Classical Essentials
    "synthwave": "37i9dQZF1DXdLEN7aqioXM",   # RetroWave / Outrun
    "blues": "37i9dQZF1DXd9rSDyQguIk",       # Blues Classics
    "country": "37i9dQZF1DX1lVhptIYRda",     # Hot Country
    "folk": "37i9dQZF1DXaUDcU6KDCj4",        # Folk & Acoustic
    "reggae": "37i9dQZF1DXbSbnqxMTGx9",      # Reggae Classics
    "disco": "37i9dQZF1DXaXB8fQg7xif",       # Disco Fever
    "ambient": "37i9dQZF1DX3Ogo9pFvBkY",     # Ambient Relaxation
    "lo-fi": "37i9dQZF1DX8Uebhn9wzrS",       # Chill Lofi Study Beats
    "k-pop": "37i9dQZF1DX9tPFwDMOaN1",       # K-Pop ON!
}

SUPPORTED_GENRES = list(GENRE_PLAYLISTS.keys())

# Built-in curated safety fallback tracks in case network is down
FALLBACK_TRACKS: Dict[str, List[Dict[str, str]]] = {
    "rock": [
        {"title": "Bohemian Rhapsody", "artist": "Queen", "url": "https://open.spotify.com/track/7tFiyTwD0nx5a1eklYtX2J"},
        {"title": "Sweet Child O' Mine", "artist": "Guns N' Roses", "url": "https://open.spotify.com/track/7o2CTH4ctstm8TNelqjb51"},
        {"title": "Smells Like Teen Spirit", "artist": "Nirvana", "url": "https://open.spotify.com/track/5ghIJD29wCJuNy9NvcNUvN"},
    ],
    "pop": [
        {"title": "Blinding Lights", "artist": "The Weeknd", "url": "https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b"},
        {"title": "As It Was", "artist": "Harry Styles", "url": "https://open.spotify.com/track/4LRPiXqCikLlN15c3yImP7"},
        {"title": "Levitating", "artist": "Dua Lipa", "url": "https://open.spotify.com/track/463CkQjx2Zk1yXoBuierM9"},
    ],
    "electronic": [
        {"title": "Get Lucky", "artist": "Daft Punk", "url": "https://open.spotify.com/track/2Foc5Q5nqNiosCNqttzHof"},
        {"title": "Strobe", "artist": "deadmau5", "url": "https://open.spotify.com/track/4fukgq4n6Ff53u4Bf25iXn"},
        {"title": "Midnight City", "artist": "M83", "url": "https://open.spotify.com/track/1eyzqe2QqGZUmfcPZtrIyt"},
    ],
    "hip-hop": [
        {"title": "Lose Yourself", "artist": "Eminem", "url": "https://open.spotify.com/track/7w9BgsqALRI73T2d9gY05r"},
        {"title": "HUMBLE.", "artist": "Kendrick Lamar", "url": "https://open.spotify.com/track/7KXjTSCq5nL1LoYtL7XAwS"},
        {"title": "God's Plan", "artist": "Drake", "url": "https://open.spotify.com/track/6DCZcSspjsKoFjzjrWoCdn"},
    ],
    "synthwave": [
        {"title": "Resonance", "artist": "HOME", "url": "https://open.spotify.com/track/1Tu2x9gM9y3o1q4QWvYp2k"},
        {"title": "Tech Noir", "artist": "Gunship", "url": "https://open.spotify.com/track/5V44r4a1mGj4jD2y7E6R5f"},
        {"title": "Sunset", "artist": "The Midnight", "url": "https://open.spotify.com/track/4N0WvdNDfl2zbLbsY7umNx"},
    ],
}


class SpotifyService:
    def __init__(self, client_id: str = "", client_secret: str = ""):
        self.client_id = client_id.strip() if client_id else ""
        self.client_secret = client_secret.strip() if client_secret else ""
        self._sp_client = None

        if self.client_id and self.client_secret:
            try:
                import spotipy
                from spotipy.oauth2 import SpotifyClientCredentials
                auth_manager = SpotifyClientCredentials(
                    client_id=self.client_id,
                    client_secret=self.client_secret
                )
                self._sp_client = spotipy.Spotify(auth_manager=auth_manager)
                logger.info("OpenSong: Spotify official API client initialized.")
            except Exception as e:
                logger.warning("OpenSong: Failed to initialize official Spotipy client: %s", e)

    async def get_random_song(self, allowed_genres: Optional[List[str]] = None) -> Optional[TrackInfo]:
        """
        Gets a random song from Spotify based on the provided list of allowed genres.
        Uses official Spotipy if configured, otherwise fetches from official Spotify embed playlists.
        """
        if not allowed_genres:
            allowed_genres = SUPPORTED_GENRES

        # Filter valid genres
        valid_genres = [g for g in allowed_genres if g in SUPPORTED_GENRES]
        if not valid_genres:
            valid_genres = SUPPORTED_GENRES

        selected_genre = random.choice(valid_genres)

        # 1. Try Spotipy official API if available
        if self._sp_client:
            try:
                track = await asyncio.to_thread(self._fetch_via_spotipy, selected_genre)
                if track:
                    return track
            except Exception as e:
                logger.warning("OpenSong: Spotipy search failed: %s. Falling back to embed.", e)

        # 2. Try fetching from public Spotify embed playlist
        try:
            track = await self._fetch_via_embed(selected_genre)
            if track:
                return track
        except Exception as e:
            logger.warning("OpenSong: Spotify embed fetch failed for genre %s: %s", selected_genre, e)

        # 3. Ultimate fallback: static curated catalog
        return self._fetch_fallback(selected_genre)

    def _fetch_via_spotipy(self, genre: str) -> Optional[TrackInfo]:
        import spotipy
        offset = random.randint(0, 50)
        query = f"genre:\"{genre}\""
        results = self._sp_client.search(q=query, type="track", limit=20, offset=offset)
        items = results.get("tracks", {}).get("items", [])
        if not items:
            # Fallback search without quotes
            results = self._sp_client.search(q=genre, type="track", limit=20, offset=offset)
            items = results.get("tracks", {}).get("items", [])

        if not items:
            return None

        chosen = random.choice(items)
        title = chosen.get("name", "Unknown")
        artists = ", ".join(a.get("name", "") for a in chosen.get("artists", []))
        duration = int(chosen.get("duration_ms", 180000) // 1000)
        spotify_url = chosen.get("external_urls", {}).get("spotify", "")
        images = chosen.get("album", {}).get("images", [])
        cover_url = images[0].get("url") if images else None

        return TrackInfo(
            title=title,
            artist=artists,
            duration=duration,
            spotify_url=spotify_url,
            cover_url=cover_url,
            genre=genre
        )

    async def _fetch_via_embed(self, genre: str) -> Optional[TrackInfo]:
        playlist_id = GENRE_PLAYLISTS.get(genre, GENRE_PLAYLISTS["pop"])
        url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()

            match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html)
            if not match:
                return None

            data = json.loads(match.group(1))
            props = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {})
            entity = props.get("entity", {})
            tracks = entity.get("trackList", [])
            if not tracks:
                return None

            chosen = random.choice(tracks)
            title = chosen.get("title", "Unknown")
            artist = chosen.get("subtitle", "Unknown")
            duration_ms = chosen.get("duration", 180000)
            duration = int(duration_ms // 1000)

            uri = chosen.get("uri", "")
            track_id = uri.split(":")[-1] if uri else ""
            spotify_url = f"https://open.spotify.com/track/{track_id}" if track_id else ""

            # Fetch cover image via Spotify oembed endpoint
            cover_url = None
            if spotify_url:
                try:
                    oembed_url = f"https://open.spotify.com/oembed?url={spotify_url}"
                    async with session.get(oembed_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as om:
                        if om.status == 200:
                            om_data = await om.json(content_type=None)
                            cover_url = om_data.get("thumbnail_url")
                except Exception:
                    pass

            return TrackInfo(
                title=title,
                artist=artist,
                duration=duration,
                spotify_url=spotify_url,
                cover_url=cover_url,
                genre=genre
            )

    def _fetch_fallback(self, genre: str) -> TrackInfo:
        candidates = FALLBACK_TRACKS.get(genre, FALLBACK_TRACKS.get("pop", []))
        if not candidates:
            candidates = FALLBACK_TRACKS["pop"]
        chosen = random.choice(candidates)
        return TrackInfo(
            title=chosen["title"],
            artist=chosen["artist"],
            duration=210,
            spotify_url=chosen["url"],
            cover_url=None,
            genre=genre
        )

