import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Optional, List, Dict, Set
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


# Multi-playlist pools per genre to guarantee large variety across hundreds/thousands of tracks
GENRE_PLAYLISTS: Dict[str, List[str]] = {
    "pop": [
        "37i9dQZF1DXcBWIGoYBM5M",  # Today's Top Hits
        "37i9dQZF1DX4UtSsGT1Sbe",  # All Out 80s
        "37i9dQZF1DXbTxeAdrVG2l",  # All Out 90s
        "37i9dQZF1DXbYM3nMMSAfg",  # Pop Rising
        "37i9dQZF1DX0b1W992W6fV",  # Hit Rewind
    ],
    "rock": [
        "37i9dQZF1DWXRqgorJj26U",  # Rock Classics
        "37i9dQZF1DWZryfp6NSvtz",  # All New Rock
        "37i9dQZF1DX1rVvRgjXZwF",  # 00s Rock Anthems
        "37i9dQZF1DX8FwnYE6PRS3",  # Modern Rock
        "37i9dQZF1DX3oM43CtKnRV",  # Hard Rock
    ],
    "electronic": [
        "37i9dQZF1DX4dyzvuaRJ0n",  # mint
        "37i9dQZF1DX8tZsk68tuDw",  # Dance Rising
        "37i9dQZF1DX1T2fHl4Nxm8",  # Housewerk
        "37i9dQZF1DXdLEN7aqioXM",  # RetroWave / Synthwave
    ],
    "hip-hop": [
        "37i9dQZF1DX0XUsuxWHRQd",  # RapCaviar
        "37i9dQZF1DX186v583rmzp",  # I Love My '90s Hip-Hop
        "37i9dQZF1DWVA1Gq4XHa6U",  # Gold School
        "37i9dQZF1DX2RxBh64g9LU",  # Hip-Hop Central
    ],
    "indie": [
        "37i9dQZF1DX2Nc3B70tvx0",  # Indie's Top 50
        "37i9dQZF1DXdbXrPNafg9d",  # All New Indie
        "37i9dQZF1DX7gIoKmxkYYi",  # Indie Pop
    ],
    "jazz": [
        "37i9dQZF1DXbITWG1ZJKYt",  # Jazz Classics
        "37i9dQZF1DX4wta20PHgwo",  # Late Night Jazz
    ],
    "metal": [
        "37i9dQZF1DX9qNs32fujYe",  # Heavy Metal
        "37i9dQZF1DX5J7oVvC6y5y",  # Kickass Metal
    ],
    "r-n-b": [
        "37i9dQZF1DX4Y4RhrZqHhr",  # R&B Classics
        "37i9dQZF1DX4SBhb3fqAp5",  # Are & Be
    ],
    "classical": [
        "37i9dQZF1DWWEJlAGA9gs0",  # Classical Essentials
    ],
    "synthwave": [
        "37i9dQZF1DXdLEN7aqioXM",  # RetroWave / Outrun
    ],
    "blues": [
        "37i9dQZF1DXd9rSDyQguIk",  # Blues Classics
    ],
    "country": [
        "37i9dQZF1DX1lVhptIYRda",  # Hot Country
    ],
    "folk": [
        "37i9dQZF1DXaUDcU6KDCj4",  # Folk & Acoustic
    ],
    "reggae": [
        "37i9dQZF1DXbSbnqxMTGx9",  # Reggae Classics
    ],
    "disco": [
        "37i9dQZF1DXaXB8fQg7xif",  # Disco Fever
    ],
    "ambient": [
        "37i9dQZF1DX3Ogo9pFvBkY",  # Ambient Relaxation
    ],
    "lo-fi": [
        "37i9dQZF1DX8Uebhn9wzrS",  # Chill Lofi Study Beats
    ],
    "k-pop": [
        "37i9dQZF1DX9tPFwDMOaN1",  # K-Pop ON!
    ],
}

SUPPORTED_GENRES = list(GENRE_PLAYLISTS.keys())

