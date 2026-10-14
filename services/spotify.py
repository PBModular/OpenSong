import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Optional, List, Dict, Set
import urllib.parse
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


# Rich subgenres for deep, diverse music discovery across decades (avoiding radio charts)
GENRE_SUBGENRES: Dict[str, List[str]] = {
    "pop": [
        "80s synthpop", "90s pop classics", "city pop", "art pop",
        "dream pop", "sophisti-pop", "indie pop gems", "french pop 80s",
        "new wave pop", "chamber pop", "yacht rock pop"
    ],
    "rock": [
        "classic rock 70s", "progressive rock", "post-punk", "grunge 90s",
        "shoegaze", "psychedelic rock", "indie rock 2000s", "hard rock legends",
        "krautrock", "noise rock", "alternative 90s", "blues rock"
    ],
    "electronic": [
        "synthwave outrun", "90s idm", "trip-hop", "classic deep house",
        "french touch", "ambient electronic", "downtempo chill", "breakbeat 90s",
        "berlin techno", "electro swing", "classic drum and bass"
    ],
    "hip-hop": [
        "90s golden era hip hop", "underground hip hop", "boom bap classics",
        "conscious rap", "90s east coast hip hop", "2000s classic rap",
        "jazz rap", "lo-fi hip hop chill", "southern hip hop 90s"
    ],
    "indie": [
        "indie rock 2000s", "shoegaze essentials", "dream pop gems",
        "post-punk revival", "lo-fi indie", "chamber pop", "midwest emo",
        "indie folk classics", "math rock", "slowcore"
    ],
    "jazz": [
        "hard bop jazz", "cool jazz classics", "blue note records", "modal jazz",
        "jazz fusion", "vocal jazz standards", "spiritual jazz", "bebop classics"
    ],
    "metal": [
        "heavy metal classics", "thrash metal legends", "doom metal",
        "progressive metal", "90s alternative metal", "stoner rock",
        "melodic death metal", "sludge metal", "power metal"
    ],
    "r-n-b": [
        "90s r&b classics", "motown classics", "neo-soul essentials",
        "classic soul 70s", "contemporary r&b slow jams", "funk classics 70s"
    ],
    "classical": [
        "baroque masterpieces", "romantic era classical", "chopin nocturnes",
        "orchestral masterpieces", "piano classics", "impressionist classical"
    ],
    "synthwave": [
        "synthwave outrun", "retrowave essentials", "darksynth",
        "cyberpunk synthwave", "dreamwave synth", "80s outrun electro"
    ],
    "blues": [
        "delta blues", "chicago blues classics", "electric blues",
        "texas blues legends", "blues rock 70s"
    ],
    "country": [
        "outlaw country classics", "classic country 70s", "bluegrass essentials",
        "americana roots", "alt-country"
    ],
    "folk": [
        "indie folk", "60s folk revival", "acoustic folk classics",
        "neo-folk", "traditional celtic folk"
    ],
    "reggae": [
        "roots reggae classics", "dub reggae essentials", "rocksteady",
        "ska classics", "lovers rock"
    ],
    "disco": [
        "70s disco classics", "italo disco 80s", "nu-disco",
        "funk disco essentials", "eurodisco"
    ],
    "ambient": [
        "ambient soundscapes", "space ambient", "dark ambient",
        "drone ambient", "relaxing ambient", "modular synth ambient"
    ],
    "lo-fi": [
        "lofi hip hop beats", "chillhop study beats", "lofi jazz beats",
        "relaxing lofi", "vintage lofi"
    ],
    "k-pop": [
        "k-pop b-sides", "k-indie gems", "k-r&b essentials", "k-rock"
    ],
}

# Curated classics & retro Spotify playlist pools (purged of commercial top-40 payola)
GENRE_PLAYLISTS: Dict[str, List[str]] = {
    "pop": [
        "37i9dQZF1DX4UtSsGT1Sbe",  # All Out 80s
        "37i9dQZF1DXbTxeAdrVG2l",  # All Out 90s
        "37i9dQZF1DX4o1oenSJRJd",  # All Out 00s
        "37i9dQZF1DWTE7dVYebp79",  # Synthpop
    ],
    "rock": [
        "37i9dQZF1DWXRqgorJj26U",  # Rock Classics
        "37i9dQZF1DX8FwnYE6PRS3",  # Modern Rock
        "37i9dQZF1DX3oM43CtKnRV",  # Hard Rock
        "37i9dQZF1DX1rVvRgjXZwF",  # 00s Rock Anthems
    ],
    "electronic": [
        "37i9dQZF1DXdLEN7aqioXM",  # RetroWave / Synthwave
        "37i9dQZF1DX8tZsk68tuDw",  # Dance Rising
        "37i9dQZF1DX1T2fHl4Nxm8",  # Housewerk
    ],
    "hip-hop": [
        "37i9dQZF1DWVA1Gq4XHa6U",  # Gold School
        "37i9dQZF1DX186v583rmzp",  # I Love My '90s Hip-Hop
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

SUPPORTED_GENRES = list(GENRE_SUBGENRES.keys())

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
        max_duration: int = 600,
    ) -> Optional[TrackInfo]:
        """
        Gets a truly random song from Spotify based on the provided list of allowed genres.
        Uses randomized wildcard queries, genre pools, filters out recently sent tracks,
        and enforces max_duration.
        """
        if not allowed_genres:
            allowed_genres = SUPPORTED_GENRES

        valid_genres = [g for g in allowed_genres if g in SUPPORTED_GENRES]
        if not valid_genres:
            valid_genres = SUPPORTED_GENRES

        selected_genre = random.choice(valid_genres)
        exclude_set = {t.lower().strip() for t in exclude_titles} if exclude_titles else set()

        # 1. Official Spotipy API with deep randomized search (if credentials provided)
        if self._sp_client:
            try:
                track = await asyncio.to_thread(self._fetch_via_spotipy, selected_genre, exclude_set, max_duration)
                if track:
                    return track
            except Exception as e:
                logger.warning("OpenSong: Spotipy search failed: %s. Falling back to Deezer subgenres.", e)

        # 2. Deep subgenre discovery via Deezer open catalog (90M+ tracks, no auth required, non-commercial)
        try:
            track = await self._fetch_via_deezer_subgenres(selected_genre, exclude_set, max_duration)
            if track:
                return track
        except Exception as e:
            logger.warning("OpenSong: Deezer subgenre fetch failed for genre %s: %s", selected_genre, e)

        # 3. Curated classic Spotify embed playlist pool (fallback)
        try:
            track = await self._fetch_via_embed(selected_genre, exclude_set, max_duration)
            if track:
                return track
        except Exception as e:
            logger.warning("OpenSong: Spotify playlist pool fetch failed for genre %s: %s", selected_genre, e)

        # 4. Fallback ignoring exclusion filter if pools exhausted
        try:
            track = await self._fetch_via_deezer_subgenres(selected_genre, set(), max_duration)
            if track:
                return track
        except Exception:
            pass

        try:
            track = await self._fetch_via_embed(selected_genre, set(), max_duration)
            if track:
                return track
        except Exception:
            pass

        return None

    async def _fetch_via_deezer_subgenres(
        self,
        genre: str,
        exclude_titles: Set[str],
        max_duration: int = 600,
    ) -> Optional[TrackInfo]:
        """
        Fetches tracks across rich subgenres via Deezer's open catalog (no auth required).
        Queries subgenre enthusiast playlists to unearth deep cuts, underground gems, and authentic classics
        instead of commercial Top-40 Billboard radio hits.
        """
        subgenres = list(GENRE_SUBGENRES.get(genre, [genre]))
        random.shuffle(subgenres)

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        async with aiohttp.ClientSession() as session:
            for subgenre in subgenres[:3]:
                url = f"https://api.deezer.com/search/playlist?q={urllib.parse.quote(subgenre)}&limit=15"
                try:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.json(content_type=None)
                        playlists = [p for p in data.get("data", []) if p.get("nb_tracks", 0) >= 5]
                        if not playlists:
                            continue

                        random.shuffle(playlists)
                        for pl in playlists[:2]:
                            pl_id = pl.get("id")
                            if not pl_id:
                                continue
                            pl_url = f"https://api.deezer.com/playlist/{pl_id}/tracks?limit=50"
                            async with session.get(pl_url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as tresp:
                                if tresp.status != 200:
                                    continue
                                tdata = await tresp.json(content_type=None)
                                tracks = tdata.get("data", [])
                                if not tracks:
                                    continue

                                # Filter excluded tracks and check duration
                                candidates = []
                                for t in tracks:
                                    name = (t.get("title") or t.get("title_short") or "").strip()
                                    if not name:
                                        continue
                                    if exclude_titles and name.lower() in exclude_titles:
                                        continue
                                    dur = int(t.get("duration", 0))
                                    if max_duration > 0 and dur > max_duration:
                                        continue
                                    candidates.append(t)

                                if not candidates:
                                    continue

                                chosen = random.choice(candidates)
                                title = chosen.get("title") or chosen.get("title_short") or "Unknown"
                                artist = chosen.get("artist", {}).get("name", "Unknown")
                                duration = int(chosen.get("duration", 180))
                                album = chosen.get("album", {})
                                cover_url = (
                                    album.get("cover_xl")
                                    or album.get("cover_big")
                                    or album.get("cover_medium")
                                    or album.get("cover")
                                )
                                spotify_url = f"https://open.spotify.com/search/{urllib.parse.quote(f'{artist} {title}')}"

                                return TrackInfo(
                                    title=title,
                                    artist=artist,
                                    duration=duration,
                                    spotify_url=spotify_url,
                                    cover_url=cover_url,
                                    genre=genre,
                                )
                except Exception as e:
                    logger.debug("Deezer subgenre fetch error for %s (%s): %s", genre, subgenre, e)
                    continue

        return None

    def _fetch_via_spotipy(self, genre: str, exclude_titles: Set[str], max_duration: int = 600) -> Optional[TrackInfo]:
        """Performs deep randomized searches using random seeds, years, and offsets."""
        random_seed = random.choice(SEARCH_WILDCARDS)
        random_year = random.randint(1975, 2024)
        subgenre = random.choice(GENRE_SUBGENRES.get(genre, [genre]))

        query_strategies = [
            f"genre:\"{genre}\" year:{random_year}",
            f"\"{subgenre}\" year:{random_year}",
            f"\"{subgenre}\"",
            f"{random_seed} genre:\"{genre}\"",
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

        # Filter out tracks with excessive mainstream popularity (> 80) to avoid radio charts
        if items:
            non_mainstream = [i for i in items if i.get("popularity", 0) <= 80]
            if non_mainstream:
                items = non_mainstream

        # Filter out previously sent tracks and tracks exceeding duration limit
        if items and exclude_titles:
            filtered = [i for i in items if i.get("name", "").lower().strip() not in exclude_titles]
            if filtered:
                items = filtered

        if items and max_duration > 0:
            dur_filtered = [i for i in items if (i.get("duration_ms", 0) // 1000) <= max_duration]
            if dur_filtered:
                items = dur_filtered

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
                if items:
                    non_mainstream = [i for i in items if i.get("popularity", 0) <= 80]
                    if non_mainstream:
                        items = non_mainstream
                if items and exclude_titles:
                    filtered = [i for i in items if i.get("name", "").lower().strip() not in exclude_titles]
                    if filtered:
                        items = filtered
                if items and max_duration > 0:
                    dur_filtered = [i for i in items if (i.get("duration_ms", 0) // 1000) <= max_duration]
                    if dur_filtered:
                        items = dur_filtered
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

    async def _fetch_via_embed(self, genre: str, exclude_titles: Set[str], max_duration: int = 600) -> Optional[TrackInfo]:
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

                    # Filter out tracks exceeding max_duration
                    if max_duration > 0:
                        candidate_tracks = [
                            t for t in tracks if (t.get("duration", 0) // 1000) <= max_duration
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