# Syllable / character wildcards for randomized Spotify search queries
SEARCH_WILDCARDS = [
    "a", "e", "i", "o", "u", "th", "sh", "ch", "st", "re",
    "li", "lo", "ma", "ra", "so", "da", "ne", "ve", "wa", "al"
]


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

    async def get_random_song(
        self,
        allowed_genres: Optional[List[str]] = None,
        exclude_titles: Optional[Set[str]] = None,
    ) -> Optional[TrackInfo]:
        """
        Gets a truly random song from Spotify based on the provided list of allowed genres.
        Uses randomized wildcard queries, genre pools, and filters out recently sent tracks.
        """
        if not allowed_genres:
            allowed_genres = SUPPORTED_GENRES

        valid_genres = [g for g in allowed_genres if g in SUPPORTED_GENRES]
        if not valid_genres:
            valid_genres = SUPPORTED_GENRES

        selected_genre = random.choice(valid_genres)
        exclude_set = {t.lower().strip() for t in exclude_titles} if exclude_titles else set()

        # 1. Official Spotipy API with deep randomized search
        if self._sp_client:
            try:
                track = await asyncio.to_thread(self._fetch_via_spotipy, selected_genre, exclude_set)
                if track:
                    return track
            except Exception as e:
                logger.warning("OpenSong: Spotipy search failed: %s. Falling back to playlist pool.", e)

        # 2. Public Spotify embed multi-playlist pool
        try:
            track = await self._fetch_via_embed(selected_genre, exclude_set)
            if track:
                return track
        except Exception as e:
            logger.warning("OpenSong: Spotify playlist pool fetch failed for genre %s: %s", selected_genre, e)

        # 3. Fallback from available pool ignoring exclude filter if pool exhausted
        try:
            track = await self._fetch_via_embed(selected_genre, set())
            if track:
                return track
        except Exception:
            pass

        return None

    def _fetch_via_spotipy(self, genre: str, exclude_titles: Set[str]) -> Optional[TrackInfo]:
        """Performs deep randomized searches using random seeds, years, and offsets."""
        random_seed = random.choice(SEARCH_WILDCARDS)
        random_year = random.randint(1975, 2026)

        query_strategies = [
            f"{random_seed} genre:\"{genre}\" year:{random_year}",
            f"{random_seed} genre:\"{genre}\"",
            f"genre:\"{genre}\" year:{random_year}",
            f"genre:\"{genre}\"",
        ]

        items = []
        for query in query_strategies:
            offset = random.randint(0, 150)
            try:
                results = self._sp_client.search(q=query, type="track", limit=50, offset=offset)
                found = results.get("tracks", {}).get("items", [])
                if found:
                    items = found
                    break
            except Exception:
                continue

        # Filter out previously sent tracks
        if items and exclude_titles:
            filtered = [i for i in items if i.get("name", "").lower().strip() not in exclude_titles]
            if filtered:
                items = filtered

        # If search didn't yield anything, try the Spotify recommendations engine
        if not items:
            try:
                seed_genres = [genre]
                rec_results = self._sp_client.recommendations(
                    seed_genres=seed_genres,
                    limit=30,
                    target_energy=random.uniform(0.2, 0.95),
                    target_valence=random.uniform(0.1, 0.95),
                    target_danceability=random.uniform(0.2, 0.9)
                )
                items = rec_results.get("tracks", [])
                if items and exclude_titles:
                    filtered = [i for i in items if i.get("name", "").lower().strip() not in exclude_titles]
                    if filtered:
                        items = filtered
            except Exception as e:
                logger.debug("Spotify recommendations fallback error: %s", e)

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

    async def _fetch_via_embed(self, genre: str, exclude_titles: Set[str]) -> Optional[TrackInfo]:
        """Randomly picks a playlist from the genre's multi-playlist pool and selects a track."""
        playlist_pool = GENRE_PLAYLISTS.get(genre, GENRE_PLAYLISTS["pop"])
        # Shuffle playlists to try
        shuffled_pool = list(playlist_pool)
        random.shuffle(shuffled_pool)

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        async with aiohttp.ClientSession() as session:
            for playlist_id in shuffled_pool:
                url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
                try:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                        if resp.status != 200:
                            continue
                        html = await resp.text()

                    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html)
                    if not match:
                        continue

                    data = json.loads(match.group(1))
                    props = data.get("props", {}).get("pageProps", {}).get("state", {}).get("data", {})
                    entity = props.get("entity", {})
                    tracks = entity.get("trackList", [])
                    if not tracks:
                        continue

                    # Filter out excluded tracks
                    if exclude_titles:
                        candidate_tracks = [
                            t for t in tracks if t.get("title", "").lower().strip() not in exclude_titles
                        ]
                        if candidate_tracks:
                            tracks = candidate_tracks

                    chosen = random.choice(tracks)
                    title = chosen.get("title", "Unknown")
                    artist = chosen.get("subtitle", "Unknown")
                    duration_ms = chosen.get("duration", 180000)
                    duration = int(duration_ms // 1000)

                    uri = chosen.get("uri", "")
                    track_id = uri.split(":")[-1] if uri else ""
                    spotify_url = f"https://open.spotify.com/track/{track_id}" if track_id else ""

                    # Fetch high resolution cover art via oEmbed
                    cover_url = None
                    if spotify_url:
                        try:
                            oembed_url = f"https://open.spotify.com/oembed?url={spotify_url}"
                            async with session.get(oembed_url, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as om:
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

                except Exception as e:
                    logger.debug("Error checking playlist %s: %s", playlist_id, e)
                    continue

        return None
